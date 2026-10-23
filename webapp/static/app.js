/* Zabbix 运维看板 —— 前端逻辑（原生 JS，无框架） */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var SEV_TONE = { "灾难": "bad", "严重": "bad", "一般": "warn", "警告": "warn", "信息": "info", "未分类": "na" };
  var SEV_ORDER = { "灾难": 5, "严重": 4, "一般": 3, "警告": 2, "信息": 1, "未分类": 0 };
  var SEV_NAMES = ["未分类", "信息", "警告", "一般", "严重", "灾难"];

  var VIEW_META = {
    overview: ["概览", "实时读取 Zabbix 监控数据"],
    hosts: ["主机", "点击主机名查看单机详情"],
    host: ["主机详情", ""],
    problems: ["告警", "当前尚未恢复的活跃问题"],
    snmp: ["SNMP 实测", "从本机直接向设备发送 SNMP 报文做独立校验"]
  };

  var state = {
    view: "overview",
    host: null,
    session: {},
    data: { overview: null, hosts: null, problems: null, snmp: null, grafana: null },
    chatBusy: false
  };

  /* ---------------------------------------------------------- 基础工具 */
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () {
        return { ok: false, error: "响应解析失败（HTTP " + res.status + "）" };
      }).then(function (body) {
        if (!res.ok || body.ok === false) throw new Error(body.error || ("HTTP " + res.status));
        return body.data;
      });
    });
  }

  function post(path, payload) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  }

  /* ---------------------------------------------------------- 卡片渲染 */
  function statsBlock(pairs) {
    return '<div class="stats">' + pairs.map(function (p) {
      return '<div class="stat ' + esc(p[2] || "info") + '">' +
        '<div class="stat-label">' + esc(p[0]) + "</div>" +
        '<div class="stat-value">' + esc(p[1]) + "</div></div>";
    }).join("") + "</div>";
  }

  function cellHtml(cell, header) {
    /* 允许单元格传入 {html: "..."} 直接输出富文本（如操作按钮） */
    if (cell && typeof cell === "object" && cell.html !== undefined) return cell.html;

    var text = cell === null || cell === undefined ? "" : String(cell);
    if (header === "级别" || header === "严重度") {
      return '<span class="tag ' + esc(SEV_TONE[text] || "na") + '">' + esc(text) + "</span>";
    }
    if (header === "主机") {
      return '<button class="host-link" data-host="' + esc(text) + '">' + esc(text) + "</button>";
    }
    if (header === "运行状态") {
      return '<span class="tag ' + (text === "UP" ? "ok" : (text === "-" ? "na" : "bad")) + '">' + esc(text) + "</span>";
    }
    if (header === "状态" || header === "结论") {
      if (/正常|异常|启用|停用|已确认|未确认/.test(text)) {
        var tone = /正常|启用|已确认/.test(text) ? "ok" : (/未确认/.test(text) ? "warn" : "bad");
        return '<span class="tag ' + tone + '">' + esc(text) + "</span>";
      }
    }
    return esc(text);
  }

  function tableBlock(title, columns, rows, hint) {
    var head = columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("");
    var body = rows.length ? rows.map(function (r) {
      return "<tr>" + r.map(function (cell, idx) {
        return "<td>" + cellHtml(cell, columns[idx]) + "</td>";
      }).join("") + "</tr>";
    }).join("") : '<tr><td class="dim" colspan="' + columns.length + '">无数据</td></tr>';

    return '<div class="card">' +
      (title ? '<div class="card-head"><span class="card-title">' + esc(title) + "</span>" +
        (hint ? '<span class="card-hint">' + esc(hint) + "</span>" : "") + "</div>" : "") +
      '<div class="table-wrap"><table><thead><tr>' + head + "</tr></thead><tbody>" +
      body + "</tbody></table></div></div>";
  }

  function listBlock(title, items, tone) {
    return '<div class="card"><div class="card-head"><span class="card-title">' + esc(title) +
      '</span></div><div class="list-block ' + esc(tone || "info") + '"><ul>' +
      items.map(function (i) { return "<li>" + esc(i) + "</li>"; }).join("") +
      "</ul></div></div>";
  }

  function noteBlock(text, tone) {
    return '<div class="note ' + esc(tone || "info") + '">' + esc(text) + "</div>";
  }

  function blocksHtml(blocks) {
    return (blocks || []).map(function (b) {
      if (!b) return "";
      if (b.type === "stats") {
        return statsBlock(b.items.map(function (i) { return [i.label, i.value, i.tone]; }));
      }
      if (b.type === "table") return tableBlock(b.title, b.columns, b.rows, b.hint);
      if (b.type === "list") return listBlock(b.title, b.items, b.tone);
      if (b.type === "note") return noteBlock(b.text, b.tone);
      return "";
    }).join("");
  }

  function severityRows(bySev) {
    return [5, 4, 3, 2, 1, 0].map(function (l) {
      return [SEV_NAMES[l], bySev[String(l)] || 0];
    });
  }

  function setTitle(view, sub) {
    var meta = VIEW_META[view] || ["", ""];
    $("#view-title").textContent = meta[0];
    $("#view-sub").textContent = sub || meta[1];
  }

  /* ---------------------------------------------------------- 各视图 */
  function ifaceTypes(h) {
    var types = h.interfaces.map(function (i) { return i.type_text; });
    return types.filter(function (v, i) { return types.indexOf(v) === i; }).join("/") || "-";
  }

  function viewOverview() {
    var ov = state.data.overview, hosts = state.data.hosts;
    var probs = ov.problems_total;
    var blocks = [statsBlock([
      ["主机总数", ov.hosts_total, "info"],
      ["活跃告警", probs, probs ? "bad" : "ok"],
      ["监控项", ov.items_total, "info"],
      ["正常采集", ov.items_with_data, "ok"],
      ["不支持项", ov.items_unsupported, ov.items_unsupported ? "warn" : "ok"],
      ["模板数", ov.templates_total, "info"]
    ])];

    if (probs) blocks.push(tableBlock("告警按严重级别", ["级别", "数量"], severityRows(ov.problems_by_severity)));

    blocks.push(tableBlock("主机健康总览",
      ["主机", "分组", "接口类型", "已采集/总数", "告警"],
      hosts.map(function (h) {
        return [h.host, h.groups.join("/") || "-", ifaceTypes(h),
          h.items.with_data + "/" + h.items.total, h.problems];
      })));

    var head = '<div class="note">Zabbix <strong>' + esc(ov.version) + "</strong> ｜ API " + esc(ov.url) +
      " ｜ 自动发现规则 " + ov.discovery_rules_total + " 条" +
      (ov.proxies.length ? " ｜ Proxy " + ov.proxies.length + " 个" : "") +
      " ｜ 维护窗口 " + ov.maintenances.length + " 个</div>";

    setTitle("overview");
    /* 注意：这里 blocks 里已经是渲染好的 HTML 片段，直接拼接即可 */
    return head + blocks.join("");
  }

  function viewHosts() {
    var hosts = state.data.hosts;
    var withProb = hosts.filter(function (h) { return h.problems > 0; }).length;
    var dash = {};
    ((state.data.grafana && state.data.grafana.dashboards) || []).forEach(function (d) {
      dash[d.host] = d;
    });
    var blocks = [statsBlock([
      ["主机总数", hosts.length, "info"],
      ["有告警", withProb, withProb ? "bad" : "ok"],
      ["健康主机", hosts.length - withProb, "ok"],
      ["不支持项合计", hosts.reduce(function (a, h) { return a + h.items.unsupported; }, 0), "warn"]
    ])];

    blocks.push(tableBlock("主机清单",
      ["主机", "分组", "接口地址", "状态", "告警", "已采集/总数", "模板", "Grafana"],
      hosts.map(function (h) {
        var addr = h.interfaces.map(function (i) {
          return (i.ip || i.dns) + ":" + i.port + " (" + i.type_text + ")";
        }).join("，") || "-";
        return [h.host, h.groups.join("/") || "-", addr, h.status_text, h.problems,
          h.items.with_data + "/" + h.items.total, h.templates.join("，") || "-",
          dashCell(h.host, dash[h.host])];
      })));

    setTitle("hosts");
    return blocks.join("");
  }

  /* 已有看板给链接，没有的给「生成看板」按钮 */
  function dashCell(host, info) {
    if (info) {
      return { html: '<a class="btn ghost sm" target="_blank" rel="noopener" href="' +
        esc(info.url) + '">看板</a>' };
    }
    return { html: '<button class="btn ghost sm dash-btn" data-host="' +
      esc(host) + '">生成看板</button>' };
  }

  function viewProblems() {
    var list = state.data.problems;
    setTitle("problems", list.length ? "共 " + list.length + " 条未恢复的告警" : "当前没有任何活跃告警");

    if (!list.length) {
      return statsBlock([["活跃告警", 0, "ok"]]) +
        listBlock("检查结果", ["所有主机的触发器均处于正常状态，无需处理。"], "info");
    }

    var worst = list.reduce(function (a, b) {
      return (SEV_ORDER[b.severity_text] || 0) > (SEV_ORDER[a.severity_text] || 0) ? b : a;
    }, list[0]);

    var hostSet = {};
    list.forEach(function (p) { hostSet[p.host] = 1; });

    var stats = statsBlock([
      ["活跃告警", list.length, "bad"],
      ["最严重级别", worst.severity_text, "bad"],
      ["涉及主机", Object.keys(hostSet).length, "warn"],
      ["未确认", list.filter(function (p) { return !p.acknowledged; }).length, "warn"]
    ]);

    var table = tableBlock("活跃告警清单",
      ["级别", "主机", "问题", "持续", "状态"],
      list.map(function (p) {
        return [p.severity_text, p.host, p.name, p.age, p.acknowledged ? "已确认" : "未确认"];
      }), "点击主机名可跳转单机详情");

    return stats + table;
  }

  function viewSnmp() {
    var list = state.data.snmp;
    setTitle("snmp");
    var head = '<div class="note">下面是 Zabbix 中配置的 SNMP 接口。点击「实测」由本机直接向设备发送 ' +
      "SNMPv2c 报文，用来区分「设备侧 SNMP 问题」与「Zabbix 采集链路问题」。</div>";

    if (!list.length) return head + listBlock("SNMP 接口", ["没有配置任何 SNMP 接口。"], "warn");

    var rows = list.map(function (i) {
      return [i.host, i.ip, i.port, i.available_text,
        { html: '<button class="btn ghost sm probe-btn" data-ip="' + esc(i.ip) + '">实测</button>' }];
    });

    return head + tableBlock("Zabbix 中的 SNMP 接口", ["主机", "IP 地址", "端口", "Zabbix 侧状态", "操作"], rows) +
      '<div id="probe-result"></div>';
  }

  function renderHostDetail(d) {
    var h = d.host;
    var iface = h.interfaces.filter(function (i) { return i.type === "2"; })[0] || h.interfaces[0];
    var head = '<div class="note">' + (h.problems === 0
      ? "该主机当前 <strong>健康</strong>，没有活跃告警 ✅"
      : "存在 <strong>" + h.problems + "</strong> 条活跃告警，最严重为 <strong>" + esc(h.worst_severity_text) + "</strong> ⚠️") +
      (iface ? " ｜ 接口 " + esc(iface.type_text) + " " + esc(iface.ip || iface.dns) + ":" + esc(iface.port) +
        "（" + esc(iface.available_text) + "）" : "") +
      " ｜ 分组 " + esc(h.groups.join("、") || "-") +
      " ｜ 模板 " + esc(h.templates.join("、") || "-") + "</div>";

    var blocks = [statsBlock([
      ["监控项总数", d.items_total, "info"],
      ["正常采集", h.items.with_data, "ok"],
      ["不支持", d.items_unsupported, d.items_unsupported ? "warn" : "ok"],
      ["活跃告警", h.problems, h.problems ? "bad" : "ok"],
      ["告警触发器", h.triggers, "info"],
      ["网络接口", d.network_interfaces.length, "info"]
    ])];

    if (d.network_interfaces.length) {
      blocks.push(tableBlock("接口状态（LLD 自动发现）",
        ["接口", "运行状态", "速率", "接收", "发送", "入错包", "出错包"],
        d.network_interfaces.map(function (i) {
          return [i.name, i.oper_text, i.speed || "-", i.bits_received || "-",
            i.bits_sent || "-", i.in_errors || "-", i.out_errors || "-"];
        }), "UP 表示链路正常"));
    }

    if (d.problems.length) {
      blocks.push(tableBlock("活跃告警", ["级别", "问题", "持续", "状态"],
        d.problems.map(function (p) {
          return [p.severity_text, p.name, p.age, p.acknowledged ? "已确认" : "未确认"];
        })));
    }

    if (d.unsupported.length) {
      blocks.push(listBlock("采集异常项（" + d.unsupported.length + " 个）",
        d.unsupported.map(function (i) { return i.name + " —— " + i.error; }), "warn"));
    }

    if (d.samples.length) {
      blocks.push(tableBlock("监控项实时值", ["监控项", "当前值", "最近更新"],
        d.samples.map(function (s) {
          return [s.name + (s.units ? " (" + s.units + ")" : ""), s.value, s.age];
        }), "最多展示 60 条"));
    }

    var bar = '<div style="margin-bottom:14px"><button class="btn ghost sm" id="back-hosts">← 返回主机列表</button>';
    var made = ((state.data.grafana && state.data.grafana.dashboards) || []).filter(function (d) {
      return d.host === h.host;
    })[0];
    bar += made
      ? ' <a class="btn ghost sm" target="_blank" rel="noopener" href="' + esc(made.url) + '">打开 Grafana 看板</a>'
      : ' <button class="btn ghost sm dash-btn" data-host="' + esc(h.host) + '">生成 Grafana 看板</button>';
    bar += "</div>";

    return bar +
      head + blocks.join("");
  }

  /* ---------------------------------------------------------- 数据加载 */
  /* 看板清单拉不到不该影响主视图，所以单独兜底 */
  function loadGrafana() {
    return api("/api/grafana").catch(function () {
      return { url: "", folder: "", dashboards: [] };
    });
  }

  function loadView(view) {
    var content = $("#content");
    content.innerHTML = '<div class="loading">正在加载…</div>';

    if (view === "host") {
      return Promise.all([
        api("/api/host?name=" + encodeURIComponent(state.host)), loadGrafana()
      ]).then(function (r) {
        state.data.grafana = r[1];
        content.innerHTML = renderHostDetail(r[0]);
      }).catch(function (e) {
        content.innerHTML = noteBlock("加载失败：" + e.message, "warn");
      });
    }

    if (view === "hosts") {
      return Promise.all([api("/api/hosts"), loadGrafana()]).then(function (r) {
        state.data.hosts = r[0];
        state.data.grafana = r[1];
        content.innerHTML = viewHosts();
      }).catch(function (e) {
        content.innerHTML = noteBlock("加载失败：" + e.message, "warn");
      });
    }

    return api("/api/" + view).then(function (data) {
      state.data[view] = data;
      if (view === "overview") content.innerHTML = viewOverview();
      else if (view === "hosts") content.innerHTML = viewHosts();
      else if (view === "problems") content.innerHTML = viewProblems();
      else if (view === "snmp") content.innerHTML = viewSnmp();
      if (view === "problems") updateBadge(data.length);
    }).catch(function (e) {
      content.innerHTML = noteBlock("加载失败：" + e.message, "warn");
    });
  }

  function updateBadge(count) {
    var badge = $("#nav-badge");
    badge.textContent = count;
    badge.hidden = !count;
  }

  var VIEWS = ["overview", "hosts", "problems", "snmp", "host"];

  function hashFor(view, host) {
    return "#" + (view === "host" && host ? "host/" + encodeURIComponent(host) : view);
  }

  function switchView(view, host, fromHash) {
    state.view = view;
    if (host) state.host = host;
    $$(".nav-item").forEach(function (b) {
      var active = b.dataset.view === view || (view === "host" && b.dataset.view === "hosts");
      b.classList.toggle("active", active);
    });
    if (!fromHash) {
      var target = hashFor(view, host || state.host);
      if (location.hash !== target) {
        state.ignoreHash = true;
        location.hash = target;
      }
    }
    loadView(view);
  }

  /* 支持直接访问 #hosts / #problems / #snmp / #host/R1，并支持浏览器前进后退 */
  function applyHash() {
    var raw = (location.hash || "").replace(/^#/, "");
    var parts = raw.split("/");
    var view = VIEWS.indexOf(parts[0]) >= 0 ? parts[0] : "overview";
    if (view === "host" && parts[1]) {
      switchView("host", decodeURIComponent(parts[1]), true);
    } else {
      switchView(view === "host" ? "overview" : view, null, true);
    }
  }

  function setConn(ok, text) {
    $("#conn .dot").className = "dot " + (ok ? "ok" : "bad");
    $("#conn-text").textContent = text;
    $("#brand-sub").textContent = text;
  }

  function refreshAll() {
    var btn = $("#refresh");
    btn.disabled = true;
    btn.textContent = "正在拉取…";
    return post("/api/refresh").then(function () {
      return Promise.all([api("/api/overview"), api("/api/hosts"), api("/api/problems")]);
    }).then(function (res) {
      state.data.overview = res[0];
      state.data.hosts = res[1];
      state.data.problems = res[2];
      updateBadge(res[2].length);
      setConn(true, "已连接 · " + new Date().toLocaleTimeString("zh-CN"));
      return loadView(state.view);
    }).catch(function (e) {
      setConn(false, "连接失败：" + e.message);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "重新拉取数据";
    });
  }

  /* ---------------------------------------------------------- 对话 */
  function mdLite(text) {
    var lines = esc(text).split("\n");
    var out = [], inList = false;
    lines.forEach(function (line) {
      if (/^#\s+/.test(line)) { out.push("<h1>" + line.replace(/^#\s+/, "") + "</h1>"); return; }
      if (/^-\s+/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + line.replace(/^-\s+/, "") + "</li>");
        return;
      }
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(line);
    });
    if (inList) out.push("</ul>");
    return out.join("\n")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function chatBody() { return $("#chat-body"); }

  function scrollChat() {
    var body = chatBody();
    body.scrollTop = body.scrollHeight;
  }

  function appendUser(text) {
    var div = document.createElement("div");
    div.className = "msg user";
    div.innerHTML = '<div class="bubble">' + esc(text) + "</div>";
    chatBody().appendChild(div);
    scrollChat();
  }

  function appendBot(text, blocks) {
    var div = document.createElement("div");
    div.className = "msg bot";
    div.innerHTML = '<div class="bubble">' + mdLite(text || "") + "</div>" +
      (blocks && blocks.length ? '<div class="msg-blocks">' + blocksHtml(blocks) + "</div>" : "");
    chatBody().appendChild(div);
    scrollChat();
  }

  function appendTyping() {
    var div = document.createElement("div");
    div.className = "msg bot";
    div.innerHTML = '<div class="bubble typing"><i></i><i></i><i></i></div>';
    chatBody().appendChild(div);
    scrollChat();
    return div;
  }

  function renderSuggestions(list) {
    var box = $("#chat-sugg");
    box.innerHTML = "";
    (list || []).slice(0, 6).forEach(function (s) {
      var chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = s;
      chip.addEventListener("click", function () { sendChat(s); });
      box.appendChild(chip);
    });
  }

  function setChatBusy(busy) {
    state.chatBusy = busy;
    $("#chat-send").disabled = busy;
    $("#chat-input").disabled = busy;
  }

  function sendChat(text) {
    var message = (text || "").trim();
    if (!message || state.chatBusy) return;
    appendUser(message);
    $("#chat-input").value = "";
    setChatBusy(true);
    var typing = appendTyping();

    post("/api/chat", { message: message, session: state.session }).then(function (data) {
      state.session = data.session || {};
      typing.remove();
      appendBot(data.text, data.blocks);
      renderSuggestions(data.suggestions);
    }).catch(function (e) {
      typing.remove();
      appendBot("请求失败：" + e.message, []);
    }).then(function () {
      setChatBusy(false);
      $("#chat-input").focus();
    });
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bindEvents() {
    $("#nav").addEventListener("click", function (e) {
      var btn = e.target.closest(".nav-item");
      if (btn) switchView(btn.dataset.view);
    });

    $("#refresh").addEventListener("click", refreshAll);

    window.addEventListener("hashchange", function () {
      if (state.ignoreHash) { state.ignoreHash = false; return; }
      applyHash();
    });

    $("#chat-toggle").addEventListener("click", function () {
      var chat = $("#chat");
      chat.classList.toggle("hidden");
      this.textContent = chat.classList.contains("hidden") ? "显示对话" : "对话面板";
    });

    $("#chat-form").addEventListener("submit", function (e) {
      e.preventDefault();
      sendChat($("#chat-input").value);
    });

    $("#chat-clear").addEventListener("click", function () {
      chatBody().innerHTML = "";
      state.session = {};
      greet();
    });

    /* 内容区委托：主机跳转、返回列表、SNMP 实测、Grafana 看板 */
    $("#content").addEventListener("click", function (e) {
      var link = e.target.closest(".host-link");
      if (link) return switchView("host", link.dataset.host);

      if (e.target.id === "back-hosts") return switchView("hosts");

      var probe = e.target.closest(".probe-btn");
      if (probe) return runProbe(probe);

      var dash = e.target.closest(".dash-btn");
      if (dash) return runDashSync(dash);
    });
  }

  function contentNote(html) {
    var content = $("#content");
    var box = document.createElement("div");
    box.innerHTML = html;
    content.insertBefore(box.firstChild, content.firstChild);
  }

  function runDashSync(btn) {
    var host = btn.dataset.host;
    btn.disabled = true;
    btn.textContent = "生成中…";

    post("/api/grafana/sync", { host: host }).then(function (list) {
      var r = (list && list[0]) || {};
      if (!r.ok) throw new Error(r.error || "未知错误");
      btn.outerHTML = '<a class="btn ghost sm" target="_blank" rel="noopener" href="' +
        esc(r.url) + '">看板 (' + r.panels.length + ')</a>';
      return loadGrafana().then(function (g) { state.data.grafana = g; });
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = "生成看板";
      contentNote(noteBlock("Grafana 看板生成失败：" + esc(e.message), "warn"));
    });
  }

  function runProbe(btn) {
    var ip = btn.dataset.ip;
    var box = $("#probe-result");
    btn.disabled = true;
    btn.textContent = "探测中…";
    if (box) box.innerHTML = '<div class="loading">正在向 ' + esc(ip) + " 发送 SNMP 报文…</div>";

    post("/api/snmp/probe", { ip: ip, community: "public" }).then(function (r) {
      var tone = r.reachable ? "info" : "warn";
      var head = noteBlock(r.reachable
        ? "✅ " + ip + " SNMP 实测通过，" + r.ok_count + "/" + r.total + " 个 OID 有响应。"
        : "❌ " + ip + " SNMP 实测失败，设备无响应。", r.reachable ? "info" : "warn");
      var table = tableBlock(ip + " SNMP 实测结果", ["OID", "含义", "返回值"],
        r.results.map(function (x) { return [x.oid, x.label, x.value || x.error]; }));
      if (box) box.innerHTML = head + table;
    }).catch(function (err) {
      if (box) box.innerHTML = noteBlock("探测失败：" + err.message, "warn");
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "实测";
    });
  }

  /* ---------------------------------------------------------- 启动 */
  function greet() {
    appendBot("你好，我是 Zabbix 运维助手 👋\n直接问我监控情况就行，数据都是实时从 Zabbix 读的。", []);
    renderSuggestions(["整体情况怎么样？", "现在有哪些告警？", "R1 正常吗？",
      "R1 的接口状态", "R1 有没有采集失败的监控项", "帮我做一次巡检"]);
  }

  function tick() {
    $("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
  }

  /* 支持 ?q=问题 直达：打开页面自动发问，便于分享 / 外部跳转 */
  function autoAsk() {
    var m = /[?&]q=([^&]*)/.exec(location.search);
    if (!m) return;
    var q = decodeURIComponent(m[1].replace(/\+/g, " "));
    if (q) sendChat(q);
  }

  function init() {
    bindEvents();
    setConn(false, "正在连接…");
    greet();
    tick();
    setInterval(tick, 1000);

    Promise.all([api("/api/overview"), api("/api/hosts"), api("/api/problems")])
      .then(function (res) {
        state.data.overview = res[0];
        state.data.hosts = res[1];
        state.data.problems = res[2];
        updateBadge(res[2].length);
        setConn(true, "已连接 · Zabbix " + res[0].version);
        return applyHash();
      })
      .then(function () {
        autoAsk();
      })
      .catch(function (e) {
        setConn(false, "连接失败：" + e.message);
        $("#content").innerHTML = noteBlock("无法连接后端：" + e.message, "warn");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
