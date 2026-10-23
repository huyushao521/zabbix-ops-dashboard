# -*- coding: utf-8 -*-
"""设备侧 SNMPv2c 实测：直接复用工作区的 snmp_get.py（纯标准库实现）。

用途：Zabbix 之外再做一层独立校验，确认设备 SNMP 真实可达、
community 正确，排除 Zabbix 自身采集链路的问题。
"""
import importlib.util
import os
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 常用标量 OID
OIDS = [
    ("1.3.6.1.2.1.1.1.0", "sysDescr", "系统描述"),
    ("1.3.6.1.2.1.1.3.0", "sysUpTime", "运行时长"),
    ("1.3.6.1.2.1.1.5.0", "sysName", "设备名称"),
    ("1.3.6.1.2.1.2.1.0", "ifNumber", "接口数量"),
]

_LIB = None


def _lib():
    """延迟加载工作区的 snmp_get.py，复用它的报文编解码。"""
    global _LIB
    if _LIB is None:
        path = os.path.join(ROOT, "snmp_get.py")
        spec = importlib.util.spec_from_file_location("snmp_get_lib", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIB = mod
    return _LIB


def _read_tlv(data, pos):
    """读取一个 BER TLV，返回 (tag, body, next_pos)。"""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[pos:pos + nbytes], "big")
        pos += nbytes
    return tag, data[pos:pos + length], pos + length


def _decode_value(tag, body):
    """把 varbind 的取值解码成可读文本。"""
    if tag == 0x02:                                   # INTEGER
        return str(int.from_bytes(body, "big", signed=bool(body and body[0] & 0x80)))
    if tag == 0x04:                                   # OCTET STRING
        return body.decode("utf-8", "replace").strip()
    if tag == 0x05:                                   # NULL
        return "(空值)"
    if tag == 0x06:                                   # OID
        if not body:
            return ""
        parts = [str(body[0] // 40), str(body[0] % 40)]
        cur = 0
        for byte in body[1:]:
            cur = (cur << 7) | (byte & 0x7F)
            if not byte & 0x80:
                parts.append(str(cur))
                cur = 0
        return ".".join(parts)
    if tag in (0x41, 0x42, 0x43, 0x46):               # Counter32/Gauge32/TimeTicks/Counter64
        return str(int.from_bytes(body, "big"))
    if tag == 0x40:                                   # IpAddress
        return ".".join(str(b) for b in body)
    return body.hex()


def _parse_response(data):
    """解析 GetResponse，返回 (error_status, 值文本, 取值标签)。"""
    _, outer, _ = _read_tlv(data, 0)                  # 外层 SEQUENCE
    pos = 0
    _, _, pos = _read_tlv(outer, pos)                 # version
    _, _, pos = _read_tlv(outer, pos)                 # community
    _, pdu, _ = _read_tlv(outer, pos)                 # PDU
    p = 0
    _, _, p = _read_tlv(pdu, p)                       # request-id
    _, err_body, p = _read_tlv(pdu, p)                # error-status
    _, _, p = _read_tlv(pdu, p)                       # error-index
    _, vbl, _ = _read_tlv(pdu, p)                     # varbind list
    _, varbind, _ = _read_tlv(vbl, 0)                 # 第一个 varbind
    v = 0
    _, _, v = _read_tlv(varbind, v)                   # OID
    vtag, vbody, _ = _read_tlv(varbind, v)            # 取值
    return int.from_bytes(err_body, "big"), _decode_value(vtag, vbody), vtag


def _duration(ticks):
    """sysUpTime 的 TimeTicks（百分之一秒）转可读时长。"""
    total = int(ticks) // 100
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return "%d 天 %02d:%02d:%02d" % (days, hours, minutes, seconds)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def query(host, community="public", timeout=4, port=161):
    """对单台设备做一组标量 OID 的 SNMP GET。"""
    lib = _lib()
    results = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for oid, name, label in OIDS:
            entry = {"oid": oid, "name": name, "label": label, "ok": False,
                     "value": "", "error": ""}
            try:
                sock.sendto(lib.build_get(community, oid), (host, port))
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                entry["error"] = "超时：community 错误 / 未启用 SNMP / UDP %d 不通" % port
                results.append(entry)
                continue
            except OSError as exc:
                entry["error"] = str(exc)
                results.append(entry)
                continue
            try:
                err_status, value, vtag = _parse_response(data)
            except (IndexError, ValueError) as exc:
                entry["error"] = "响应解析失败：%s" % exc
                results.append(entry)
                continue

            if err_status:
                entry["error"] = "设备返回 SNMP 错误状态 %d（该 OID 不存在或无权限）" % err_status
                results.append(entry)
                continue

            if oid == "1.3.6.1.2.1.1.3.0" and vtag == 0x43:
                value = "%s（%s 个时间刻度）" % (_duration(value), value)

            entry["ok"] = True
            entry["value"] = value
            results.append(entry)
    finally:
        sock.close()

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "host": host,
        "port": port,
        "community": community,
        "reachable": ok_count > 0,
        "ok_count": ok_count,
        "total": len(results),
        "results": results,
    }
