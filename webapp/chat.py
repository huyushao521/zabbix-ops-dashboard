# -*- coding: utf-8 -*-
"""对话引擎：把自然语言问题映射成 Zabbix 查询 / 纳管动作，并生成回答 + 结构化卡片。

不依赖任何大模型，纯关键词意图 + 会话上下文（记住最近讨论的主机）+ 槽位状态机。

两类意图：
  * 只读查询 —— 直接查 Zabbix 回答（原逻辑）
  * 写操作纳管 —— 多轮收集信息 -> 预检 -> 确认闸门 -> 落库 -> 回读验证
"""
import os
import re
import threading
import time

import gdash
import provision
from zbx import (AVAILABILITY, IFACE_TYPE, SEVERITY, ZabbixError, get_client,
                 human_age)

DEFAULT_SUGGESTIONS = [
    "整体情况怎么样？",
    "现在有哪些告警？",
    "R1 正常吗？",
    "R1 的接口状态",
    "帮我做一次巡检",
    "添加一台监控节点",
]

HELP_TEXT = (
    "我是你的 Zabbix 运维助手，可以直接读 Zabbix 的实时数据。\n\n"
    "你可以这样问我："
)

COMMAND_HINTS = [
    "整体情况怎么样 / 概览",
    "现在有哪些告警 / 问题",
    "有哪些主机",
    "R1 正常吗（换成任意主机名）",
    "添加一台监控节点（如：SW1 192.168.1.30）",
    "把 R1 的 SNMP 团体名改成 secret（修改）",
    "R1 的接口状态",
    "R1 有哪些监控项没采到数据",
    "SNMP 链路实测",
    "帮我做一次巡检",
    "R1 看板（同步该主机到 Grafana）",
    "同步所有看板到 Grafana",
]

_PRONOUN = re.compile(r"(它|他|该设备|这台|这个设备|这个主机|上述|上面|刚才|那台)")


# ---------------------------------------------------------------- 卡片构造
def _block_stats(items):
    return {"type": "stats",
            "items": [{"label": l, "value": v, "tone": t} for l, v, t in items]}


def _block_table(title, columns, rows, hint=None):
    return {"type": "table", "title": title, "columns": columns,
            "rows": rows, "hint": hint}


def _block_list(title, items, tone="info"):
    return {"type": "list", "title": title, "items": items, "tone": tone}


def _block_note(text, tone="info"):
    return {"type": "note", "text": text, "tone": tone}


def _reply(text, blocks=None, session=None, suggestions=None):
    return {
        "text": text,
        "blocks": blocks or [],
        "session": session or {},
        "suggestions": suggestions or DEFAULT_SUGGESTIONS,
    }


# ---------------------------------------------------------------- Grafana 看板
SYNC_STATE = {}          # host -> 最近一次同步结果，后台补偿同步会覆盖


def _dash_block(result, host):
    """把同步结果转成卡片：成功给可点链接，失败给原因。"""
    if not result.get("ok"):
        return _block_note("Grafana 看板没生成：%s" % result.get("error", "未知原因"),
                           "warn")
    title = "%s · %s" % (host, "接口与链路" if result["interfaces"] else "设备概览")
    return _block_table(
        "Grafana 看板",
        ["看板", "面板", "接口", "打开"],
        [[title, len(result["panels"]), len(result["interfaces"]),
          {"html": '<a class="btn ghost sm" target="_blank" rel="noopener" '
                   'href="%s">打开看板</a>' % result["url"]}]],
        hint="Grafana 每 30 秒扫描一次看板目录，若刚生成请稍等再刷新页面。")


def _deferred_sync(host, delay=80):
    """LLD 还没跑完时，等一会儿再同步一次，把接口图补上。

    新增主机后模板里非 LLD 的监控项立刻就有，但接口是自动发现出来的，
    要等一个 LLD 周期（默认 60 秒）才有。这里用后台线程补一次，
    用户不必记住回来手动刷新。
    """
    time.sleep(delay)
    try:
        client = get_client()
        client.invalidate()
        SYNC_STATE[host] = gdash.sync(client, host)
    except Exception:                                  # noqa: BLE001
        pass


