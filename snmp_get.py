# -*- coding: utf-8 -*-
"""极简 SNMPv2c GET 工具（纯标准库，不依赖第三方包）。

用法:
    python snmp_get.py <host> <community> <oid> [oid2 ...]
"""
import socket
import sys


def tlv(tag, value):
    length = len(value)
    if length < 0x80:
        return bytes([tag, length]) + value
    enc = []
    while length:
        enc.insert(0, length & 0xFF)
        length >>= 8
    return bytes([tag, 0x80 | len(enc)]) + bytes(enc) + value


def int_tlv(value):
    if value == 0:
        return tlv(0x02, b"\x00")
    buf = []
    while value:
        buf.insert(0, value & 0xFF)
        value >>= 8
    if buf[0] & 0x80:
        buf.insert(0, 0)
    return tlv(0x02, bytes(buf))


def oid_tlv(oid):
    parts = [int(x) for x in oid.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            body += bytes([p])
        else:
            enc = [p & 0x7F]
            p >>= 7
            while p:
                enc.insert(0, (p & 0x7F) | 0x80)
                p >>= 7
            body += bytes(enc)
    return tlv(0x06, body)


def build_get(community, oid, reqid=0x1234):
    varbind = tlv(0x30, oid_tlv(oid) + tlv(0x05, b""))
    pdu = tlv(0xA0, int_tlv(reqid) + int_tlv(0) + int_tlv(0) + tlv(0x30, varbind))
    return tlv(0x30, int_tlv(1) + tlv(0x04, community.encode()) + pdu)


def printable_runs(data):
    out, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= 2:
                out.append(cur.decode("ascii"))
            cur = bytearray()
    if len(cur) >= 2:
        out.append(cur.decode("ascii"))
    return out


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    host, community = sys.argv[1], sys.argv[2]
    oids = sys.argv[3:]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)

    rc = 0
    for oid in oids:
        try:
            sock.sendto(build_get(community, oid), (host, 161))
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            print(f"[TIMEOUT] {host} {oid}  -> 无响应（community 错误 / 未启用 SNMP / UDP161 不通）")
            rc = 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {host} {oid}  -> {exc}")
            rc = 1
            continue

        err_status = None
        # 粗略判定 error-status 字段：响应第 3 个 INTEGER
        ints = []
        i = 0
        while i < len(data) - 2:
            if data[i] == 0x02 and data[i + 1] <= 4:
                n = data[i + 1]
                ints.append(int.from_bytes(data[i + 2:i + 2 + n], "big"))
                i += 2 + n
            else:
                i += 1
        if len(ints) >= 3:
            err_status = ints[2]

        runs = printable_runs(data)
        val = runs[-1] if runs else ""
        if err_status:
            print(f"[SNMP-ERROR status={err_status}] {oid}")
            rc = 1
        else:
            print(f"[OK] {oid} = {val}")
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
