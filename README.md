# Zabbix 运维看板

面向网络运维的 Zabbix 轻量看板：**只读聚合 + 中文对话查询 + 对话式主机纳管 + Grafana 看板自动生成**。

后端纯 Python 标准库实现，前端原生 JS 零框架，**不需要 pip install，也不需要任何大模型或前端构建工具**，克隆下来配好 Token 就能跑。

```
Zabbix 7.x  ──►  webapp/server.py :8770  ──►  浏览器单页看板
                      │
                      ├─►  webapp/gdash.py  ──►  grafana/dashboards/zbx-*.json
                      │                              │
                      └─►  webapp/chat.py           └─►  Grafana 文件式 provisioning（30s 自动上线）
```

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 实时概览 | 主机数、活跃告警、模板数、采集健康度一屏掌握，数据直连 Zabbix API |
| 主机清单与详情 | 主机 / 接口 / 模板 / 监控项统计，主机名可点击下钻 |
| 活跃告警 | 按主机过滤，带严重级别配色与时间信息 |
| SNMP 实测 | 手写 BER/TLV 编解码，从本机直接发 SNMPv2c GET，用来判断「采不到」是设备侧问题还是 Zabbix 采集链路问题 |
| 中文对话助手 | 纯关键词意图 + 会话槽位状态机，不接大模型；支持主机名与「它 / 这台」代词回指 |
| 对话式纳管主机 | 识别意图 → 追问缺失参数 → 预检（IP 冲突、SNMP 连通性）→ 确认卡片 → 写库 → 回读验证 |
| Grafana 看板自动生成 | 按主机**实际存在**的监控项动态生成面板，写入本地产出目录即可上线，不调 Grafana API |
| Grafana 一键部署 | 自动下载 Grafana 与 Zabbix 插件、解压、装插件、生成数据源配置、启停管理 |
| 零依赖 | 无第三方 Python 包，无 npm 依赖，无构建步骤 |

## 目录结构

```
.
├── webapp/                 Web 看板服务（核心）
│   ├── server.py           HTTP 服务与 REST 路由（纯标准库，默认 8770）
│   ├── zbx.py              Zabbix JSON-RPC 客户端：Bearer 认证 + TTL 缓存 + 数据聚合
│   ├── chat.py             对话助手：关键词意图、卡片渲染、纳管状态机
│   ├── provision.py        主机纳管：建组 / 建主机 / 绑模板 / 配 SNMP 接口 / 设宏（幂等）
│   ├── gdash.py            Grafana 看板 JSON 生成器（按真实监控项动态装配）
│   ├── snmp_probe.py       设备侧 SNMPv2c 实测
│   └── static/             原生 JS / CSS / HTML 单页
├── grafana/
│   ├── grafana.py          Grafana + Zabbix 插件本地部署与启停脚本
│   └── dashboards/         自动生成的看板 JSON（运行期产物，不入库）
├── snmp_get.py             极简 SNMPv2c GET 库（也是命令行工具）
├── zabbix_probe.py         Zabbix API 连通性探测 + 只读快照
├── zabbix_add_host.py      命令行版纳管脚本（对话式纳管的前身）
├── cisco_telnet.py         Telnet 登录 Cisco 设备批量执行命令
├── .env.example            配置样例
└── README.md
```

## 快速开始

### 1. 前置条件

- Zabbix Server **7.0+**（使用 API Token 认证）
- Python **3.8+**（`cisco_telnet.py` 依赖 `telnetlib`，需 Python ≤ 3.12）

### 2. 创建 API Token

Zabbix Web → **Users** → 选中用户 → **API tokens** → **Create API token**，记下 Token（只显示一次）。

### 3. 配置

```bash
cp .env.example .env
```

```ini
ZABBIX_URL=http://192.168.1.10
ZABBIX_TOKEN=your-api-token
```

> `.env` 已被 `.gitignore` 忽略，不会进版本库。

### 4. 启动

```bash
python webapp/server.py          # 默认 8770，端口被占用会自动顺延
python webapp/server.py 9000     # 也可以指定端口
```

```
==========================================================
 Zabbix 运维看板
==========================================================
 Zabbix API : http://192.168.1.10
 API 版本   : 7.0.27
 纳管主机   : 12 台   活跃告警: 3 条
----------------------------------------------------------
 看板地址   : http://127.0.0.1:8770
 停止服务   : Ctrl+C
==========================================================
```

