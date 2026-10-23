# -*- coding: utf-8 -*-
"""SNMP 网络设备纳管（写操作层）。

职责边界：
  * zbx.py        —— 只管 JSON-RPC 传输与只读聚合
  * provision.py  —— 只管 SNMP 主机的「预检 / 生成方案 / 落库 / 回读验证」（本文件）
  * chat.py       —— 只管多轮对话与确认闸门

所有写操作都是幂等的：同一个 spec 重复执行结果一致，不会重复建组或重复绑模板。
"""
import re

from zbx import SEVERITY, ZabbixError

# ---------------------------------------------------------------- 默认值
DEFAULT_GROUP = "Network devices/Cisco"
DEFAULT_TEMPLATE = "Cisco IOS by SNMP"
DEFAULT_COMMUNITY = "public"
DEFAULT_PORT = "161"
DEFAULT_VERSION = "2"                     # SNMPv2c

# SNMP 接口可用性回读时容忍的等待时间（秒）
READBACK_WAIT = 8

IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
# 主机名：允许字母数字与 - _ . ，必须以字母或数字开头，避免把中文句子误吞进来
NAME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]{1,31})\b")


class ProvisionError(RuntimeError):
    """纳管流程中的可预期错误（会直接展示给用户）。"""


# ---------------------------------------------------------------- spec 工具
def normalize_spec(spec):
    """补齐默认值并做基础校验，返回新 dict。"""
    out = {
        "host": (spec.get("host") or "").strip(),
        "ip": (spec.get("ip") or "").strip(),
        "port": str(spec.get("port") or DEFAULT_PORT).strip() or DEFAULT_PORT,
        "community": (spec.get("community") or DEFAULT_COMMUNITY).strip() or DEFAULT_COMMUNITY,
        "group": (spec.get("group") or DEFAULT_GROUP).strip() or DEFAULT_GROUP,
        "template": (spec.get("template") or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE,
        "version": str(spec.get("version") or DEFAULT_VERSION),
        "bulk": str(spec.get("bulk") or "1"),
        "description": (spec.get("description") or "").strip(),
    }
    if out["host"] and not re.match(r"^[A-Za-z0-9._-]+$", out["host"]):
        raise ProvisionError("主机名只能包含字母、数字、点、下划线和短横线：%s" % out["host"])
    if out["ip"] and not _valid_ip(out["ip"]):
        raise ProvisionError("IP 地址不合法：%s" % out["ip"])
    if not out["port"].isdigit():
        raise ProvisionError("端口必须是数字：%s" % out["port"])
    return out


def _valid_ip(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


def missing_slots(spec):
    """还缺哪些必填槽位（按追问顺序返回）。"""
    lack = []
    if not spec.get("host"):
        lack.append("host")
    if not spec.get("ip"):
        lack.append("ip")
    return lack


def parse_spec(text, base=None):
    """从一句话里抽取主机名与 IP，补进已有的 spec。

    支持：'SW1 192.168.6.130'、'加台设备 192.168.6.130 叫 SW2'、
          '社区名用 secret'、'用 Cisco IOS by SNMP 模板'、'端口 1161'。
    """
    spec = dict(base or {})
    raw = text or ""

    ip = IP_RE.search(raw)
    if ip:
        spec["ip"] = ip.group(1)

    m = re.search(r"(?:端口|port)\s*([0-9]{1,5})", raw, re.I)
    if m:
        spec["port"] = m.group(1)

    m = re.search(r"(?:团体名?|community|community\s*string|只读串)\s*(?:用|是|为|改成|=|:)?\s*(\S+)", raw, re.I)
    if m:
        spec["community"] = m.group(1).strip("，。,.；;")

    m = re.search(r"(?:模板|template)\s*(?:用|是|为|改成|=|:)?\s*([A-Za-z0-9 ._\-]{3,})", raw, re.I)
    if m:
        spec["template"] = m.group(1).strip()

    m = re.search(r"(?:分组|主机组|group)\s*(?:用|是|为|改成|=|:)?\s*([A-Za-z0-9 /\u4e00-\u9fa5._\-]{2,})", raw, re.I)
    if m:
        spec["group"] = m.group(1).strip()

    if not spec.get("ip"):
        return spec

    # 主机名：优先取 IP 之外的第一个"像主机名"的英文词
    if not spec.get("host"):
        before = raw[:ip.start()] if ip else raw
        after = raw[ip.end():] if ip else ""
        for chunk in (after, before):
            for cand in NAME_RE.findall(chunk):
                low = cand.lower()
                if low in ("add", "ip", "snmp", "port", "host", "device", "node",
                           "community", "template", "group", "public", "v2c"):
                    continue
                spec["host"] = cand
                break
            if spec.get("host"):
                break
    return spec


# ---------------------------------------------------------------- 预检
def preflight(zbx, spec, probe=True, exclude_hostid=None):
    """落库前的两道检查：IP 是否被占用 + 设备侧 SNMP 是否真的通。

    exclude_hostid: 修改已有主机时传入该主机 hostid。否则改自己会被误判成
    "主机名已存在 + IP 被占用"两个冲突，逼用户每次都按强制确认——闸门一旦
    变成常态，就等于没有闸门。

    返回 {'ok': bool, 'conflicts': [...], 'probe': {...}|None, 'notes': [...]}
    """
    conflicts, notes = [], []

    host, ip = spec.get("host"), spec.get("ip")
    for h in zbx.hosts():
        if exclude_hostid and h["hostid"] == exclude_hostid:
            continue          # 改自己：自己的主机名和 IP 都是预期内的
        if host and h["host"].lower() == host.lower():
            conflicts.append({"kind": "host_exists", "text":
                              "主机名 **%s** 已存在（hostid=%s），将走「修改」流程"
                              % (h["host"], h["hostid"]), "hostid": h["hostid"]})
        for itf in h["interfaces"]:
            if ip and itf["ip"] == ip:
                conflicts.append({"kind": "ip_in_use", "text":
                                  "IP **%s** 已被主机 **%s** 的 %s 接口占用"
                                  % (ip, h["host"], itf["type_text"]), "hostid": h["hostid"]})

    result = {"ok": True, "conflicts": conflicts, "probe": None, "notes": notes}

    if probe and ip:
        import snmp_probe
        pr = snmp_probe.query(ip, spec.get("community") or DEFAULT_COMMUNITY,
                              port=int(spec.get("port") or DEFAULT_PORT))
        result["probe"] = pr
        if not pr["reachable"]:
            result["ok"] = False
            notes.append("SNMP 实测不通，先别落库：%s"
                         % (pr["results"][0]["error"] if pr["results"] else "无响应"))

    # IP 被别的设备占用是硬冲突，必须让用户知悉
    for c in conflicts:
        if c["kind"] == "ip_in_use":
            result["ok"] = False
    return result


def probe_summary(pr):
    """把 SNMP 实测结果压成一行摘要。"""
    if not pr:
        return ""
    if not pr["reachable"]:
        return "❌ SNMP 实测失败（%s）" % (pr["results"][0]["error"] if pr["results"] else "无响应")
    vals = {r["name"]: r["value"] for r in pr["results"] if r["ok"]}
    descr = (vals.get("sysDescr") or "").split("\n")[0].strip()
    name = vals.get("sysName") or "-"
    n_if = vals.get("ifNumber") or "-"
    return "✅ SNMP 实测通过 · sysName=%s · 接口数=%s · %s" % (name, n_if, descr[:70])


# ---------------------------------------------------------------- 资源解析
def resolve_group(zbx, name):
    """按名称找主机组，没有就建（幂等）。"""
    found = zbx.call("hostgroup.get", {"filter": {"name": [name]}, "output": ["groupid", "name"]})
    if found:
        return found[0]["groupid"], False
    created = zbx.call("hostgroup.create", {"name": name})
    return created["groupids"][0], True


def resolve_template(zbx, name):
    hit = zbx.call("template.get", {"filter": {"host": [name]},
                                    "output": ["templateid", "host", "name"]})
    if not hit:
        hit = zbx.call("template.get", {"filter": {"name": [name]},
                                        "output": ["templateid", "host", "name"]})
    if not hit:
        raise ProvisionError("Zabbix 中找不到模板「%s」" % name)
    return hit[0]


def snmp_interface_of(zbx, hostid):
    """取指定主机的 SNMP 接口（找不到返回 None）。"""
    ifs = zbx.call("hostinterface.get", {"hostids": [hostid], "output": "extend"})
    for itf in ifs:
        if itf.get("type") == "2":
            return itf
    return None


def interface_payload(spec):
    return {
        "type": 2,                      # SNMP
        "main": 1,
        "useip": 1,
        "ip": spec["ip"],
        "dns": "",
        "port": spec["port"],
        "details": {
            "version": spec.get("version") or DEFAULT_VERSION,
            "bulk": spec.get("bulk") or "1",
            "community": spec["community"],
        },
    }


def interface_details(spec):
    return {"version": spec.get("version") or DEFAULT_VERSION,
            "bulk": spec.get("bulk") or "1",
            "community": spec["community"]}


# ---------------------------------------------------------------- 落库
def apply(zbx, spec, mode="add", hostid=None):
    """执行纳管。mode='add' 建新主机，mode='update' 改已有主机。

    返回 {'hostid', 'mode', 'created_group', 'template', 'actions'}
    """
    spec = normalize_spec(spec)
    if missing_slots(spec):
        raise ProvisionError("信息不全，还缺：%s" % "、".join(missing_slots(spec)))

    actions = []
    groupid, created_group = resolve_group(zbx, spec["group"])
    if created_group:
        actions.append("新建主机组 %s" % spec["group"])

    tmpl = resolve_template(zbx, spec["template"])

    if mode == "update" and hostid:
        _update_existing(zbx, hostid, spec, groupid, tmpl, actions)
    else:
        hostid = _create_new(zbx, spec, groupid, tmpl, actions)

    # {$SNMP_COMMUNITY} 宏：模板里的监控项靠它取值
    zbx.call("host.update", {"hostid": hostid, "macros": [
        {"macro": "{$SNMP_COMMUNITY}", "value": spec["community"]}
    ]})
    actions.append("设置宏 {$SNMP_COMMUNITY}=%s" % spec["community"])

    zbx.invalidate()                     # 丢弃只读缓存，回读才是新数据
    return {"hostid": hostid, "mode": mode, "created_group": created_group,
            "group": spec["group"], "template": tmpl["host"], "actions": actions}


def _create_new(zbx, spec, groupid, tmpl, actions):
    payload = {
        "host": spec["host"],
        "name": spec["host"],
        "status": 0,
        "description": spec.get("description") or "SNMP 网络设备（对话式纳管）",
        "groups": [{"groupid": groupid}],
        "templates": [{"templateid": tmpl["templateid"]}],
        "interfaces": [interface_payload(spec)],
    }
    created = zbx.call("host.create", payload)
    hostid = created["hostids"][0]
    actions.append("创建主机 %s" % spec["host"])
    actions.append("添加 SNMP 接口 %s:%s（v2c/bulk/%s）"
                   % (spec["ip"], spec["port"], spec["community"]))
    actions.append("绑定模板 %s" % tmpl["host"])
    return hostid


def _update_existing(zbx, hostid, spec, groupid, tmpl, actions):
    """只改用户显式关心的三件事：IP/端口/团体名、模板、分组。"""
    # 1) 组：并入而不是替换，避免踢掉用户手工加过的组
    cur = zbx.call("host.get", {"hostids": [hostid], "selectGroups": ["groupid", "name"]})
    gids = {g["groupid"] for g in cur[0].get("groups", [])}
    if groupid not in gids:
        gids.add(groupid)
        zbx.call("host.update", {"hostid": hostid,
                                 "groups": [{"groupid": g} for g in sorted(gids)]})
        actions.append("加入主机组 %s" % spec["group"])
    else:
        actions.append("主机组 %s 已在其中，跳过" % spec["group"])

    # 2) 模板：追加，不替换整集
    zbx.call("host.update", {"hostid": hostid,
                             "templates": [{"templateid": tmpl["templateid"]}]})
    actions.append("确保绑定模板 %s" % tmpl["host"])

    # 3) SNMP 接口：改 IP/端口/团体名
    itf = snmp_interface_of(zbx, hostid)
    if itf:
        zbx.call("hostinterface.update", dict(
            {"interfaceid": itf["interfaceid"], "ip": spec["ip"], "port": spec["port"],
             "details": interface_details(spec)}))
        actions.append("更新 SNMP 接口 %s -> %s:%s"
                       % (itf.get("ip"), spec["ip"], spec["port"]))
    else:
        zbx.call("hostinterface.create", dict(
            {"hostid": hostid}, **interface_payload(spec)))
        actions.append("新增 SNMP 接口 %s:%s" % (spec["ip"], spec["port"]))

    zbx.call("host.update", {"hostid": hostid, "status": 0})
    actions.append("确保主机为启用状态")
    return hostid


# ---------------------------------------------------------------- 回读验证
def readback(zbx, host, hostid=None):
    """落库后真读一次 Zabbix，验证主机/接口/监控项/LLD 是否生效。"""
    hosts = zbx.call("host.get", {
        "filter": {"host": [host]},
        "output": ["hostid", "host", "name", "status"],
        "selectGroups": ["name"],
        "selectParentTemplates": ["name"],
        "selectInterfaces": ["ip", "port", "type", "available", "error"],
    })
    if not hosts:
        return {"found": False}
    h = hosts[0]
    hostid = hostid or h["hostid"]

    items = zbx.call("item.get", {
        "hostids": [hostid],
        "output": ["itemid", "name", "state", "lastvalue", "lastclock"],
        "limit": 20000,
    })
    supported = [i for i in items if i.get("state") == "0"]
    unsupported = [i for i in items if i.get("state") == "1"]
    with_data = [i for i in supported if i.get("lastclock") not in ("0", "", None)]
    ifaces = sorted({i["name"].split("()")[0].replace("Interface ", "").strip()
                     for i in supported if i["name"].startswith("Interface ")})

    problems = zbx.call("problem.get", {"hostids": [hostid],
                                        "output": ["eventid", "name", "severity"]})

    snmp = None
    for itf in h.get("interfaces", []):
        if itf.get("type") == "2":
            snmp = itf

    return {
        "found": True,
        "hostid": hostid,
        "host": h["host"],
        "status": h.get("status", "0"),
        "groups": [g["name"] for g in h.get("groups", [])],
        "templates": [t["name"] for t in h.get("parentTemplates", [])],
        "snmp": snmp,
        "items_total": len(items),
        "items_supported": len(supported),
        "items_unsupported": len(unsupported),
        "items_with_data": len(with_data),
        "interfaces_discovered": ifaces,
        "problems": [{"name": p["name"], "severity": int(p.get("severity", 0))}
                     for p in problems],
    }


def readback_lines(rb):
    """把回读结果整理成给用户看的文字。"""
    if not rb or not rb.get("found"):
        return "⚠️ 写入后回读不到这台主机，请到 Zabbix 页面确认。"
    lines = ["**%s**（hostid=%s）已就位。" % (rb["host"], rb["hostid"])]
    lines.append("- 主机组：%s" % ("、".join(rb["groups"]) or "-"))
    lines.append("- 模板：%s" % ("、".join(rb["templates"]) or "-"))
    if rb.get("snmp"):
        itf = rb["snmp"]
        state = {"0": "未知", "1": "可用", "2": "不可用"}.get(itf.get("available"), "?")
        lines.append("- SNMP 接口：%s:%s，Zabbix 侧可用性 = **%s**"
                     % (itf.get("ip"), itf.get("port"), state))
        if itf.get("error"):
            lines.append("- 接口报错：%s" % itf["error"])
    lines.append("- 监控项：共 %d，支持 %d，已采到数据 %d"
                 % (rb["items_total"], rb["items_supported"], rb["items_with_data"]))
    if rb["interfaces_discovered"]:
        lines.append("- LLD 已发现接口 %d 个：%s"
                     % (len(rb["interfaces_discovered"]),
                        "、".join(rb["interfaces_discovered"][:12])))
    else:
        lines.append("- LLD 尚未发现接口（Zabbix 首次采集约需 1 分钟，稍后问「%s 的接口状态」）"
                     % rb["host"])
    if rb["problems"]:
        lines.append("- 已有 %d 条活跃告警：%s" % (
            len(rb["problems"]),
            "；".join("%s[%s]" % (p["name"], SEVERITY.get(p["severity"], "?"))
                      for p in rb["problems"][:5])))
    else:
        lines.append("- 当前无活跃告警 ✅")
    return "\n".join(lines)
