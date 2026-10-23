# -*- coding: utf-8 -*-
"""Grafana 看板生成器（写操作层）。

职责边界：
  * zbx.py       —— Zabbix JSON-RPC 传输与只读聚合
  * provision.py —— SNMP 主机的预检 / 落库 / 回读验证
  * gdash.py     —— 依据主机「真实存在的监控项」生成 Grafana 看板（本文件）
  * chat.py      —— 多轮对话与确认闸门

为什么不用 Grafana API 建看板：
  Grafana 侧已开启 file provisioning（grafana/provisioning/dashboards/default.yml，
  updateIntervalSeconds=30）。把看板 JSON 写进 grafana/dashboards/ 即自动上线，
  不需要 Grafana 登录态、不需要 API Token，也不要求 Grafana 正在运行。

面板不写死：先读回主机的监控项，再按实际存在的指标决定画哪些图、
以及每个面板的 item 过滤器怎么写。所以换一台华为/华三设备同样能自动出图，
不会出现一排空白面板。
"""
import json
import os
import re
import sqlite3

from zbx import load_env

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

GRAFANA_DIR = os.path.join(ROOT, "grafana")
DASH_DIR = os.path.join(GRAFANA_DIR, "dashboards")
PROV_DIR = os.path.join(GRAFANA_DIR, "provisioning", "datasources")
DB_PATH = os.path.join(GRAFANA_DIR, "data", "grafana.db")

DEFAULT_GRAFANA_URL = "http://127.0.0.1:3000"
DS_TYPE = "alexanderzobnin-zabbix-datasource"
DS_NAME = "Zabbix"
FOLDER = "网络监控"
FILE_PREFIX = "zbx-"
PLUGIN_VERSION = "12.4.2"

# 'Interface Et0/0(): Bits received' 与 'Interface eth0: Bits received' 都能拆开
IFACE_RE = re.compile(r"^Interface\s+(.+?)(?:\(\))?\s*:\s*(.+)$")
ERR_RE = re.compile(r"packets?\s+(with errors|discarded)$", re.I)

# 单值指标目录：监控项名 -> (面板名, 单位, 特殊处理)
# 顺序即优先级：一行只放得下 6 个，排后面的会被 STAT_ROW_MAX 截掉。
STAT_CATALOG = [
    ("System name", "设备名称", None, None),
    ("Operating system", "系统版本", None, None),
    ("Uptime (network)", "运行时间", "s", None),
    ("Uptime", "运行时间", "s", None),
    ("SNMP agent availability", "SNMP 采集", None, "avail"),
    ("Zabbix agent availability", "Agent 采集", None, "avail"),
    ("ICMP loss", "ICMP 丢包率", "percent", None),
    ("ICMP response time", "ICMP 响应时间", "s", None),
    ("ICMP ping", "ICMP 可达", None, "avail"),
    ("Number of network interfaces", "接口数量", None, None),
    ("Hardware model name", "硬件型号", None, None),
    ("Device serial number", "序列号", None, None),
]

STAT_ROW_MAX = 6

AVAIL_MAP = [{"type": "value", "options": {
    "0": {"color": "red", "index": 1, "text": "DOWN"},
    "1": {"color": "green", "index": 0, "text": "UP"}}}]

UP_DOWN_MAP = [{"type": "value", "options": {
    "0": {"color": "text", "index": 2, "text": "UNKNOWN"},
    "1": {"color": "green", "index": 0, "text": "UP"},
    "2": {"color": "red", "index": 1, "text": "DOWN"}}}]

TS_CUSTOM = {
    "axisBorderShow": False, "axisCenteredZero": False, "axisColorMode": "text",
    "axisLabel": "", "axisPlacement": "auto", "barAlignment": 0, "barWidthFactor": 0.6,
    "drawStyle": "line", "fillOpacity": 10, "gradientMode": "none",
    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
    "insertNulls": False, "lineInterpolation": "smooth", "lineWidth": 2, "pointSize": 5,
    "scaleDistribution": {"type": "linear"}, "showPoints": "never", "spanNulls": True,
    "stacking": {"group": "A", "mode": "none"}, "thresholdsStyle": {"mode": "off"},
}


