# -*- coding: utf-8 -*-
"""Zabbix 7 API 客户端：只读查询、结果聚合与轻量缓存。

被 Web 后端 (server.py) 与对话引擎 (chat.py) 共同复用。
全部基于标准库，无需第三方依赖。
"""
import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------- 常量映射
SEVERITY = {0: "未分类", 1: "信息", 2: "警告", 3: "一般", 4: "严重", 5: "灾难"}
SEVERITY_KEY = {0: "na", 1: "info", 2: "warn", 3: "avg", 4: "high", 5: "disaster"}
AVAILABILITY = {"0": "未知", "1": "可用", "2": "不可用"}
AVAILABILITY_KEY = {"0": "unknown", "1": "ok", "2": "down"}
HOST_STATUS = {"0": "启用", "1": "停用"}
IFACE_TYPE = {"1": "Zabbix agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}
OPER_STATUS = {
    "1": "UP", "2": "DOWN", "3": "testing", "4": "unknown",
    "5": "dormant", "6": "notPresent", "7": "lowerLayerDown",
}


class ZabbixError(RuntimeError):
    """Zabbix API 调用失败。"""


def load_env():
    """依次读取工作区根目录与 webapp 目录下的 .env。"""
    env = {}
    for path in (os.path.join(ROOT, ".env"), os.path.join(HERE, ".env")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    return env


def human_age(clock):
    """unix 时间戳 -> 「3 天 4 小时前」这类可读文案。"""
    try:
        delta = max(0, int(time.time()) - int(clock))
    except (TypeError, ValueError):
        return "-"
    if delta < 60:
        return "%d 秒前" % delta
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    return "%d 天 %d 小时前" % (delta // 86400, (delta % 86400) // 3600)


def parse_iface_name(item_name):
    """'Interface Et0/0(): Operational status' -> ('Et0/0', 'Operational status')"""
    if not item_name.startswith("Interface "):
        return None, None
    rest = item_name[len("Interface "):]
    if "()" not in rest:
        return None, None
    iface, _, metric = rest.partition("()")
    iface = iface.strip()
    metric = metric.lstrip(":").strip()
    if not iface or not metric:
        return None, None
    return iface, metric


class Zabbix(object):
    """Zabbix JSON-RPC 只读客户端（结果带 TTL 缓存）。"""

    def __init__(self, url=None, token=None):
        env = load_env()
        self.url = (url or env.get("ZABBIX_URL", "")).rstrip("/")
        self.token = token or env.get("ZABBIX_TOKEN", "")
        self.api = self.url + "/api_jsonrpc.php"
        self._id = 0
        self._cache = {}

    # ------------------------------------------------------------ 底层调用
    def call(self, method, params=None, auth=True, timeout=25):
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._id,
        }
        headers = {"Content-Type": "application/json-rpc"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(
            self.api, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ZabbixError("HTTP %s 访问 %s 失败" % (exc.code, self.url)) from exc
        except urllib.error.URLError as exc:
            raise ZabbixError("无法连接 Zabbix %s：%s" % (self.url, exc.reason)) from exc
        except OSError as exc:
            raise ZabbixError("网络异常：%s" % exc) from exc
        if "error" in body:
            err = body["error"]
            raise ZabbixError("%s 失败：%s / %s" % (method, err.get("message"), err.get("data")))
        return body.get("result")

    def _cached(self, key, ttl, producer):
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        val = producer()
        self._cache[key] = (time.time(), val)
        return val

    def invalidate(self):
        self._cache.clear()

    # ------------------------------------------------------------ 原始数据
    def version(self):
        return self._cached("version", 120, lambda: self.call("apiinfo.version", auth=False))

    def raw_hosts(self):
        return self._cached("hosts", 5, lambda: self.call("host.get", {
            "output": ["hostid", "host", "name", "status", "description",
                       "maintenance_status", "active_available"],
            "selectInterfaces": ["interfaceid", "ip", "dns", "port", "type",
                                 "available", "error"],
            "selectGroups": ["groupid", "name"],
            "selectParentTemplates": ["templateid", "name"],
            "sortfield": "host",
        }))

    def raw_items(self):
        return self._cached("items", 5, lambda: self.call("item.get", {
            "output": ["itemid", "hostid", "name", "key_", "state", "status",
                       "lastvalue", "lastclock", "units", "error", "value_type"],
            "limit": 20000,
        }))

    def raw_problems(self):
        # 不传 recent -> 仅返回未恢复的活跃问题。
        # 注意：Zabbix 7 的 problem.get 不支持 selectHosts，
        # 主机信息需通过 objectid(=triggerid) 关联 trigger 获取。
        return self._cached("problems", 5, lambda: self.call("problem.get", {
            "output": ["eventid", "objectid", "clock", "name", "severity",
                       "acknowledged"],
            "selectTags": "extend",
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": 500,
        }))

    def _trigger_map(self):
        """triggerid -> 主机名 / 主机ID，用于把 problem 关联回主机。"""
        def build():
            names, hostids = {}, {}
            for t in self.raw_triggers():
                tid = t["triggerid"]
                names[tid] = [h["host"] for h in t.get("hosts", [])]
                hostids[tid] = [h["hostid"] for h in t.get("hosts", [])]
            return {"names": names, "hostids": hostids}
        return self._cached("trigger_map", 5, build)

    def raw_triggers(self):
        return self._cached("triggers", 5, lambda: self.call("trigger.get", {
            "output": ["triggerid", "description", "priority", "value",
                       "lastchange", "status"],
            "only_true": True,
            "monitored": True,
            "selectHosts": ["hostid", "host", "name"],
            "sortfield": "priority",
            "sortorder": "DESC",
            "limit": 500,
        }))

    def raw_templates(self):
        return self._cached("templates", 60, lambda: self.call("template.get", {
            "output": ["templateid", "host", "name"], "sortfield": "host",
        }))

    def raw_discovery_rules(self):
        return self._cached("drules", 60, lambda: self.call("discoveryrule.get", {
            "output": ["itemid", "name", "key_", "status", "state"],
        }))

    def raw_proxies(self):
        def fetch():
            try:
                return self.call("proxy.get", {
                    "output": ["proxyid", "name", "operating_mode", "lastaccess"],
                    "selectHosts": ["hostid"],
                })
            except ZabbixError:
                return []
        return self._cached("proxies", 60, fetch)

    def raw_maintenances(self):
        def fetch():
            try:
                return self.call("maintenance.get", {
                    "output": ["maintenanceid", "name", "active_since",
                               "active_till", "maintenance_type"],
                })
            except ZabbixError:
                return []
        return self._cached("maints", 30, fetch)

    # ------------------------------------------------------------ 聚合视图
    @staticmethod
    def _interface_stats(items):
        """按接口名归并监控项，还原 LLD 发现的网络接口清单。"""
        grouped = {}
        for it in items:
            iface, metric = parse_iface_name(it.get("name", ""))
            if not iface:
                continue
            grouped.setdefault(iface, {})[metric] = it

        out = []
        for iface, metrics in sorted(grouped.items()):
            def val(metric):
                hit = metrics.get(metric)
                return (hit or {}).get("lastvalue", "")

            oper = val("Operational status")
            out.append({
                "name": iface,
                "oper_status": oper,
                "oper_text": OPER_STATUS.get(oper, "-"),
                "speed": val("Speed"),
                "bits_received": val("Bits received"),
                "bits_sent": val("Bits sent"),
                "in_errors": val("Inbound packets with errors"),
                "out_errors": val("Outbound packets with errors"),
            })
        return out

    def hosts(self):
        """主机列表 + 每台主机的接口、模板、告警、监控项统计。"""
        hosts = self.raw_hosts()
        items = self.raw_items()
        problems = self.raw_problems()
        triggers = self.raw_triggers()

        prob_by_host, trig_by_host = {}, {}
        trig_map = self._trigger_map()
        for p in problems:
            for hid in trig_map["hostids"].get(p.get("objectid"), []):
                prob_by_host.setdefault(hid, []).append(p)
        for t in triggers:
            for h in t.get("hosts", []):
                trig_by_host.setdefault(h["hostid"], []).append(t)

        stat = {}
        avail_hint = {}
        for it in items:
            # Zabbix 7 的接口 available 字段常停在「未知」，
            # 用内部监控项 zabbix[host,*,available] 兜底判断真实可用性
            if it.get("key_") in ("zabbix[host,snmp,available]",
                                  "zabbix[host,agent,available]",
                                  "zabbix[host,active_agent,available]"):
                if it.get("lastvalue") == "1":
                    avail_hint[it["hostid"]] = True
            s = stat.setdefault(it["hostid"],
                                {"total": 0, "enabled": 0, "unsupported": 0, "with_data": 0})
            s["total"] += 1
            if it.get("status") == "0":
                s["enabled"] += 1
            if it.get("state") == "1":
                s["unsupported"] += 1
            if it.get("lastclock") not in ("0", "", None):
                s["with_data"] += 1

        out = []
        for h in hosts:
            hid = h["hostid"]
            s = stat.get(hid, {"total": 0, "enabled": 0, "unsupported": 0, "with_data": 0})
            probs = prob_by_host.get(hid, [])
            worst = max([int(p.get("severity", 0)) for p in probs], default=-1)
            ifaces = []
            for i in h.get("interfaces", []):
                avail = i.get("available", "0")
                avail_text = AVAILABILITY.get(avail, "?")
                avail_key = AVAILABILITY_KEY.get(avail, "unknown")
                if avail == "0" and avail_hint.get(hid):
                    avail_text, avail_key = "可用", "ok"
                ifaces.append({
                    "ip": i.get("ip", ""),
                    "dns": i.get("dns", ""),
                    "port": i.get("port", ""),
                    "type": i.get("type", ""),
                    "type_text": IFACE_TYPE.get(i.get("type"), "?"),
                    "available": avail,
                    "available_text": avail_text,
                    "available_key": avail_key,
                    "error": i.get("error", ""),
                })
            out.append({
                "hostid": hid,
                "host": h["host"],
                "name": h.get("name") or h["host"],
                "status": h.get("status", "0"),
                "status_text": HOST_STATUS.get(h.get("status", "0"), "?"),
                "maintenance": h.get("maintenance_status", "0") == "1",
                "description": h.get("description", ""),
                "groups": [g["name"] for g in h.get("groups", [])],
                "templates": [t["name"] for t in h.get("parentTemplates", [])],
                "interfaces": ifaces,
                "items": s,
                "problems": len(probs),
                "triggers": len(trig_by_host.get(hid, [])),
                "worst_severity": worst,
                "worst_severity_text": SEVERITY.get(worst, "正常") if worst >= 0 else "正常",
                "worst_severity_key": SEVERITY_KEY.get(worst, "ok") if worst >= 0 else "ok",
                "health_key": "ok" if worst < 0 else SEVERITY_KEY.get(worst, "warn"),
            })
        out.sort(key=lambda x: (-x["worst_severity"], x["host"]))
        return out

    def overview(self):
        hosts = self.hosts()
        items = self.raw_items()
        problems = self.raw_problems()
        by_sev = {str(i): 0 for i in range(6)}
        for p in problems:
            key = str(p.get("severity", "0"))
            by_sev[key] = by_sev.get(key, 0) + 1
        proxies = self.raw_proxies()
        maints = self.raw_maintenances()
        return {
            "url": self.url,
            "web_problem_url": self.url + "/zabbix.php?action=problem.view",
            "web_host_url": self.url + "/zabbix.php?action=host.list",
            "version": self.version(),
            "hosts_total": len(hosts),
            "hosts_enabled": sum(1 for h in hosts if h["status"] == "0"),
            "hosts_disabled": sum(1 for h in hosts if h["status"] == "1"),
            "hosts_with_problems": sum(1 for h in hosts if h["problems"] > 0),
            "problems_total": len(problems),
            "problems_by_severity": by_sev,
            "items_total": len(items),
            "items_unsupported": sum(1 for i in items if i.get("state") == "1"),
            "items_with_data": sum(1 for i in items if i.get("lastclock") not in ("0", "", None)),
            "templates_total": len(self.raw_templates()),
            "discovery_rules_total": len(self.raw_discovery_rules()),
            "proxies": [{"name": p.get("name"),
                         "mode": "主动" if p.get("operating_mode") == "0" else "被动",
                         "hosts": len(p.get("hosts", []))} for p in proxies],
            "maintenances": [{"name": m.get("name"),
                              "till": m.get("active_till")} for m in maints],
        }

    def problems(self, host=None, limit=200):
        """活跃问题列表，可按主机名过滤。"""
        out = []
        trig_map = self._trigger_map()
        for p in self.raw_problems():
            names = trig_map["names"].get(p.get("objectid"), [])
            if host and host.lower() not in [n.lower() for n in names]:
                continue
            sev = int(p.get("severity", 0))
            out.append({
                "eventid": p.get("eventid"),
                "host": ", ".join(names) or "-",
                "hosts": names,
                "name": p.get("name", ""),
                "severity": sev,
                "severity_text": SEVERITY.get(sev, "?"),
                "severity_key": SEVERITY_KEY.get(sev, "na"),
                "clock": p.get("clock"),
                "age": human_age(p.get("clock")),
                "acknowledged": p.get("acknowledged") == "1",
                "tags": ["%s=%s" % (t.get("tag"), t.get("value", ""))
                         for t in p.get("tags", [])],
            })
        out.sort(key=lambda x: (-x["severity"], -int(x["clock"] or 0)))
        return out[:limit]

    def find_host(self, name):
        """按 host / name 精确匹配一台主机。"""
        if not name:
            return None
        low = name.lower()
        for h in self.hosts():
            if h["host"].lower() == low or h["name"].lower() == low:
                return h
        return None

    def host_detail(self, name):
        """单主机详情：监控项、不支持项、LLD 接口、告警。"""
        host = self.find_host(name)
        if not host:
            return None
        hid = host["hostid"]
        items = [i for i in self.raw_items() if i["hostid"] == hid]
        unsupported = [{
            "name": i.get("name"),
            "key": i.get("key_"),
            "error": (i.get("error") or "").strip() or "（无错误详情）",
        } for i in items if i.get("state") == "1"]
        samples = [{
            "name": i.get("name"),
            "key": i.get("key_"),
            "value": (i.get("lastvalue") or "").strip() or "-",
            "units": i.get("units", ""),
            "age": human_age(i.get("lastclock")) if i.get("lastclock") not in ("0", "", None) else "未采集",
        } for i in items if i.get("state") == "0"
            and i.get("lastclock") not in ("0", "", None)]
        samples.sort(key=lambda x: x["name"])

        return {
            "host": host,
            "items_total": len(items),
            "items_unsupported": len(unsupported),
            "unsupported": unsupported[:50],
            "samples": samples[:60],
            "network_interfaces": self._interface_stats(items),
            "problems": self.problems(host=host["host"]),
            "triggers": [{
                "description": t.get("description"),
                "priority": int(t.get("priority", 0)),
                "priority_text": SEVERITY.get(int(t.get("priority", 0)), "?"),
                "lastchange": human_age(t.get("lastchange")),
            } for t in self.raw_triggers()
                if any(x["hostid"] == hid for x in t.get("hosts", []))],
        }

    def templates(self):
        kw = ("cisco", "huawei", "h3c", "snmp", "switch", "router", "ios", "network")
        out = []
        for t in self.raw_templates():
            blob = (t.get("host", "") + " " + t.get("name", "")).lower()
            out.append({
                "host": t.get("host"),
                "name": t.get("name"),
                "relevant": any(k in blob for k in kw),
            })
        return out

    def snmp_interfaces(self):
        """所有主机上的 SNMP 接口，用于 SNMP 实测。"""
        out = []
        for h in self.hosts():
            for i in h["interfaces"]:
                if i["type"] == "2":
                    out.append({"host": h["host"], "ip": i["ip"], "port": i["port"],
                                "available_text": i["available_text"]})
        return out


_CLIENT = None


def get_client():
    """进程内共享的 Zabbix 客户端单例。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Zabbix()
    return _CLIENT