def sync_dashboard(host, allow_defer=True):
    """同步一台主机的 Grafana 看板。绝不抛异常，失败只是少一张看板。"""
    try:
        client = get_client()
        result = gdash.sync(client, host)
        SYNC_STATE[host] = result
        # 只有「命令成功但还没有接口」才值得补一次，其它情况补了也一样
        if allow_defer and result.get("ok") and not result["interfaces"]:
            threading.Thread(target=_deferred_sync, args=(host,),
                             daemon=True).start()
            result["deferred"] = True
        return result
    except Exception as exc:                           # noqa: BLE001
        return {"host": host, "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- 纳管（写操作）
# 触发词刻意收得很紧：这里一旦误判，就会把只读提问拉进写操作流程。
ADD_WORDS = ("添加", "新增", "纳管", "录入", "加一台", "加个", "加台", "开监控",
             "加监控", "添加监控", "监控node", "新加", "补一台")
MODIFY_WORDS = ("修改", "改成", "换成", "改为", "变更", "调整", "更新配置", "换模板")
SHORT_CONFIRM = {"ok", "yes", "y", "好", "行", "可以", "嗯", "对", "确认", "确定"}
SHORT_CANCEL = {"no", "cancel", "算了", "取消", "不", "不用", "不要"}


def _has(text, low, words):
    return any(w in text or w in low for w in words)


def _is_confirm(text, low):
    if low in SHORT_CONFIRM or text in SHORT_CONFIRM:
        return True
    return _has(text, low, ("确认", "确定", "执行", "创建吧", "建吧", "就这样",
                            "同意", "可以了", "没问题", "go ahead", "do it"))


def _is_cancel(text, low):
    if low in SHORT_CANCEL or text in SHORT_CANCEL:
        return True
    return _has(text, low, ("取消", "算了", "不建了", "放弃", "撤销", "不要了", "先不"))


def _is_force(text, low):
    return _has(text, low, ("强制", "强行", "force"))


def _spec_from_host(zbx, detail, override):
    """修改流程：用主机现有配置预填 spec，再用用户这句话覆盖。

    返回 (spec, warnings)。warnings 用于把"读取不到原值、已回退默认值"这类
    静默降级暴露到确认卡片上——否则用户会在不知情的情况下改错 community。
    """
    warnings = []
    spec = {"host": detail["host"], "port": provision.DEFAULT_PORT}
    for itf in detail["interfaces"]:
        if itf["type"] == "2":
            spec["ip"], spec["port"] = itf["ip"], itf["port"] or provision.DEFAULT_PORT
            break
    else:
        warnings.append("这台主机没有 SNMP 接口，IP 需你手动指定。")
    groups = detail["groups"]
    if provision.DEFAULT_GROUP in groups:
        spec["group"] = provision.DEFAULT_GROUP
    elif groups:
        spec["group"] = sorted(groups, key=len, reverse=True)[0]
    tpls = detail["templates"]
    spec["template"] = (provision.DEFAULT_TEMPLATE if provision.DEFAULT_TEMPLATE in tpls
                        else (tpls[0] if tpls else provision.DEFAULT_TEMPLATE))
    # 读原 community，读不到就必须告诉用户（不能静默退回 public）
    try:
        macros = zbx.call("usermacro.get", {"hostids": [detail["hostid"]],
                                            "output": ["macro", "value"]})
        current = [m["value"] for m in macros
                   if m["macro"].upper() == "{$SNMP_COMMUNITY}"]
        if current:
            spec["community"] = current[0]
        else:
            warnings.append("原有主机上没有 {$SNMP_COMMUNITY} 宏，community 将用默认值 %s。"
                            % provision.DEFAULT_COMMUNITY)
    except ZabbixError as exc:
        warnings.append("读取原有 SNMP 宏失败（%s），community 将用默认值 %s，请务必确认。"
                        % (exc, provision.DEFAULT_COMMUNITY))
    spec.update({k: v for k, v in (override or {}).items() if v})
    spec["host"] = detail["host"]
    return spec, warnings


def _pv_ask(session, spec, lack, mode="add", target=None):
    spec = provision.normalize_spec(spec)
    ask = []
    if "host" in lack:
        ask.append("主机名")
    if "ip" in lack:
        ask.append("SNMP 管理 IP")
    text = ("好的，开始纳管。还需要 **%s**。\n"
            "直接一行发我，比如 `SW1 192.168.1.30`。" % "、".join(ask))
    blocks = [_block_note(
        "其余项先用默认值：分组 %s ｜ 模板 %s ｜ 团体名 %s ｜ 端口 %s。"
        "想改就直说，比如「团体名用 secret」「端口 1161」。"
        % (provision.DEFAULT_GROUP, provision.DEFAULT_TEMPLATE,
           provision.DEFAULT_COMMUNITY, provision.DEFAULT_PORT))]
    session["pv"] = {"stage": "collect", "spec": spec, "mode": mode, "target": target}
    return _reply(text, blocks, session, suggestions=["取消"])


def _pv_confirm_card(zbx, session, spec, mode="add", target=None, note=None,
                     pre_notes=None):
    """渲染确认卡片。预检不通过（IP 冲突 / SNMP 不通）时提高确认门槛。

    pre_notes 是"预填阶段"的告警（如读不到原 community），会随会话保留，
    在用户补充信息后重新渲染时依然可见。
    """
    prev = session.get("pv") or {}
    warns = list(prev.get("warns") or []) if pre_notes is None else list(pre_notes)

    spec = provision.normalize_spec(spec)
    pf = provision.preflight(zbx, spec,
                             exclude_hostid=target if mode == "update" else None)
    need_force = not pf["ok"]

    rows = [
        ["模式", "新建主机" if mode == "add" else "修改已有主机 hostid=%s" % target],
        ["主机名", spec["host"]],
        ["SNMP 接口", "%s:%s  ·  v2c / bulk / community=%s"
         % (spec["ip"], spec["port"], spec["community"])],
        ["主机组", spec["group"]],
        ["模板", spec["template"]],
        ["宏", "{$SNMP_COMMUNITY}=%s" % spec["community"]],
    ]
    blocks = [_block_table("待执行方案（回复「确认」才写库）", ["字段", "取值"], rows)]

    lines = [note or "方案如下，确认无误请回复 **确认**。", ""]
    # 实测通过才在这里展示摘要；失败原因由下面的 notes 统一给出，避免重复
    if pf["probe"] and pf["probe"]["reachable"]:
        lines.append(provision.probe_summary(pf["probe"]))
    for c in pf["conflicts"]:
        lines.append("- ⚠️ " + c["text"])
    for n in pf["notes"]:
        lines.append("- ❌ " + n)
    for w in warns:
        lines.append("- ❗ " + w)

    if need_force:
        lines.append("")
        lines.append("**预检未通过。**确实要强写，请回复 **强制确认**；否则回复「取消」。")
        suggestions = ["强制确认", "取消"]
    else:
        suggestions = ["确认", "取消"]

    blocks += [_block_note(w, "warn") for w in warns]

    session["pv"] = {"stage": "confirm", "spec": spec, "mode": mode,
                     "target": target, "need_force": need_force, "warns": warns}
    return _reply("\n".join(lines), blocks, session, suggestions=suggestions)


def _pv_execute(zbx, session, pv):
    spec, mode, target = pv["spec"], pv.get("mode", "add"), pv.get("target")
    try:
        res = provision.apply(zbx, spec, mode=mode, hostid=target)
    except (provision.ProvisionError, ZabbixError) as exc:
        session.pop("pv", None)
        return _reply("❌ 写库失败，已中止：%s\n\nZabbix 未接受这次写入。" % exc,
                      [_block_note("没有产生任何变更。", "bad")], session)

    # 写库后必须让客户端缓存失效，否则接下来 5 秒内的查询
    # （包括用户马上追问的"XX 正常吗"）读到的还是写入前的旧快照
    zbx.invalidate()

    session.pop("pv", None)
    session["host"] = spec["host"]
    blocks = [_block_list("已执行 %d 个动作" % len(res["actions"]),
                          res["actions"], "ok")]

    try:
        rb = provision.readback(zbx, spec["host"], res["hostid"])
    except ZabbixError as exc:
        rb = {"found": False, "error": str(exc)}

    text = "✅ **%s** 纳管完成（hostid=%s）。\n\n%s" % (
        spec["host"], res["hostid"], provision.readback_lines(rb))
    blocks.append(_block_note(
        "Zabbix 首次采集约需 1 分钟；稍后问「%s 正常吗」或「%s 的接口状态」看结果。"
        % (spec["host"], spec["host"]), "info"))

    # 纳管成功就顺手把 Grafana 看板生成出来。看板是「写文件 + provisioning
    # 自动装载」，不需要 Grafana 登录态，Grafana 没开也不会让纳管失败。
    dash = sync_dashboard(spec["host"])
    blocks.append(_dash_block(dash, spec["host"]))
    if dash.get("ok"):
        text += "\n\n📊 **Grafana 看板已生成**（%d 个面板%s）。" % (
            len(dash["panels"]),
            "；接口图等 LLD 跑完会自动补上" if dash.get("deferred") else "")
    else:
        text += "\n\n⚠️ Grafana 看板未生成：%s" % dash.get("error")
    return _reply(text, blocks, session,
                  suggestions=["%s 正常吗？" % spec["host"],
                               "%s 的接口状态" % spec["host"],
                               "现在有哪些告警？"])


def _provision(zbx, session, text, low, host_hint):
    """纳管对话状态机。返回回复；返回 None 表示不是纳管意图，交回只读分支。"""
    pv = session.get("pv")

    # ---- 阶段二：等待确认，只认「确认 / 强制确认 / 取消 / 补充信息」----
    if pv and pv.get("stage") == "confirm":
        if _is_cancel(text, low):
            session.pop("pv", None)
            return _reply("好的，已取消，没有对 Zabbix 做任何写操作。", [], session)
        if _is_confirm(text, low):
            if pv.get("need_force") and not _is_force(text, low):
                pf = provision.preflight(
                    zbx, pv["spec"],
                    exclude_hostid=pv["target"] if pv.get("mode") == "update" else None)
                blocks = [_block_note(n, "bad") for n in pf["notes"]]
                blocks += [_block_note(c["text"], "warn") for c in pf["conflicts"]]
                if not blocks:
                    blocks = [_block_note("存在未通过项，请先确认。", "warn")]
                return _reply(
                    "预检没通过，还不能直接写库。确实要强写请回复 **强制确认**。",
                    blocks, session, suggestions=["强制确认", "取消"])
            return _pv_execute(zbx, session, pv)
        spec = provision.parse_spec(text, pv["spec"])
        return _pv_confirm_card(zbx, session, spec, pv.get("mode"), pv.get("target"),
                                note="已按你的补充更新方案：")

    # ---- 阶段一：正在收集槽位 ----
    if pv and pv.get("stage") == "collect":
        if _is_cancel(text, low):
            session.pop("pv", None)
            return _reply("好的，已取消。", [], session)
        spec = provision.parse_spec(text, pv["spec"])
        lack = provision.missing_slots(spec)
        if lack:
            return _pv_ask(session, spec, lack, pv.get("mode"), pv.get("target"))
        return _pv_confirm_card(zbx, session, spec, pv.get("mode"), pv.get("target"))

    # ---- 阶段零：识别纳管意图 ----
    is_add = _has(text, low, ADD_WORDS)
    is_mod = _has(text, low, MODIFY_WORDS)
    if not (is_add or is_mod):
        return None

    spec = provision.parse_spec(text, {})

    if is_mod and not is_add:
        detail = zbx.find_host(host_hint or spec.get("host") or "")
        if not detail:
            return _reply(
                "要修改哪台设备？直接给主机名，比如「把 R1 的 SNMP 团体名改成 secret」。",
                [_block_list("当前纳管的主机", [h["host"] for h in zbx.hosts()])],
                session, suggestions=["取消"])
        spec, warns = _spec_from_host(zbx, detail, spec)
        return _pv_confirm_card(zbx, session, spec, "update", detail["hostid"],
                                note="准备修改 **%s**，确认后生效：" % detail["host"],
                                pre_notes=warns)

    # 新增：同名主机已存在时自动切到修改流程，避免反复创建失败
    existing = zbx.find_host(spec["host"]) if spec.get("host") else None
    if existing:
        spec, warns = _spec_from_host(zbx, existing, spec)
        return _pv_confirm_card(
            zbx, session, spec, "update", existing["hostid"],
            note="主机 **%s** 已存在，已自动切到「修改」流程：" % existing["host"],
            pre_notes=warns)

    lack = provision.missing_slots(spec)
    if lack:
        return _pv_ask(session, spec, lack, "add", None)
    return _pv_confirm_card(zbx, session, spec, "add", None)


# ---------------------------------------------------------------- 主机识别
def _detect_host(text, session, zbx):
    """从问句中识别主机（支持主机名 / 显示名 / 接口 IP / 代词指代）。"""
    try:
        hosts = zbx.hosts()
    except ZabbixError:
        return None
    low = text.lower()
    best, best_len = None, 0
    for h in hosts:
        candidates = [h["host"], h["name"]]
        candidates += [i["ip"] for i in h["interfaces"] if i.get("ip")]
        for cand in candidates:
            if cand and cand.lower() in low and len(cand) > best_len:
                best, best_len = h["host"], len(cand)
    if best:
        return best
    if session.get("host") and _PRONOUN.search(text):
        return session["host"]
    return None


def _severity_rows(by_sev):
    rows = []
    for level in range(5, -1, -1):
        count = by_sev.get(str(level), 0)
        rows.append([SEVERITY[level], count])
    return rows


# ---------------------------------------------------------------- 主入口
def _grafana(zbx, session, host):
    """Grafana 看板：指定主机则同步该机，未指定则全量同步。"""
    if host:
        result = sync_dashboard(host)
        if not result.get("ok"):
            return _reply("❌ **%s** 的看板没同步成功：%s\n\n%s"
                          % (host, result.get("error"),
                             "Zabbix 里先得有这台主机。" if "没有主机" in str(result.get("error"))
                             else "纳管后等 LLD 跑完（约 1 分钟）再试一次。"),
                          [_dash_block(result, host)], session)

        bits = ["📊 **%s** 的 Grafana 看板已同步，共 **%d** 个面板（数据源 uid `%s`）。"
                % (host, len(result["panels"]),
                   result.get("datasource_uid") or "未解析到，改用默认数据源")]
        if result["interfaces"]:
            bits.append("接口 %d 个：%s。" % (len(result["interfaces"]),
                                            "、".join(result["interfaces"])))
        if result.get("deferred"):
            bits.append("当前还没有发现接口（LLD 未跑完），已安排后台自动补同步，"
                        "约 1 分钟后接口图会自己出现。")
        bits.append("看板文件：`%s`" % os.path.basename(result.get("path", "-")))
        return _reply("\n\n".join(bits), [_dash_block(result, host)], session,
                      suggestions=["%s 的接口状态" % host, "%s 正常吗？" % host,
                                   "现在有哪些告警？"])

    names = [h["host"] for h in zbx.hosts()]
    results = [sync_dashboard(n, allow_defer=False) for n in names]
    good = [r for r in results if r.get("ok")]
    rows = []
    for r in results:
        if r.get("ok"):
            rows.append([r["host"], len(r["panels"]), len(r["interfaces"]),
                         {"html": '<a class="btn ghost sm" target="_blank" '
                                  'rel="noopener" href="%s">打开</a>' % r["url"]}])
        else:
            rows.append([r["host"], "-", "-", str(r.get("error"))[:70]])
    return _reply(
        "已为 **%d/%d** 台主机生成 Grafana 看板。\n\n"
        "Grafana 每 30 秒扫描一次看板目录；刚建好的看板稍等一会儿再打开。"
        % (len(good), len(results)),
        [_block_table("Grafana 看板总览", ["主机", "面板", "接口", "打开"], rows)],
        session)


def answer(text, session=None, client=None):
    zbx = client or get_client()
    session = dict(session or {})
    q = (text or "").strip()
    if not q:
        return _reply("直接问我就行，比如「现在有哪些告警」。", [], session)

    low = q.lower()
    host = _detect_host(q, session, zbx)
    if host:
        session["host"] = host

    def has(*words):
        return any(w in low or w in q for w in words)

    try:
        # 纳管（写操作）优先：否则「节点 / 监控」会被下面的只读分支抢走
        pv_reply = _provision(zbx, session, q, low, host)
        if pv_reply is not None:
            return pv_reply

        if has("帮助", "help", "你能做什么", "能做什么", "怎么用", "支持哪些"):
            return _reply(HELP_TEXT, [_block_list("可以问我", COMMAND_HINTS)], session)

        if has("巡检", "体检", "报告", "report"):
            return _inspection(zbx, session)

        # Grafana 看板。必须排在「概览 / 主机」之前，
        # 否则「把 R1 展示到大屏」会被 _host_status 抢走。
        if has("grafana", "看板", "大屏", "仪表盘", "dashboard", "监控面板"):
            return _grafana(zbx, session, host)

        if has("告警", "报警", "问题", "故障", "异常", "alert", "problem", "alarm"):
            return _problems(zbx, session, host)

        if has("接口", "端口", "链路", "interface", "port"):
            return _interfaces(zbx, session, host)

        if has("不支持", "没数据", "没采到", "采不到", "采集失败", "unsupported"):
            return _unsupported(zbx, session, host)

        if has("监控项", "指标", "item", "metric"):
            return _items(zbx, session, host)

        if has("snmp", "community", "团体"):
            return _snmp(zbx, session, host, q)

        if has("模板", "template"):
            return _templates(zbx, session)

        if has("概览", "总览", "整体", "总体", "汇总", "情况", "怎么样",
               "overview", "summary", "健康"):
            return _host_status(zbx, session, host) if host else _overview(zbx, session)

        if has("主机", "设备", "节点", "host", "device", "有哪些", "列表"):
            return _host_status(zbx, session, host) if host else _hosts(zbx, session)

        if host:
            return _host_status(zbx, session, host)

    except ZabbixError as exc:
        return _reply("查询 Zabbix 时出错了：%s" % exc, [], session)

    return _reply(
        "这句话我没太理解 🤔 换个说法，或者试试下面这些：",
        [_block_list("可以问我", COMMAND_HINTS)],
        session,
    )

# ---------------------------------------------------------------- 各类意图
def _overview(zbx, session):
    ov = zbx.overview()
    hosts = zbx.hosts()
    probs = ov["problems_total"]
    if probs == 0:
        tail = "当前**没有活跃告警**，一切正常 ✅"
    elif probs == 1:
        tail = "当前有 **1** 条活跃告警 ⚠️"
    else:
        tail = "当前有 **%d** 条活跃告警 ⚠️" % probs

    text = ("Zabbix **%s** 共纳管 **%d** 台主机，%s\n"
            "监控项 %d 个：**%d** 个正常采集，%d 个不支持。"
            % (ov["version"], ov["hosts_total"], tail,
               ov["items_total"], ov["items_with_data"], ov["items_unsupported"]))

    blocks = [_block_stats([
        ("主机总数", ov["hosts_total"], "info"),
        ("活跃告警", probs, "bad" if probs else "ok"),
        ("监控项", ov["items_total"], "info"),
        ("正常采集", ov["items_with_data"], "ok"),
        ("不支持项", ov["items_unsupported"], "warn" if ov["items_unsupported"] else "ok"),
        ("模板数", ov["templates_total"], "info"),
    ])]

    if probs:
        blocks.append(_block_table("告警按严重级别", ["级别", "数量"],
                                   _severity_rows(ov["problems_by_severity"])))

    blocks.append(_block_table(
        "主机健康总览",
        ["主机", "分组", "接口类型", "已采集/总数", "告警"],
        [[h["host"],
          "/".join(h["groups"]) or "-",
          "/".join(sorted({i["type_text"] for i in h["interfaces"]})) or "-",
          "%d/%d" % (h["items"]["with_data"], h["items"]["total"]),
          str(h["problems"])] for h in hosts],
    ))
    return _reply(text, blocks, session)


def _hosts(zbx, session):
    hosts = zbx.hosts()
    if not hosts:
        return _reply("Zabbix 里目前一台主机都没有。", [], session)

    rows = []
    for h in hosts:
        addrs = " / ".join(
            "%s:%s" % (i["ip"] or i["dns"], i["port"]) for i in h["interfaces"]
        ) or "-"
        rows.append([h["host"], "/".join(h["groups"]) or "-", addrs,
                     h["status_text"], str(h["problems"]),
                     "%d/%d" % (h["items"]["with_data"], h["items"]["total"])])

    text = ("共 **%d** 台主机：%s。\n其中 %d 台有活跃告警。"
            % (len(hosts), "、".join("**%s**" % h["host"] for h in hosts),
               sum(1 for h in hosts if h["problems"] > 0)))

    blocks = [_block_table("主机清单",
                           ["主机", "分组", "接口", "状态", "告警", "已采集/总数"],
                           rows)]
    return _reply(text, blocks, session)


def _iface_rows(ifaces):
    return [[i["name"], i["oper_text"], i["speed"] or "-",
             i["bits_received"] or "-", i["bits_sent"] or "-",
             i["in_errors"] or "-", i["out_errors"] or "-"] for i in ifaces]


def _host_status(zbx, session, host):
    if not host:
        return _hosts(zbx, session)
    d = zbx.host_detail(host)
    if not d:
        return _reply("没找到主机 **%s**，确认一下名字？" % host, [], session)

    h = d["host"]
    items = h["items"]
    iface = next((i for i in h["interfaces"] if i["type"] == "2"), None) \
        or (h["interfaces"][0] if h["interfaces"] else None)
    iface_desc = "-"
    if iface:
        iface_desc = "%s %s:%s（%s）" % (
            iface["type_text"], iface["ip"] or iface["dns"],
            iface["port"], iface["available_text"])

    if h["problems"] == 0:
        head = "**%s** 目前健康，没有活跃告警 ✅" % h["host"]
    else:
        head = ("**%s** 有 **%d** 条活跃告警，最严重等级为 **%s** ⚠️"
                % (h["host"], h["problems"], h["worst_severity_text"]))

    text = (head + "\n"
            "接口：%s ｜ 监控项 %d 个（已采集 **%d**，不支持 %d）\n"
            "分组：%s ｜ 模板：%s"
            % (iface_desc, items["total"], items["with_data"], items["unsupported"],
               "、".join(h["groups"]) or "-", "、".join(h["templates"]) or "-"))

    blocks = [_block_stats([
        ("监控项总数", items["total"], "info"),
        ("正常采集", items["with_data"], "ok"),
        ("不支持", items["unsupported"], "warn" if items["unsupported"] else "ok"),
        ("活跃告警", h["problems"], "bad" if h["problems"] else "ok"),
        ("告警触发器", h["triggers"], "info"),
        ("网络接口", len(d["network_interfaces"]), "info"),
    ])]

    if d["network_interfaces"]:
        blocks.append(_block_table(
            "接口状态（LLD 自动发现）",
            ["接口", "运行状态", "速率", "接收", "发送", "入错包", "出错包"],
            _iface_rows(d["network_interfaces"])))

    if d["problems"]:
        blocks.append(_block_table(
            "活跃告警", ["级别", "问题", "持续"],
            [[p["severity_text"], p["name"], p["age"]] for p in d["problems"]]))

    if d["unsupported"]:
        blocks.append(_block_list(
            "不支持项（%d 个）" % len(d["unsupported"]),
            ["%s（%s）" % (i["name"], i["error"]) for i in d["unsupported"]],
            tone="warn"))

    session["host"] = h["host"]
    return _reply(text, blocks, session)


def _problems(zbx, session, host):
    problems = zbx.problems(host=host)
    scope = "**%s** 的" % host if host else "整个环境"

    if not problems:
        return _reply("✅ %s当前**没有活跃告警**，一切正常。" % scope, [], session)

    worst = problems[0]
    text = ("%s活跃告警共 **%d** 条。最严重的是 **%s** 级别：\n"
            "「%s」— %s，已持续 %s。"
            % (scope, len(problems), worst["severity_text"],
               worst["name"], worst["host"], worst["age"]))

    rows = [[p["severity_text"], p["host"], p["name"], p["age"],
             "已确认" if p["acknowledged"] else "未确认"] for p in problems]
    blocks = [_block_table("活跃告警清单",
                           ["级别", "主机", "问题", "持续", "状态"], rows)]

    by_host = {}
    for p in problems:
        by_host[p["host"]] = by_host.get(p["host"], 0) + 1
    if len(by_host) > 1:
        blocks.append(_block_table("按主机汇总", ["主机", "告警数"],
                                   [[k, v] for k, v in sorted(by_host.items(),
                                                              key=lambda x: -x[1])]))
    return _reply(text, blocks, session)

def _interfaces(zbx, session, host):
    targets = [host] if host else [h["host"] for h in zbx.hosts()]
    tables, total, lines = [], 0, []

    for name in targets:
        d = zbx.host_detail(name)
        if not d:
            continue
        ifaces = d["network_interfaces"]
        if not ifaces:
            lines.append("**%s**：没有 LLD 发现的网络接口" % name)
            continue
        up = sum(1 for i in ifaces if i["oper_text"] == "UP")
        down = [i["name"] for i in ifaces if i["oper_text"] != "UP"]
        total += len(ifaces)
        lines.append("**%s**：共 %d 个接口，**%d 个 UP**%s"
                     % (name, len(ifaces), up,
                        ("，异常：" + "、".join(down)) if down else "，全部正常"))
        tables.append(_block_table(
            "%s 接口状态" % name,
            ["接口", "运行状态", "速率", "接收", "发送", "入错包", "出错包"],
            _iface_rows(ifaces)))

    if not tables:
        return _reply("没有查到网络接口信息。", [], session)

    text = "\n".join(lines) + ("\n\n合计 **%d** 个接口。" % total)
    return _reply(text, tables, session)


def _unsupported(zbx, session, host):
    targets = [host] if host else [h["host"] for h in zbx.hosts()]
    blocks, lines, total = [], [], 0

    for name in targets:
        d = zbx.host_detail(name)
        if not d:
            continue
        items = d["unsupported"]
        total += len(items)
        if not items:
            lines.append("**%s**：没有不支持项 ✅" % name)
            continue
        lines.append("**%s**：**%d** 个监控项采集失败" % (name, len(items)))
        blocks.append(_block_list(
            "%s 不支持项" % name,
            ["%s —— %s" % (i["name"], i["error"]) for i in items],
            tone="warn"))

    text = "\n".join(lines) + ("\n\n合计 **%d** 个监控项没有采到数据。" % total)
    return _reply(text, blocks, session)


def _items(zbx, session, host):
    if not host:
        ov = zbx.overview()
        return _reply(
            "整个环境共有 **%d** 个监控项，其中 **%d** 个正常采集，%d 个不支持。\n"
            "想细看哪台主机？直接说主机名，例如「R1 的监控项」。"
            % (ov["items_total"], ov["items_with_data"], ov["items_unsupported"]),
            [_block_stats([
                ("监控项总数", ov["items_total"], "info"),
                ("正常采集", ov["items_with_data"], "ok"),
                ("不支持", ov["items_unsupported"], "warn"),
            ])],
            session)

    d = zbx.host_detail(host)
    if not d:
        return _reply("没找到主机 **%s**。" % host, [], session)

    samples = d["samples"]
    text = ("**%s** 共 **%d** 个监控项，%d 个已采集到数据，%d 个不支持。\n"
            "下面是最近一次采集值（最多展示 60 条）。"
            % (host, d["items_total"], len(samples), d["items_unsupported"]))

    blocks = []
    if samples:
        blocks.append(_block_table(
            "监控项实时值", ["监控项", "当前值", "最近更新"],
            [["%s%s" % (s["name"], (" (%s)" % s["units"]) if s["units"] else ""),
              s["value"], s["age"]] for s in samples],
            hint="数据来自 Zabbix 最近一次采集，仅作快速浏览"))
    session["host"] = host
    return _reply(text, blocks, session)


def _snmp(zbx, session, host, question):
    ifaces = zbx.snmp_interfaces()
    if not ifaces:
        return _reply("没有找到任何 SNMP 接口配置。", [], session)

    want_probe = any(k in question for k in ("实测", "探测", "测一下", "检查", "连通", "test"))
    if host:
        ifaces = [i for i in ifaces if i["host"].lower() == host.lower()] or ifaces

    lines = []
    blocks = []
    for i in ifaces:
        lines.append("**%s** → SNMP %s:161（Zabbix 侧状态：%s）"
                     % (i["host"], i["ip"], i["available_text"]))

    if want_probe:
        import snmp_probe
        for i in ifaces:
            res = snmp_probe.query(i["ip"])
            if res["reachable"]:
                lines.append("✅ **%s** SNMP 实测通过（%d/%d 个 OID 有响应）"
                             % (i["ip"], res["ok_count"], res["total"]))
                blocks.append(_block_table(
                    "%s SNMP 实测结果" % i["ip"],
                    ["OID", "含义", "返回值"],
                    [[r["oid"], r["label"], r["value"] or r["error"]]
                     for r in res["results"]]))
            else:
                lines.append("❌ **%s** SNMP 实测失败：%s"
                             % (i["ip"], res["results"][0]["error"] if res["results"] else "无响应"))

    text = "\n".join(lines)
    if not want_probe:
        text += "\n\n需要我从本机直接发 SNMP 报文做一次真实校验吗？说「SNMP 实测」即可。"
    return _reply(text, blocks, session)


def _templates(zbx, session):
    templates = zbx.templates()
    relevant = [t for t in templates if t["relevant"]]
    text = ("Zabbix 中共有 **%d** 个模板，其中 **%d** 个与网络设备相关"
            "（Cisco / 华为 / SNMP / 交换机 / 路由器）。\n"
            "下面列出相关的模板（最多 40 个）。"
            % (len(templates), len(relevant)))
    blocks = [_block_table("网络设备相关模板", ["模板名", "显示名"],
                           [[t["host"], t["name"]] for t in relevant[:40]])]
    return _reply(text, blocks, session)


def _inspection(zbx, session):
    ov = zbx.overview()
    hosts = zbx.hosts()
    problems = zbx.problems()

    healthy = [h for h in hosts if h["problems"] == 0]
    unhealthy = [h for h in hosts if h["problems"] > 0]

    text = ("# 巡检结果\n"
            "Zabbix **%s**，纳管 **%d** 台主机：健康 %d 台，异常 %d 台；"
            "活跃告警 **%d** 条，不支持项 %d 个。"
            % (ov["version"], ov["hosts_total"], len(healthy), len(unhealthy),
               ov["problems_total"], ov["items_unsupported"]))

    if unhealthy:
        text += "\n需要关注：" + "、".join("**%s**（%d 条告警）"
                                          % (h["host"], h["problems"]) for h in unhealthy)
    else:
        text += "\n所有主机均无活跃告警 ✅"

    blocks = [_block_stats([
        ("主机", ov["hosts_total"], "info"),
        ("健康主机", len(healthy), "ok"),
        ("异常主机", len(unhealthy), "bad" if unhealthy else "ok"),
        ("活跃告警", ov["problems_total"], "bad" if ov["problems_total"] else "ok"),
        ("不支持项", ov["items_unsupported"], "warn" if ov["items_unsupported"] else "ok"),
        ("模板数", ov["templates_total"], "info"),
    ])]

    blocks.append(_block_table(
        "主机巡检明细",
        ["主机", "分组", "监控项(已采集/总数)", "不支持", "告警", "结论"],
        [[h["host"], "/".join(h["groups"]) or "-",
          "%d/%d" % (h["items"]["with_data"], h["items"]["total"]),
          str(h["items"]["unsupported"]), str(h["problems"]),
          "正常 ✅" if h["problems"] == 0 else "异常 ⚠️"] for h in hosts]))

    if problems:
        blocks.append(_block_table(
            "活跃告警", ["级别", "主机", "问题", "持续"],
            [[p["severity_text"], p["host"], p["name"], p["age"]] for p in problems]))

    for h in unhealthy:
        d = zbx.host_detail(h["host"])
        if d and d["unsupported"]:
            blocks.append(_block_list(
                "%s 采集异常项" % h["host"],
                ["%s —— %s" % (i["name"], i["error"]) for i in d["unsupported"]],
                tone="warn"))

    return _reply(text, blocks, session)


