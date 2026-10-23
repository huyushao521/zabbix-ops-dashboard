# -*- coding: utf-8 -*-
"""将 Cisco 设备纳管到 Zabbix 7（幂等）。

用法:
    python zabbix_add_host.py add      # 创建/更新主机、绑定模板、配置 SNMP 接口
    python zabbix_add_host.py check    # 查看该主机的监控项支持情况
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

HOST_NAME = "R1"
DEVICE_IP = "192.168.1.29"
SNMP_COMMUNITY = "public"
SNMP_PORT = "161"
TEMPLATE_HOST_NAME = "Cisco IOS by SNMP"
GROUP_PARENT = "Network devices"
GROUP_CHILD = "Network devices/Cisco"

_id = [0]


def load_env():
    env = {}
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    return env


ENV = load_env()
URL = ENV.get("ZABBIX_URL", "").rstrip("/") + "/api_jsonrpc.php"
TOKEN = ENV.get("ZABBIX_TOKEN", "")


def call(method, params=None, auth=True):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": _id[0]}
    headers = {"Content-Type": "application/json-rpc"}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    if "error" in body:
        raise RuntimeError(f"{method} -> {json.dumps(body['error'], ensure_ascii=False)}")
    return body["result"]


def ensure_group(name):
    found = call("hostgroup.get", {"filter": {"name": [name]}})
    if found:
        return found[0]["groupid"]
    created = call("hostgroup.create", {"name": name})
    return created["groupids"][0]


def snmp_interface():
    return {
        "type": 2,          # SNMP
        "main": 1,
        "useip": 1,
        "ip": DEVICE_IP,
        "dns": "",
        "port": SNMP_PORT,
        "details": {
            "version": "2",             # SNMPv2c
            "bulk": "1",
            "community": SNMP_COMMUNITY,
        },
    }


def cmd_add():
    print(f"API: {URL}")
    print(f"API 版本: {call('apiinfo.version', auth=False)}")

    tmpl = call("template.get", {"filter": {"host": [TEMPLATE_HOST_NAME]},
                                 "output": ["templateid", "host", "name"]})
    if not tmpl:
        raise RuntimeError(f"未找到模板 {TEMPLATE_HOST_NAME}")
    templateid = tmpl[0]["templateid"]
    print(f"模板: {tmpl[0]['host']} (templateid={templateid})")

    parent_gid = ensure_group(GROUP_PARENT)
    child_gid = ensure_group(GROUP_CHILD)
    print(f"主机组: {GROUP_PARENT}({parent_gid}) / {GROUP_CHILD}({child_gid})")

    existing = call("host.get", {"filter": {"host": [HOST_NAME]},
                                 "output": ["hostid", "host", "status"]})
    if existing:
        hostid = existing[0]["hostid"]
        print(f"主机 {HOST_NAME} 已存在 (hostid={hostid})，执行更新")
        call("host.update", {
            "hostid": hostid,
            "status": 0,
            "groups": [{"groupid": child_gid}],
            "templates": [{"templateid": templateid}],
        })
        # 接口单独维护，避免整体替换导致丢失已有配置
        ifs = call("hostinterface.get", {"hostids": [hostid], "output": "extend"})
        for itf in ifs:
            if itf["type"] == "2":
                call("hostinterface.update", {
                    "interfaceid": itf["interfaceid"],
                    "ip": DEVICE_IP,
                    "port": SNMP_PORT,
                    "details": {"version": "2", "bulk": "1", "community": SNMP_COMMUNITY},
                })
                print(f"已更新 SNMP 接口 {itf['interfaceid']} -> {DEVICE_IP}")
    else:
        created = call("host.create", {
            "host": HOST_NAME,
            "name": HOST_NAME,
            "status": 0,
            "description": "Cisco IOS-XE lab router on EVE-NG (IOL)",
            "groups": [{"groupid": child_gid}],
            "templates": [{"templateid": templateid}],
            "interfaces": [snmp_interface()],
            "macros": [],
        })
        hostid = created["hostids"][0]
        print(f"已创建主机 {HOST_NAME} (hostid={hostid})")

    call("host.update", {"hostid": hostid, "macros": [
        {"macro": "{$SNMP_COMMUNITY}", "value": SNMP_COMMUNITY}
    ]})
    print("已设置宏 {$SNMP_COMMUNITY}")
    print(f"\n完成。主机列表: {URL.replace('/api_jsonrpc.php', '')}/zabbix.php?action=host.list")
    return hostid


def cmd_check():
    hosts = call("host.get", {"filter": {"host": [HOST_NAME]}, "output": ["hostid", "host"]})
    if not hosts:
        print(f"未找到主机 {HOST_NAME}")
        return 1
    hostid = hosts[0]["hostid"]

    items = call("item.get", {
        "hostids": [hostid],
        "output": ["itemid", "name", "key_", "state", "error", "lastvalue", "lastclock"],
        "sortfield": "name",
    })
    enabled = [i for i in items if i["state"] == "0"]
    unsupported = [i for i in items if i["state"] == "1"]
    with_data = [i for i in enabled if i.get("lastclock", "0") != "0"]

    print(f"主机 {HOST_NAME}: 监控项 {len(items)} 个 | 支持 {len(enabled)} | 不支持 {len(unsupported)} | 已有数据 {len(with_data)}")
    if with_data:
        print("\n--- 已采集到数据的监控项（抽样）---")
        for it in with_data[:12]:
            print(f"  [OK] {it['name']} = {it['lastvalue']}")
    if unsupported:
        print("\n--- 不支持项（最多 10 条）---")
        for it in unsupported[:10]:
            print(f"  [!!] {it['name']} | {it['key_']} | {it['error'][:110]}")

    av = call("host.get", {"hostids": [hostid], "selectInterfaces": ["available", "error", "ip", "type"]})
    for itf in av[0]["interfaces"]:
        state = {"0": "unknown", "1": "available", "2": "unavailable"}.get(itf["available"], "?")
        print(f"\nSNMP 接口可用性: {state}  ip={itf['ip']}  err={itf['error'] or '-'}")

    ifaces = sorted({i["name"].split(":")[0] for i in items if i["name"].startswith("Interface ")})
    print(f"\nLLD 已发现的网络接口({len(ifaces)}): {', '.join(ifaces)}")

    triggers = call("trigger.get", {"hostids": [hostid], "output": ["triggerid", "description", "priority", "value"],
                                    "filter": {"value": 1}})
    print(f"\nR1 当前告警触发器: {len(triggers)}")
    for t in triggers:
        print(f"  [{t['priority']}] {t['description']}")

    problems = call("problem.get", {"hostids": [hostid], "output": ["eventid", "name", "severity", "clock"]})
    print(f"R1 当前活跃问题: {len(problems)}")
    for p in problems:
        print(f"  sev={p['severity']} {p['name']}")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "add"
    if action == "add":
        cmd_add()
    elif action == "check":
        sys.exit(cmd_check())
    else:
        print(__doc__)
