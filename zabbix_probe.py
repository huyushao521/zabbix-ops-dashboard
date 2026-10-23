# -*- coding: utf-8 -*-
"""Zabbix 7 API 连通性探测 + 全量信息采集（只读）"""
import json
import os
import sys
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def load_env():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()


load_env()
URL = os.environ.get("ZABBIX_URL", "").rstrip("/")
TOKEN = os.environ.get("ZABBIX_TOKEN", "")
API = f"{URL}/api_jsonrpc.php"

_id = [0]


def call(method, params=None, use_header=True, no_auth=False):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": _id[0]}
    headers = {"Content-Type": "application/json-rpc"}
    if no_auth:
        pass
    elif not use_header:
        payload["auth"] = TOKEN
    else:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"), headers=headers
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(f"{method} -> {body['error'].get('code')} {body['error'].get('message')} :: {body['error'].get('data')}")
    return body.get("result")


def main():
    out = {}

    # 1) 版本 + 鉴权方式确认
    out["api_version"] = call("apiinfo.version", no_auth=True)
    auth_mode = "Authorization: Bearer"
    try:
        me = call("user.checkAuthentication", {"extend": True})
        out["auth_via_header_ok"] = True
        out["current_user"] = {
            "userid": me.get("userid"),
            "username": me.get("username"),
            "name": me.get("name"),
            "surname": me.get("surname"),
        }
        out["current_user_type"] = me.get("type", me.get("roleid"))
    except Exception as e:
        out["auth_via_header_ok"] = False
        out["auth_header_error"] = str(e)
        auth_mode = "body auth (legacy)"

    out["auth_mode"] = auth_mode

    # 2) 主机
    hosts = call(
        "host.get",
        {
            "output": ["hostid", "host", "name", "status", "available", "active_available", "maintenance_status"],
            "selectGroups": ["groupid", "name"],
            "selectInterfaces": ["interfaceid", "ip", "dns", "port", "type", "available", "error"],
            "selectParentTemplates": ["templateid", "name"],
            "sortfield": "host",
        },
    )
    out["hosts_count"] = len(hosts)
    out["hosts"] = hosts

    # 3) 主机组
    groups = call("hostgroup.get", {"output": ["groupid", "name"], "selectHosts": ["hostid"]})
    out["hostgroups"] = [{"name": g["name"], "hosts": len(g.get("hosts", []))} for g in groups]

    # 4) 活跃问题
    problems = call(
        "problem.get",
        {
            "output": "extend",
            "recent": True,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": 200,
            "selectTags": "extend",
            "selectAcknowledges": "extend",
        },
    )
    out["problems_count"] = len(problems)
    out["problems"] = problems

    # 5) 触发告警的触发器 -> 关联主机
    triggers = call(
        "trigger.get",
        {
            "output": ["triggerid", "description", "priority", "value", "lastchange", "status"],
            "only_true": True,
            "monitored": True,
            "selectHosts": ["hostid", "host", "name"],
            "sortfield": "priority",
            "sortorder": "DESC",
            "limit": 200,
        },
    )
    out["active_triggers_count"] = len(triggers)
    out["active_triggers"] = triggers

    # 6) 监控项统计
    items = call("item.get", {"output": ["itemid", "name", "key_", "state", "status"], "monitored": True, "countOutput": True})
    out["items_monitored_count"] = items
    items_ns = call("item.get", {"output": ["itemid"], "filter": {"state": 1}, "countOutput": True})
    out["items_unsupported_count"] = items_ns

    # 7) 模板（含 Cisco / 华为相关）
    templates = call("template.get", {"output": ["templateid", "host", "name"], "sortfield": "host"})
    out["templates_count"] = len(templates)
    kw = ("cisco", "huawei", "snmp", "switch", "router", "ios")
    out["templates_relevant"] = [t for t in templates if any(k in t["host"].lower() or k in t["name"].lower() for k in kw)]

    # 8) 自动发现规则
    drules = call("discoveryrule.get", {"output": ["itemid", "name", "key_", "status", "state"]})
    out["discovery_rules_count"] = len(drules)

    # 9) Proxy
    try:
        proxies = call("proxy.get", {"output": ["proxyid", "name", "operating_mode", "lastaccess"], "selectHosts": ["hostid"]})
        out["proxies"] = proxies
    except Exception as e:
        out["proxies_error"] = str(e)

    # 10) 未监控/不可用主机
    out["hosts_unavailable"] = [
        {"host": h["host"], "available": h.get("available"), "interfaces": h.get("interfaces")}
        for h in hosts
        if h.get("status") == "0" and h.get("available") not in ("1", None)
    ]

    # 11) 维护
    try:
        maint = call("maintenance.get", {"output": ["maintenanceid", "name", "active_since", "active_till"]})
        out["maintenances"] = maint
    except Exception as e:
        out["maintenances_error"] = str(e)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"fatal_error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)
