# -*- coding: utf-8 -*-
"""在本机拉起一套 Grafana + Zabbix 插件的监控看板（纯标准库，不需要 Docker）。

用法:
    python grafana/grafana.py setup     # 准备 Grafana 与 Zabbix 插件、生成配置
    python grafana/grafana.py start     # 启动 Grafana
    python grafana/grafana.py stop      # 停止 Grafana
    python grafana/grafana.py status    # 查看运行状态

下载物、数据、插件都在 grafana/ 下，删掉整个 grafana 目录即可完全卸载。

如果自动下载太慢，可以手动下载后放进 grafana/dist/，脚本会自动识别：
    Grafana     -> grafana/dist/grafana-12.4.2-windows-amd64.tar.gz
    Zabbix 插件 -> grafana/dist/alexanderzobnin-zabbix-app-6.9.1.zip
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GDIR = os.path.join(ROOT, "grafana")
DIST = os.path.join(GDIR, "dist")
PROV = os.path.join(GDIR, "provisioning")
DASH = os.path.join(GDIR, "dashboards")

DATA = os.path.join(GDIR, "data")
LOGS = os.path.join(GDIR, "logs")
PLUGINS = os.path.join(DATA, "plugins")
PIDFILE = os.path.join(GDIR, ".grafana.pid")

GRAFANA_VERSION = "12.4.2"
# 官方 CDN 对大文件限速严重（实测 0.09 MB/s，451 MB 要 1.4 小时），
# 因此优先走华为云镜像（实测 8 MB/s，约 35 秒），官方 OSS 源兜底。
# 注意：华为云只提供 Enterprise 包的 Windows 二进制，不加载 License 时功能与 OSS 一致。
GRAFANA_URLS = [
    "https://mirrors.huaweicloud.com/grafana/12.4.2/"
    "grafana-enterprise_12.4.2_23531306697_windows_amd64.tar.gz",
    "https://dl.grafana.com/oss/release/grafana-12.4.2.windows-amd64.zip",
]

PLUGIN_ID = "alexanderzobnin-zabbix-app"
PLUGIN_VERSION = "6.9.1"
PLUGIN_ZIP = os.path.join(DIST, "%s-%s.zip" % (PLUGIN_ID, PLUGIN_VERSION))
PLUGIN_URLS = [
    "https://grafana.com/api/plugins/%s/versions/%s/download"
    % (PLUGIN_ID, PLUGIN_VERSION),
]

DEFAULT_PORT = 3000
ADMIN_USER = os.environ.get("GRAFANA_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")

UA = {"User-Agent": "zabbix-dashboard-setup/1.0"}


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ 下载/解压
def _fetch_one(url, dest):
    """流式下载单个源，支持断点续传（.part 文件保留，失败不删）。"""
    tmp = dest + ".part"
    start = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    headers = dict(UA)
    if start:
        headers["Range"] = "bytes=%d-" % start
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        if start and getattr(resp, "status", 200) != 206:
            start = 0                       # 服务端不支持断点，从头来
        total = int(resp.headers.get("Content-Length") or 0) + start
        got = start
        mark = (got * 100 // total) if total else 0
        with open(tmp, "ab" if start else "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct >= mark + 10:
                        mark = pct
                        log("    %3d%%   %.1f / %.1f MB"
                            % (pct, got / 1048576.0, total / 1048576.0))
    return got


def _fetch(urls, label, existing=None):
    """依次尝试多个下载源；existing 是手动放进 dist 的包，优先级最高。"""
    if existing and os.path.isfile(existing) and os.path.getsize(existing) > 0:
        log("  [已就位] %s（%.1f MB）"
            % (os.path.basename(existing), os.path.getsize(existing) / 1048576.0))
        return existing
    os.makedirs(DIST, exist_ok=True)
    last = None
    for url in urls:
        host = url.split("/")[2]
        dest = os.path.join(DIST, os.path.basename(url.split("?")[0]))
        try:
            log("  下载 %s  <- %s" % (label, host))
            _fetch_one(url, dest)
            os.replace(dest + ".part", dest)
            log("  完成 %s（%.1f MB）"
                % (os.path.basename(dest), os.path.getsize(dest) / 1048576.0))
            return dest
        except Exception as exc:                   # noqa: BLE001
            last = exc
            log("    [失败] %s: %s" % (host, exc))
    raise SystemExit("下载 %s 失败，已试过所有源：%s" % (label, last))


def _scan_local(prefix, exts):
    """在 dist 下找手动下载好的安装包（文件名只要前缀/后缀对得上就行）。"""
    if not os.path.isdir(DIST):
        return None
    for name in sorted(os.listdir(DIST)):
        low = name.lower()
        if low.startswith(prefix) and low.endswith(exts) and ".part" not in low:
            return os.path.join(DIST, name)
    return None


def local_grafana_archive():
    return _scan_local("grafana-", (".zip", ".tar.gz", ".tgz"))


def local_plugin_archive():
    return _scan_local(PLUGIN_ID, (".zip",))


def _extract(archive, target, label, done=False):
    """解压前先校验，避免镜像返回的错误页被当成正常包。

    done 表示目标已就绪（不能只看 target 是否为空：Grafana 是解压进 dist 的，
    而 dist 本来就放着安装包）。
    """
    if done:
        log("  [跳过] %s 已解压" % label)
        return
    log("  校验并解压 %s ..." % label)
    os.makedirs(target, exist_ok=True)
    low = archive.lower()
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad:
                    raise SystemExit("%s 压缩包损坏（首个坏文件：%s）。"
                                     "请删除后重下：%s" % (label, bad, archive))
                zf.extractall(target)
        except zipfile.BadZipFile:
            raise SystemExit("%s 不是合法 zip（可能是镜像返回的错误页）。"
                             "请删除后重试：%s" % (label, archive))
    elif low.endswith((".tar.gz", ".tgz")):
        try:
            with tarfile.open(archive, "r:gz") as tf:
                try:
                    tf.extractall(target, filter="data")
                except TypeError:                  # Python < 3.11.4 没有 filter 参数
                    tf.extractall(target)
        except tarfile.TarError as exc:
            raise SystemExit("%s 不是合法 tar.gz（可能镜像返回了错误页）：%s"
                             % (label, exc))
    else:
        raise SystemExit("不认识的安装包格式：%s" % archive)


def find_home():
    """定位解压出来的 Grafana 根目录（含 bin/ 与 conf/ 的那一层）。"""
    if not os.path.isdir(DIST):
        return None
    for name in sorted(os.listdir(DIST)):
        cand = os.path.join(DIST, name)
        if os.path.isdir(cand) and os.path.isdir(os.path.join(cand, "bin")) \
                and os.path.isdir(os.path.join(cand, "conf")):
            return cand
    return None


def grafana_bin(home):
    for name in ("grafana.exe", "grafana-server.exe"):
        path = os.path.join(home, "bin", name)
        if os.path.isfile(path):
            return path
    return None


# -------------------------------------------------------------- 数据源与看板
def _zabbix_conf():
    """从 .env 读取 Zabbix 地址与 API Token（复用 webapp/zbx.py 的解析逻辑）。"""
    sys.path.insert(0, os.path.join(ROOT, "webapp"))
    import zbx
    env = zbx.load_env()
    url = (env.get("ZABBIX_URL") or "").rstrip("/")
    token = env.get("ZABBIX_TOKEN") or ""
    if not url:
        raise SystemExit("未在 .env 中找到 ZABBIX_URL")
    if not token:
        raise SystemExit("未在 .env 中找到 ZABBIX_TOKEN")
    return url, token


def write_datasource():
    """生成数据源 provisioning。Token 只写进本地运行目录，不落进仓库。"""
    url, token = _zabbix_conf()
    ds_dir = os.path.join(PROV, "datasources")
    os.makedirs(ds_dir, exist_ok=True)
    body = """# 由 grafana/grafana.py 自动生成，含 API Token，请勿提交到版本库。