# ---------------------------------------------------------------- 环境与地址
def grafana_url():
    env = load_env()
    return (env.get("GRAFANA_URL") or DEFAULT_GRAFANA_URL).rstrip("/")


def slug(host):
    """Grafana 的 uid 只允许字母数字与 - _ ，主机名里的点要换掉。"""
    return re.sub(r"[^A-Za-z0-9_-]", "-", host or "host")[:34]


def dashboard_uid(host):
    return "zbx-" + slug(host)


def dashboard_path(host):
    return os.path.join(DASH_DIR, FILE_PREFIX + slug(host) + ".json")


def dashboard_url(host):
    return "%s/d/%s" % (grafana_url(), dashboard_uid(host))


def datasource_uid():
    """Zabbix 数据源的 uid：优先 provisioning YAML 的显式声明，其次 Grafana 库。

    grafana.py 生成的 YAML 没有写 uid，Grafana 会自己随机生成一个，
    所以必须回读而不能硬编码——否则换台机器、重装一次就全断。
    """
    if os.path.isdir(PROV_DIR):
        for fn in sorted(os.listdir(PROV_DIR)):
            if not fn.endswith((".yml", ".yaml")):
                continue
            try:
                with open(os.path.join(PROV_DIR, fn), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if DS_TYPE not in text:
                continue
            hit = re.search(r"^\s*uid:\s*([A-Za-z0-9_-]+)\s*$", text, re.M)
            if hit:
                return hit.group(1)
    try:
        uri = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        try:
            row = con.execute(
                "select uid from data_source where type = ? order by id limit 1",
                (DS_TYPE,)).fetchone()
            return row[0] if row and row[0] else None
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None


def pick_group(groups):
    """从主机所属分组里挑一个稳定的给看板用（优先 Network devices 前缀）。"""
    names = [g for g in (groups or []) if g]
    if not names:
        return ""
    for n in names:
        if n.startswith("Network devices"):
            return n
    return sorted(names, key=len)[-1]


# ---------------------------------------------------------------- 指标识别
def split_iface(item_name):
    """'Interface Et0/0(): Bits received' -> ('Et0/0', 'Bits received')"""
    hit = IFACE_RE.match(item_name or "")
    if not hit:
        return None, None
    iface, metric = hit.group(1).strip(), hit.group(2).strip()
    if not iface or not metric:
        return None, None
    return iface, metric


def collect_metrics(items):
    """按接口归并监控项，返回 (接口->指标集合, 全部指标名集合)。"""
    per_iface, metrics = {}, set()
    for it in items:
        iface, metric = split_iface(it.get("name", ""))
        if not iface:
            continue
        per_iface.setdefault(iface, {})[metric] = it
        metrics.add(metric)
    return per_iface, metrics


RX_SPECIAL = re.compile(r"([\\^$.|?*+()\[\]{}/])")


def _esc(text):
    """按 JS RegExp 的规则转义。

    不能用 re.escape：它会把空格也转成 '\\ '（Python 为 re.VERBOSE 保留），
    在 JS 里虽然多数情况能跑，但没必要留这种隐患。
    """
    return RX_SPECIAL.sub(r"\\\1", text)


def _rx(metrics):
    """把指标名拼成插件的正则过滤器 /Interface .*: (A|B)/ 。"""
    parts = sorted(_esc(m) for m in metrics)
    return "/Interface .*: (%s)/" % "|".join(parts)


# ---------------------------------------------------------------- 面板工厂
def _target(ds, group, host, item, ref="A"):
    return {
        "refId": ref, "datasource": ds, "queryType": "0", "mode": 0,
        "group": {"filter": group or ""}, "host": {"filter": host},
        "item": {"filter": item}, "application": {"filter": ""}, "functions": [],
        "options": {"count": False, "disableDataAlignment": False,
                    "showDisabledItems": False, "skipEmptyValues": False,
                    "useTrends": "default", "useZabbixValueMapping": False},
        "resultFormat": "time_series", "schema": 12, "evaltype": "0", "textFilter": "",
        "trigger": {"filter": ""}, "macro": {"filter": ""}, "proxy": {"filter": ""},
        "table": {"skipEmptyValues": False}, "tags": {"filter": ""},
        "countTriggers": False, "countTriggersBy": "",
    }


def _stat(pid, title, ds, targets, unit=None, mappings=None):
    return {
        "id": pid, "type": "stat", "title": title, "datasource": ds, "gridPos": {},
        "targets": targets,
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"}, "mappings": mappings or [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            "unit": unit,
        }, "overrides": []},
        "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto",
                    "orientation": "auto", "percentChangeColorMode": "standard",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "showPercentChange": False, "textMode": "auto", "wideLayout": True},
        "pluginVersion": PLUGIN_VERSION,
    }


