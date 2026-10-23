# -*- coding: utf-8 -*-
"""通过 telnet 连接 Cisco 设备控制台，执行命令并回显结果。

用法:
    python cisco_telnet.py <host> <port> [--enable] "<cmd1>" "<cmd2>" ...

默认以只读方式执行传入的命令；--enable 会在需要时尝试进入特权模式。
"""
import re
import sys
import time
import telnetlib

PROMPT = re.compile(rb"[\r\n]([A-Za-z0-9_\-\.\(\)/:]{1,60})[>#]\s*$")


def read_until_prompt(tn, timeout=20):
    try:
        idx, match, data = tn.expect([PROMPT], timeout=timeout)
        return data.decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        return f"<read error: {exc}>"


def main():
    argv = sys.argv[1:]
    if len(argv) < 3:
        print(__doc__)
        return 2

    host, port = argv[0], int(argv[1])
    rest = argv[2:]
    do_enable = False
    if rest and rest[0] == "--enable":
        do_enable = True
        rest = rest[1:]
    commands = rest

    tn = telnetlib.Telnet(host, port, timeout=20)
    log = []

    banner = tn.read_until(b"#", timeout=5) if False else b""
    time.sleep(1.5)
    try:
        banner = tn.read_very_eager()
    except Exception:  # noqa: BLE001
        banner = b""
    log.append("[banner] " + banner.decode("utf-8", "ignore"))

    # 无账密场景：遇到用户名/密码提示直接回车
    if b"Username:" in banner or b"Login:" in banner:
        tn.write(b"\r\n")
        time.sleep(1)
    if b"Password:" in banner:
        tn.write(b"\r\n")
        time.sleep(1)

    tn.write(b"\r\n")
    out = read_until_prompt(tn)
    log.append("[initial prompt] " + out)

    if do_enable:
        tn.write(b"enable\r\n")
        out = read_until_prompt(tn)
        log.append("[enable] " + out)
        if "Password" in out or "password" in out:
            tn.write(b"\r\n")
            out = read_until_prompt(tn)
            log.append("[enable blank pw] " + out)

    # 关闭分页，保证长输出一次性返回
    tn.write(b"terminal length 0\r\n")
    read_until_prompt(tn)

    for cmd in commands:
        tn.write(cmd.encode() + b"\r\n")
        out = read_until_prompt(tn, timeout=30)
        log.append(f"\n===== $ {cmd} =====\n{out}")

    tn.write(b"exit\r\n")
    time.sleep(0.5)
    try:
        tn.close()
    except Exception:  # noqa: BLE001
        pass

    print("".join(log))


if __name__ == "__main__":
    sys.exit(main() or 0)