apiVersion: 1

datasources:
  - name: Zabbix
    type: alexanderzobnin-zabbix-datasource
    access: proxy
    url: %s/api_jsonrpc.php
    isDefault: true
    jsonData:
      authType: token
      username: ""
      trends: true
      trendsFrom: "7d"
      trendsRange: "4d"
      cacheTTL: "1m"
      timeout: 30
      alerting: false
    secureJsonData:
      apiToken: "%s"
""" % (url, token)
    path = os.path.join(ds_dir, "zabbix.yml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    log("  数据源已生成 -> %s" % os.path.relpath(path, ROOT))
    log("      Zabbix: %s" % url)

    # 看板 provider：把 grafana/dashboards/*.json 自动装载进来
    dash_dir = os.path.join(PROV, "dashboards")
    os.makedirs(dash_dir, exist_ok=True)
    # provider 指向的是 DASH，这个目录不存在 Grafana 会每 30 秒刷一条报错
    os.makedirs(DASH, exist_ok=True)
    with open(os.path.join(dash_dir, "default.yml"), "w", encoding="utf-8") as fh:
        fh.write("""apiVersion: 1

providers:
  - name: Zabbix Dashboards
    orgId: 1
    folder: "网络监控"
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: %s
      foldersFromFilesStructure: false
