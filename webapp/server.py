# -*- coding: utf-8 -*-
"""Zabbix 运维看板 Web 服务（纯标准库，无需安装任何依赖）。

启动:
    python server.py [端口]        # 默认 8770

接口:
    GET  /api/overview        总览统计
    GET  /api/hosts           主机列表
    GET  /api/host?name=R1    单主机详情
    GET  /api/problems        活跃告警
    GET  /api/templates       模板列表
    GET  /api/snmp            SNMP 接口清单
    GET  /api/grafana         已生成的 Grafana 看板 + Grafana 地址
    POST /api/chat            对话（body: {message, session}）
    POST /api/snmp/probe      设备侧 SNMP 实测（body: {ip, community}）
    POST /api/grafana/sync    生成/刷新看板（body: {host} 省略则全量同步）
    POST /api/refresh         清缓存后重新拉取
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
sys.path.insert(0, HERE)

import chat          # noqa: E402
import gdash         # noqa: E402
import snmp_probe    # noqa: E402
import zbx           # noqa: E402
from zbx import ZabbixError   # noqa: E402

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ZabbixDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _fail(self, message, code=500):
        self._json({"ok": False, "error": message}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):
        """静默访问日志，保持控制台干净。"""

    def _static(self, rel):
        rel = (rel or "index.html").replace("\\", "/").lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            return self._fail("静态资源不存在: %s" % rel, 404)
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            self._send(200, fh.read(),
                       CONTENT_TYPES.get(ext, "application/octet-stream"))

    # ------------------------------------------------------------ GET
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        client = zbx.get_client()

        try:
            if route in ("/", "/index.html"):
                return self._static("index.html")
            if route.startswith("/static/"):
                return self._static(route[len("/static/"):])

            if route == "/api/overview":
                return self._json({"ok": True, "data": client.overview()})
            if route == "/api/hosts":
                return self._json({"ok": True, "data": client.hosts()})
            if route == "/api/problems":
                return self._json({"ok": True,
                                   "data": client.problems(host=query.get("host"))})
            if route == "/api/host":
                name = query.get("name", "")
                detail = client.host_detail(name)
                if not detail:
                    return self._fail("未找到主机 %s" % name, 404)
                return self._json({"ok": True, "data": detail})
            if route == "/api/templates":
                return self._json({"ok": True, "data": client.templates()})
            if route == "/api/snmp":
                return self._json({"ok": True, "data": client.snmp_interfaces()})
            if route == "/api/grafana":
                return self._json({"ok": True, "data": {
                    "url": gdash.grafana_url(),
                    "folder": gdash.FOLDER,
                    "datasource_uid": gdash.datasource_uid(),
                    "dashboards": gdash.existing(),
                }})
        except ZabbixError as exc:
            return self._fail(str(exc), 502)
        except Exception as exc:                       # noqa: BLE001
            return self._fail("服务端异常：%s" % exc, 500)

        return self._fail("未知路径 %s" % route, 404)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        route = urlparse(self.path).path
        payload = self._body()
        client = zbx.get_client()

        try:
            if route == "/api/chat":
                result = chat.answer(payload.get("message", ""),
                                     payload.get("session") or {}, client)
                return self._json({"ok": True, "data": result})

            if route == "/api/snmp/probe":
                ip = (payload.get("ip") or "").strip()
                if not ip:
                    return self._fail("缺少 ip 参数", 400)
                community = (payload.get("community") or "public").strip()
                return self._json({"ok": True,
                                   "data": snmp_probe.query(ip, community)})

            if route == "/api/grafana/sync":
                host = (payload.get("host") or "").strip()
                if host:
                    return self._json({"ok": True,
                                       "data": [chat.sync_dashboard(host)]})
                # 不带 host 就是全量同步，用于初次铺开或批量刷新
                return self._json({"ok": True, "data": [
                    chat.sync_dashboard(h["host"], allow_defer=False)
                    for h in client.hosts()]})

            if route == "/api/refresh":
                client.invalidate()
                return self._json({"ok": True, "data": client.overview()})
        except ZabbixError as exc:
            return self._fail(str(exc), 502)
        except Exception as exc:                       # noqa: BLE001
            return self._fail("服务端异常：%s" % exc, 500)

        return self._fail("未知路径 %s" % route, 404)


def _bind(port, host, tries=10):
    """端口被占用时自动向后顺延。"""
    last = None
    for offset in range(tries):
        try:
            return ThreadingHTTPServer((host, port + offset), DashboardHandler)
        except OSError as exc:
            last = exc
    raise last


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass

    host = os.environ.get("DASH_HOST", "127.0.0.1")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770

    client = zbx.get_client()
    print("=" * 58)
    print(" Zabbix 运维看板")
    print("=" * 58)
    print(" Zabbix API : %s" % (client.url or "(未配置 .env)"))
    try:
        print(" API 版本   : %s" % client.version())
        ov = client.overview()
        print(" 纳管主机   : %d 台   活跃告警: %d 条"
              % (ov["hosts_total"], ov["problems_total"]))
    except Exception as exc:                           # noqa: BLE001
        print(" [!] 无法连接 Zabbix: %s" % exc)

    httpd = _bind(port, host)
    real_port = httpd.server_address[1]
    print("-" * 58)
    print(" 看板地址   : http://%s:%d" % (host, real_port))
    print(" 停止服务   : Ctrl+C")
    print("=" * 58)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务 ...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