浏览器打开 <http://127.0.0.1:8770> 即可。

Windows 下后台常驻：

```powershell
Start-Process python -ArgumentList 'webapp/server.py' -WindowStyle Hidden `
  -RedirectStandardOutput webapp/server.out.log -RedirectStandardError webapp/server.err.log
```

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ZABBIX_URL` | 无（必填） | Zabbix 站点根地址，如 `http://192.168.1.10` |
| `ZABBIX_TOKEN` | 无（必填） | Zabbix 7 API Token，走 `Authorization: Bearer` |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | 生成看板后拼接访问链接用 |
| `DASH_HOST` | `127.0.0.1` | Web 服务监听地址 |

`.env` 优先从仓库根目录读取，其次 `webapp/.env`。

## 界面

左侧导航四个视图，右侧是可收起的对话面板：

- **概览**：主机总数、告警数、采集状态分布、最近告警
- **主机**：主机列表，点击主机名进入详情（接口 / 监控项 / 不支持项 / 最新值）
- **告警**：活跃问题列表，导航项上带告警数角标
- **SNMP 实测**：填 IP 与 community，服务端发真实 SNMP 报文并回显 OID 结果

地址栏支持 hash 直链（`#hosts`、`#problems`、`#host/R1`）与浏览器前进后退；`?q=问题` 可以打开页面后自动提问，方便把某次查询直接分享给同事。

## 对话助手

问答是**纯规则**的：关键词命中意图后走对应的取数逻辑，没有模型调用、没有外发请求，取舍是"可控、可预测、零成本"。支持的问法示例：

| 你可以问 | 行为 |
| --- | --- |
| `帮助` / `你能做什么` | 输出能力清单与示例问法 |
| `R1 正常吗` / `它现在怎么样` | 单机状态卡片（含代词回指上一台主机） |
| `有哪些告警` / `故障` | 活跃告警列表，可按主机过滤 |
| `接口状态` / `链路` | 该主机接口 UP/DOWN、速率与流量 |
| `哪些监控项没采到` | 不支持的监控项（unsupported）清单 |
| `CPU 使用率` / `监控项` | 监控项实时值 |
| `SNMP 通不通` / `测一下 192.168.1.20` | 现场发 SNMP 报文实测 |
| `有哪些模板` | 模板列表 |
| `概览` / `整体情况` | 全局统计 |
| `生成 R1 的看板` | 生成 / 刷新 Grafana 看板并返回链接 |
| `巡检 R1` / `出个报告` | 主机巡检报告 |
| `把 SW1 192.168.1.30 加进来` | 进入纳管流程 |

主机识别支持主机名、显示名、接口 IP，按最长匹配；只提到「它 / 这台 / 刚才那台」时沿用会话里最近一次的主机。

## 主机纳管（唯一的写操作）

对话里发起（例：`添加一台设备 192.168.1.30 叫 SW1，团体名用 public`）后走六步：

1. **识别**：命中纳管触发词 + 提取已有的槽位（名称、IP、community、端口、模板、主机组）
2. **追问**：缺什么问什么，一次只问缺的
3. **预检**：检查 IP 是否已被占用；用真实 SNMP GET 验证 community 是否可用
4. **确认**：输出确认卡片（建什么组、绑什么模板、设什么宏），明确回复确认才执行
5. **执行**：建主机组 → 建主机 → 配 SNMP 接口 → 绑模板 → 设 `{$SNMP_COMMUNITY}` 宏
6. **回读**：重新查询校验结果，成功后自动生成该主机的 Grafana 看板

预检不通过时不会静默降级，必须显式回复「强制确认」才继续；同名主机已存在会自动转为修改流程。整体是幂等的：重复执行同一份描述不会重复建组或重复绑模板。

## Grafana 看板

### 生成方式

三种入口，效果一致：

- 界面上的「生成看板」按钮
- 接口 `POST /api/grafana/sync`（`{"host": "R1"}`，省略 host 为全量同步）
- 对话里说「生成 R1 的看板」

生成的看板：