""" % DASH.replace("\\", "/"))
    log("  看板目录已挂载 -> %s" % os.path.relpath(DASH, ROOT))


def install_plugin(home, plugin_pkg):
    """把插件解压到 Grafana 的 plugins 目录。"""
    os.makedirs(PLUGINS, exist_ok=True)
    target = os.path.join(PLUGINS, PLUGIN_ID)
    if os.path.isdir(target) and os.listdir(target):
        log("  [跳过] Zabbix 插件已安装")
        return target
    staging = os.path.join(PLUGINS, "_stage")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    _extract(plugin_pkg, staging, "Zabbix 插件")
    # 插件包顶层一般是 alexanderzobnin-zabbix-app/，也兼容直接铺平
    inner = os.path.join(staging, PLUGIN_ID)
    src = inner if os.path.isdir(inner) else staging
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.move(src, target)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    log("  Zabbix 插件已安装 -> %s" % os.path.relpath(target, ROOT))
    return target


# ------------------------------------------------------------------ 生命周期
def env_for(home, port):
    e = dict(os.environ)
    e.update({
        "GF_PATHS_PROVISIONING": PROV,
        "GF_PATHS_DATA": DATA,
        "GF_PATHS_LOGS": LOGS,
        "GF_PATHS_PLUGINS": PLUGINS,
        "GF_SERVER_HTTP_PORT": str(port),
        "GF_SERVER_HTTP_ADDR": "127.0.0.1",
        "GF_SECURITY_ADMIN_USER": ADMIN_USER,
        "GF_SECURITY_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_NEWS_NEWS_FEED_ENABLED": "false",
        "GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS": PLUGIN_ID,
        "GF_LOG_LEVEL": "warn",
    })
    return e


def read_pid():
    try:
        with open(PIDFILE, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def is_running(pid):
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                         capture_output=True, text=True)
    return str(pid) in (out.stdout or "")


def do_start(port):
    home = find_home()
    if not home:
        raise SystemExit("还没安装 Grafana，请先执行：python grafana/grafana.py setup")
    exe = grafana_bin(home)
    if not exe:
        raise SystemExit("在 %s/bin 下找不到 Grafana 可执行文件" % home)

    pid = read_pid()
    if is_running(pid):
        log("Grafana 已在运行（PID %d）" % pid)
        return
    if not os.path.isfile(os.path.join(PROV, "datasources", "zabbix.yml")):
        write_datasource()

    os.makedirs(LOGS, exist_ok=True)
    out = open(os.path.join(LOGS, "grafana.out.log"), "a", encoding="utf-8")
    err = open(os.path.join(LOGS, "grafana.err.log"), "a", encoding="utf-8")

    args = [exe]
    if os.path.basename(exe).lower().startswith("grafana.exe"):
        args.append("server")
    args += ["--homepath", home,
             "--config", os.path.join(home, "conf", "defaults.ini")]

    creation = 0x00000008 | 0x00000200         # DETACHED_PROCESS | NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, env=env_for(home, port),
                            stdout=out, stderr=err, creationflags=creation, cwd=home)
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    log("Grafana 启动中（PID %d），等待就绪 ..." % proc.pid)

    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port,
                                        timeout=5) as resp:
                if json.loads(resp.read().decode()).get("database") == "ok":
                    log("就绪：http://127.0.0.1:%d  （%s / %s）"
                        % (port, ADMIN_USER, ADMIN_PASSWORD))
                    return
        except Exception:                          # noqa: BLE001
            pass
    log("等待超时，请查看 %s" % os.path.join(LOGS, "grafana.err.log"))


def do_stop():
    pid = read_pid()
    if not is_running(pid):
        log("Grafana 未在运行")
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True)
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)
    log("已停止 Grafana（PID %d）" % pid)


def do_status(port):
    pid = read_pid()
    if not is_running(pid):
        log("状态：未运行")
        return
    log("状态：运行中（PID %d）" % pid)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port,
                                    timeout=5) as resp:
            body = json.loads(resp.read().decode())
            log("  版本：%s   数据库：%s" % (body.get("version"), body.get("database")))
        log("  地址：http://127.0.0.1:%d" % port)
    except Exception as exc:                       # noqa: BLE001
        log("  健康检查失败：%s" % exc)


def do_setup(skip_download=False):
    log("=" * 60)
    log(" Grafana 监控看板 · 环境准备")
    log("=" * 60)
    os.makedirs(DIST, exist_ok=True)

    # 手动放进 dist 的安装包优先级最高；没有才去下载
    grafana_pkg = local_grafana_archive()
    plugin_pkg = local_plugin_archive()

    if skip_download:
        log("  [跳过] 下载步骤")
    else:
        log("[1/4] Grafana %s" % GRAFANA_VERSION)
        grafana_pkg = _fetch(GRAFANA_URLS, "Grafana", grafana_pkg)
        log("[2/4] Zabbix 插件 %s" % PLUGIN_VERSION)
        plugin_pkg = _fetch(PLUGIN_URLS, "Zabbix 插件", plugin_pkg)

    if not grafana_pkg or not plugin_pkg:
        raise SystemExit("缺少安装包，请把它们放进 %s" % DIST)

    log("[3/4] 解压")
    home = find_home()
    _extract(grafana_pkg, DIST, "Grafana", done=home is not None)
    if not home:
        home = find_home()
    if not home:
        raise SystemExit("解压后找不到 Grafana 目录，请检查 %s" % DIST)
    install_plugin(home, plugin_pkg)

    log("[4/4] 生成配置")
    write_datasource()

    log("-" * 60)
    log("准备完成。Grafana 根目录：%s" % os.path.relpath(home, ROOT))
    log("下一步：python grafana/grafana.py start")


def main():
    ap = argparse.ArgumentParser(description="Grafana + Zabbix 监控看板")
    ap.add_argument("action", choices=["setup", "start", "stop", "status", "restart"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass

    if args.action == "setup":
        do_setup(args.skip_download)
    elif args.action == "start":
        do_start(args.port)
    elif args.action == "stop":
        do_stop()
    elif args.action == "status":
        do_status(args.port)
    elif args.action == "restart":
        do_stop()
        time.sleep(2)
        do_start(args.port)


if __name__ == "__main__":
    main()