def _timeseries(pid, title, ds, targets, unit, overrides=None, fill=10):
    custom = dict(TS_CUSTOM)
    custom["fillOpacity"] = fill
    return {
        "id": pid, "type": "timeseries", "title": title, "datasource": ds,
        "gridPos": {}, "targets": targets,
        "fieldConfig": {"defaults": {
            "color": {"mode": "palette-classic"}, "custom": custom, "mappings": [],
            "thresholds": {"mode": "absolute",
                           "steps": [{"color": "green", "value": None}]},
            "unit": unit,
        }, "overrides": overrides or []},
        "options": {"legend": {"calcs": ["lastNotNull", "max"], "displayMode": "table",
                               "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "pluginVersion": PLUGIN_VERSION,
    }


def _problems_panel(pid, ds, group, host, y):
    """告警面板的 options 必须带完整默认值，缺字段会渲染成空白。"""
    severity = [
        {"priority": i, "severity": s, "color": c, "show": True}
        for i, (s, c) in enumerate([
            ("Not classified", "rgb(108, 108, 108)"), ("Information", "rgb(120, 158, 183)"),
            ("Warning", "rgb(175, 180, 36)"), ("Average", "rgb(255, 137, 30)"),
            ("High", "rgb(255, 101, 72)"), ("Disaster", "rgb(215, 0, 0)")])
    ]
    return {
        "id": pid, "type": "alexanderzobnin-zabbix-triggers-panel",
        "title": "活跃告警", "datasource": ds,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
        "targets": [{"group": {"filter": group or ""}, "host": {"filter": host},
                     "application": {"filter": ""}, "trigger": {"filter": ""},
                     "tags": {"filter": ""}, "proxy": {"filter": ""},
                     "datasource": DS_NAME}],
        "options": {
            "schemaVersion": 8, "hostField": True, "hostTechNameField": False,
            "hostIpField": False, "hostProxy": False, "hostGroups": False,
            "showTags": True, "statusField": True, "statusIcon": False,
            "severityField": True, "ackField": True, "ageField": False,
            "opdataField": False, "descriptionField": True, "descriptionAtNewLine": False,
            "showDatasourceName": False, "sortProblems": "lastchange", "limit": 100,
            "layout": "table", "fontSize": "100%", "pageSize": 10,
            "showSearchFilter": False, "problemTimeline": True,
            "highlightBackground": False, "highlightNewEvents": False,
            "highlightNewerThan": "1h", "customLastChangeFormat": False,
            "lastChangeFormat": "", "resizedColumns": [], "allowDangerousHTML": False,
            "triggerSeverity": severity, "okEventColor": "rgb(56, 189, 113)",
            "ackEventColor": "rgb(56, 219, 156)", "markAckEvents": False,
        },
        "pluginVersion": "6.9.1",
    }


# ---------------------------------------------------------------- 组装
def build(host, group, items, ds_uid):
    """按主机真实监控项组装看板。返回 (看板 dict, 接口名列表)。"""
    ds = {"type": DS_TYPE, "uid": ds_uid} if ds_uid else {"type": DS_TYPE, "uid": None}
    per_iface, metrics = collect_metrics(items)
    names = {it.get("name") for it in items}
    panels, counter, y = [], [0], 0

    def nid():
        counter[0] += 1
        return counter[0]

    def row(row_panels, height):
        """每行最多 6 格（最小宽度 4），超出的换行，行内均分 24 格。

        必须限 6：24 // 7 == 3，被 max(...,4) 拉回 4 之后第 7 格会落到 x=24，
        直接掉出栅格。
        """
        nonlocal y
        if not row_panels:
            return
        for start in range(0, len(row_panels), 6):
            chunk = row_panels[start:start + 6]
            width = max(4, 24 // len(chunk))
            for idx, panel in enumerate(chunk):
                panel["gridPos"] = {"h": height, "w": width, "x": idx * width, "y": y}
                panels.append(panel)
            y += height

    # ---- 第 1 行：单值指标（只画真实存在的） ----
    stats = []
    for item_name, label, unit, special in STAT_CATALOG:
        if item_name not in names:
            continue
        # 'Uptime (network)' 与 'Uptime' 同一块面板，先到先得
        if label in [p["title"] for p in stats]:
            continue
        stats.append(_stat(nid(), label, ds, [_target(ds, group, host, item_name)],
                           unit=unit,
                           mappings=AVAIL_MAP if special == "avail" else None))
    row(stats[:STAT_ROW_MAX], 4)

    # ---- 接口流量 ----
    traffic = {m for m in metrics if m.lower().startswith("bits ")}
    if traffic:
        row([_timeseries(nid(), "接口流量（Bits received / sent）", ds,
                         [_target(ds, group, host, _rx(traffic))], "bps",
                         overrides=[{"matcher": {"id": "byRegexp", "options": "/received/i"},
                                     "properties": [{"id": "custom.transform",
                                                     "value": "negative-Y"}]}],
                         fill=25)], 9)

    # ---- 错包 / 丢包 ----
    errors = {m for m in metrics if ERR_RE.search(m)}
    # ---- ICMP 趋势：只画响应时间。
    # 丢包率单位是 %，和秒级延迟共用一个 Y 轴会互相压扁，它已经在上面出卡片了。
    icmp_panel = None
    if "ICMP response time" in names:
        icmp_panel = _timeseries(nid(), "ICMP 响应时间趋势", ds,
                                 [_target(ds, group, host, "ICMP response time")], "s")
    row(([_timeseries(nid(), "接口错包 / 丢包", ds,
                      [_target(ds, group, host, _rx(errors))], "short")]
         if errors else []) + ([icmp_panel] if icmp_panel else []), 8)

    status = {m for m in metrics if m.lower() == "operational status"}
    speed = {m for m in metrics
             if m.lower() in ("speed", "interface speed", "if speed")}
    tail = []
    if status:
        tail.append(_stat(nid(), "接口 Operational 状态", ds,
                          [_target(ds, group, host, _rx(status))],
                          mappings=UP_DOWN_MAP))
    if speed:
        tail.append(_stat(nid(), "接口速率", ds,
                          [_target(ds, group, host, _rx(speed))], unit="bps"))
    row(tail, 8)

    panels.append(_problems_panel(nid(), ds, group, host, y))

    if per_iface:
        layout = "接口与链路"
    elif panels:
        layout = "设备概览"
    else:
        layout = "监控项为空"
    dash = {
        "annotations": {"list": [{
            "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard"}]},
        "description": ("由 webapp/gdash.py 依据 Zabbix 实际监控项自动生成，"
                        "请勿在 Grafana 界面手工修改（下次同步会被覆盖）。"
                        "分组 %s，接口 %d 个。" % (group or "-", len(per_iface))),
        "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1,
        "id": None, "links": [], "panels": panels, "refresh": "30s",
        "schemaVersion": 40, "tags": ["zabbix", "自动生成", slug(host)],
        "templating": {"list": []},
        "time": {"from": "now-6h", "to": "now"}, "timepicker": {},
        "timezone": "browser", "title": "%s · %s" % (host, layout),
        "uid": dashboard_uid(host), "version": 1, "weekStart": "",
    }
    return dash, sorted(per_iface)


# ---------------------------------------------------------------- 对外动作
def write(dash, host):
    """原子写入 grafana/dashboards/，Grafana 30 秒内自动装载。"""
    os.makedirs(DASH_DIR, exist_ok=True)
    path = dashboard_path(host)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dash, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def sync(client, host):
    """把一台主机的看板同步到 Grafana。返回结构化结果，不抛异常。"""
    info = {"host": host, "ok": False, "url": dashboard_url(host),
            "uid": dashboard_uid(host)}
    try:
        detail = client.find_host(host)
        if not detail:
            info["error"] = "Zabbix 里没有主机 %s" % host
            return info
        hostid = detail.get("hostid")
        # 只统计启用且状态正常的监控项：不支持项画出来必然是空面板
        items = [i for i in client.raw_items()
                 if i.get("hostid") == hostid
                 and str(i.get("status")) == "0" and str(i.get("state")) == "0"]
        if not items:
            info["error"] = "%s 还没有监控项（模板刚挂上时 LLD 需要 1 分钟左右）" % host
            return info
        group = pick_group(detail.get("groups") or [])
        ds_uid = datasource_uid()
        dash, ifaces = build(host, group, items, ds_uid)
        path = write(dash, host)
        info.update({
            "ok": True, "path": path, "group": group, "datasource_uid": ds_uid,
            "items": len(items), "interfaces": ifaces,
            "panels": [p["title"] for p in dash["panels"]],
            "show_url": dashboard_url(host),
        })
    except Exception as exc:                            # noqa: BLE001
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def remove(host):
    """删除看板文件（Grafana 会随之移除看板）。"""
    path = dashboard_path(host)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def existing():
    """列出已生成的看板：[{host, uid, file, url}]。

    主机名从标题里取（标题格式 '主机 · 布局' 由 build() 保证），
    比反推文件名可靠——文件名里的点是被替换过的。
    """
    if not os.path.isdir(DASH_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(DASH_DIR)):
        if not (fn.startswith(FILE_PREFIX) and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(DASH_DIR, fn), encoding="utf-8") as fh:
                dash = json.load(fh)
        except (OSError, ValueError):
            continue
        uid = dash.get("uid") or fn[:-5]
        host = str(dash.get("title", "")).split(" \u00b7 ")[0] or fn
        out.append({"host": host, "uid": uid, "file": fn,
                    "url": "%s/d/%s" % (grafana_url(), uid)})
    return out


def main():
    sys_argv = __import__("sys").argv[1:]
    import zbx
    client = zbx.get_client()
    hosts = sys_argv or [h["host"] for h in client.hosts()]
    print("Grafana 数据源 uid : %s" % (datasource_uid() or "(未找到，将用默认数据源)"))
    print("看板目录           : %s" % DASH_DIR)
    print("-" * 62)
    ok = 0
    for host in hosts:
        r = sync(client, host)
        if r["ok"]:
            ok += 1
            print("[OK] %-12s 面板 %d 个  接口 %d 个  %s"
                  % (host, len(r["panels"]), len(r["interfaces"]), r["show_url"]))
        else:
            print("[!!] %-12s %s" % (host, r.get("error")))
    print("-" * 62)
    print("完成：%d/%d 台" % (ok, len(hosts)))


if __name__ == "__main__":
    main()