- uid / 文件名：`zbx-<slug>`，标题 `<主机> · 接口与链路`
- 文件夹：`网络监控`；数据源：`Zabbix`（uid 自动探测，不硬编码）
- 面板按主机**真实存在**的监控项动态生成，换厂商不会出现空白面板：
  - 单值指标行（设备名、系统版本、运行时间、SNMP/Agent 采集状态、ICMP 丢包与响应、接口数、型号、序列号，每行 ≤6 格）
  - 接口流量 `timeseries`（收发双向、接收反向轴）
  - 接口错包 / 丢包、ICMP 响应时间趋势
  - 接口 Operational 状态与速率
  - 活跃告警表（Zabbix Triggers 面板，带 severity 配色）

### 部署 Grafana

```bash
python grafana/grafana.py setup     # 下载 Grafana + Zabbix 插件、解压、装插件、生成数据源
python grafana/grafana.py start     # 启动，默认 127.0.0.1:3000
python grafana/grafana.py status
python grafana/grafana.py stop
python grafana/grafana.py restart --port 3000
```

默认账号 `admin` / `admin`，仅监听 `127.0.0.1`；可用环境变量 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` 覆盖。`setup` 会优先从镜像下载（支持断点续传），已把安装包放进 `grafana/dist/` 则直接使用本地包。

看板通过**文件式 provisioning** 装载：`grafana/provisioning/dashboards/default.yml` 指向 `grafana/dashboards/`，Grafana 每 30 秒扫描一次。因此生成看板不需要 Grafana 登录态、不需要 API Token，Grafana 没启动也不影响生成；删掉 JSON 文件即删除看板。

> 看板是「文件即真相」：每次同步整文件原子重写，Grafana 界面上的手工修改会在下次同步时被覆盖（看板描述里也写了这条提醒）。要固化改动，请改 `webapp/gdash.py`。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` | 总览统计 |
| GET | `/api/hosts` | 主机列表（含接口、模板、监控项数、告警数） |
| GET | `/api/host?name=R1` | 单主机详情 |
| GET | `/api/problems[?host=R1]` | 活跃告警 |
| GET | `/api/templates` | 模板列表 |
| GET | `/api/snmp` | SNMP 接口清单 |
| GET | `/api/grafana` | 已生成的看板与 Grafana 地址 |
| POST | `/api/chat` | 对话，body `{"message": "...", "session": {}}` |
| POST | `/api/snmp/probe` | 设备侧 SNMP 实测，body `{"ip": "...", "community": "public"}` |
| POST | `/api/grafana/sync` | 生成 / 刷新看板，body `{"host": "R1"}`，省略为全量 |
| POST | `/api/refresh` | 清缓存后重新拉取 |

统一返回 `{"ok": true, "data": ...}`，失败为 `{"ok": false, "error": "..."}`。

## 辅助脚本

| 脚本 | 用途 |
| --- | --- |
| `snmp_get.py` | 不依赖任何库的 SNMPv2c GET（手写 BER/TLV），可单独当命令行工具用 |
| `zabbix_probe.py` | Zabbix API 连通性探测与全量只读采集，输出 JSON 快照 |
| `zabbix_add_host.py` | 命令行版纳管（`add` / `check`），参数在文件头，适合批量脚本化 |
| `cisco_telnet.py` | Telnet 登录 Cisco 设备按提示符批量执行命令并回显（`--enable` 进特权模式） |

## 已知限制

- **只读为主**：除主机纳管外不提供修改 Zabbix 的能力
- **无鉴权**：Web 服务默认只监听 `127.0.0.1`，要对外暴露请自行加反向代理与认证
- **单实例缓存**：数据缓存是进程内内存字典（主机 5s / 模板 60s / 版本 120s），多实例之间不共享
- **Python 3.13**：`telnetlib` 已从标准库移除，`cisco_telnet.py` 需 Python ≤ 3.12（其余模块不受影响）
- **Grafana 看板**：不做双向合并，UI 改动会被下一次同步覆盖
- **新增接口**：SNMP 接口依赖 LLD 发现，新纳管主机首次生成看板时接口可能为空，程序会在约 80 秒后自动补一次同步

## 安全

- `.env`、`grafana/provisioning/`（内含明文 API Token）、`grafana/data/`、设备配置与探测输出均已写入 `.gitignore`
- 提交前请自行确认没有把真实内网地址、Token、设备配置带进仓库
- 如历史提交中误入密钥，请立即在 Zabbix 侧吊销 Token，而不是只删文件

## License

[MIT](LICENSE)
