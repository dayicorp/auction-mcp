# auction-mcp

![MCP](https://img.shields.io/badge/MCP-server-7C3AED)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-240%20passed%20%7C%2021%20skipped-brightgreen)
![Stars](https://img.shields.io/github/stars/dayicorp/auction-mcp?style=flat&label=★)

司法拍卖实时查询 MCP server — **阿里拍卖 + 京东拍卖** 双端聚合，并提供江门贝壳小区挂牌参考。默认拍卖查询使用纯 Python httpx；浏览器能力均采用明确、隔离且 fail-closed 的交互链路。

## 解决什么问题

司法拍卖数据散落在阿里和京东**两个独立池**, 各自有 sign / 反爬 / 编码 vintage 错位 (阿里区县编码用 pre-2013, 现代数据集会**静默返全国乱掺垃圾**而不报错) / 价格单位不一 (阿里分 / 京东元) 等坑。LLM agent 想"查个法拍数据"自己面对这些坑会撞墙。

这个 MCP 把它们全在工具层封掉, **暴露给 agent 的就是干净的中文名 + 统一字段**:

```python
search_judicial(province="广东", city="深圳市", district="福田区")
# → 双端并行打完, 价格降序, 单位归一到元, 每条带 platform 标源
```

## 核心特性

- **双端聚合, 一次拿全量** — `search_judicial` 并行打阿里+京东, 价格降序合并, 单位归一到元, 加 `platform` 字段标源
- **三级地区精准查询** — 全国 31 省 / 3146 区县 (阿里) + 33 省 / 5344 区县 (京东), 中文名直传
- **地区参数 fail-closed** — 层级缺失或地区无法解析时, 在请求 Ali/JD provider 前返回结构化错误
- **反爬守门** — 阿里 server 不认编码时会静默返全国乱掺垃圾, 工具自动校验拒绝
- **常驻自愈** — `_m_h5_tk` cookie 过期自动重 bootstrap; baxia 风控 HTML 返结构化错误不崩
- **PC 完整筛选与详情适配器 (Experimental)** — 可选非持久化 Chrome 会话实现关键词、价格、开始时间、页面动态筛选和单拍品详情读取；登录/验证由用户手动完成
- **江门贝壳小区 Provider** — 只附加现有本机 CDP 页面，搜索标准小区、读取详情和第一主挂牌列表；推荐区、广告、登录和验证码均严格隔离
- **261 项 pytest 测试** — 240 项离线通过 + 21 项 Live 默认跳过

## Quick start

从仓库构建并安装正式消费端入口：

```bash
python -m pip install .
auction-mcp
```

注册到 Claude (`~/.claude.json` 或 `claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "auction": {
      "type": "stdio",
      "command": "auction-mcp"
    }
  }
}
```

源码开发时仍可使用 `python /path/to/auction-mcp/server.py`；正式消费端验收以
wheel 安装后的 `auction-mcp` 控制台入口为准，不依赖仓库 cwd 或 `PYTHONPATH`。

```bash
pip install -r requirements.txt    # mcp 1.x, httpx, playwright, pytest, coverage
pytest                             # 单元 + 容错 (零网络)
pytest --run-live                  # + 全部显式 Live 集成
pytest tests/test_live_beike.py --run-live  # 仅复验已有贝壳 CDP 会话
```

### 自动化 CI 与本地发布门禁

GitHub Actions 在 Pull Request、`main` 推送和手动触发时运行九个任务：六个 clean-room 发布矩阵覆盖 Windows/Linux 与 Python 3.10、3.12、3.14；两个 Windows/Linux Python 3.12 主动压力任务；一个 Linux Python 3.12 覆盖率与变异任务。每个 clean-room 任务都在仓库外创建全新临时 venv、安装运行与固定版本构建依赖、执行 `pip check`，再从任意非仓库 cwd 运行统一门禁；不会复用 runner 或开发者的既有环境。工作流权限固定为 `contents: read`，同一分支的新运行会取消旧运行，所有任务都有固定超时。所有外部 Action 固定到40位不可变提交 SHA，checkout 不保留Git凭据；Dependabot每周检查GitHub Actions和根目录Python依赖，最多同时提出5个更新PR。

本地执行与 CI 完全相同的 clean-room 验收（只要求系统 Python，不复用项目 `.venv`）：

```bash
python scripts/verify_cleanroom.py
```

bootstrapper 会创建临时 venv、全新安装依赖并运行 `pip check`，随后调用 `scripts/verify_release.py`。统一门禁完成所有受 Git 跟踪的 Python 文件编译、进程内精确 15 工具注册，并通过官方 MCP 客户端从仓库外 cwd 启动真实 `server.py`，完成 stdio `initialize`、`tools/list`、精确参数 Schema 契约、静态地区读取、无效输入 fail-closed、浏览器未启动状态、异常退出、超时清理和 stderr 安全检查。消费端子进程会加载临时网络阻断器，任何意外 socket 连接都会立即失败。门禁还检查依赖版本、敏感信息与禁止产物、Action不可变SHA，并运行全部离线 pytest。

随后 `scripts/verify_artifact.py` 从显式源码白名单复制出两个独立构建树，以固定
`SOURCE_DATE_EPOCH` 各构建一次 wheel 和 sdist；仓库内 PEP 517 后端会进一步
规范化 sdist 的 gzip/tar 时间、所有者和成员顺序，要求同类制品 SHA-256
逐字节一致，并以精确成员白名单审计归档内容、元数据、Python 版本、运行依赖、MIT 许可证、
`auction-mcp = server:main` 入口及四份运行数据/公开契约。任何 tests、scripts、
Live 文件、缓存、密钥或浏览器状态进入制品都会失败。最终 wheel 被安装到第三个
全新 venv，重新执行 `pip check`，从仓库外 cwd 通过安装后的控制台入口完成断网
MCP 握手、15 工具 Schema 与六个安全离线调用，并证明 `server` 和数据资源没有
从源码 checkout 泄漏。整个流程会清空外部 `PYTEST_ADDOPTS`，不会传入
`--run-live`、下载或启动浏览器，也不会读取、导出或保存 Cookie。

P3.4 额外可靠性门禁均输出单行机器可读 JSON，且不使用纯 `sleep` 伪造长稳时间：

```bash
# 原始 JSON-RPC 混沌 + 两轮(100 次顺序 + 8 路×20 次并发)，共 527 个真实进程生命周期
python scripts/runtime_chaos.py --mode all

# 冻结于 ec4e050 基线；语句≥75.5%、分支≥66.2%，fail-closed 核心两者均为100%
python scripts/coverage_gate.py

# 12 个强制安全变异；任何幸存、超时或基础设施退出均失败
python scripts/mutation_gate.py
```

原始客户端覆盖分片写入、连续请求、未知 method、无效工具参数、损坏 JSON、就绪后的下一请求提前 EOF 和客户端超时。EOF 的退出时钟在完整握手后开始，避免把冷解释器启动抖动误判为协议泄漏；10秒阈值、Job/进程组清空和 guard 注销要求保持不变。每个进程都从仓库外 cwd 启动，在 `sitecustomize` 强制断网下完成握手/工具列表/安全离线调用，验证 stdout 仅含 JSON-RPC、stderr 小于 64 KiB 且不含敏感模式，并在结束后回收进程、管道、线程与临时目录。Windows 使用每生命周期独立 Job Object 验证内核成员归零，Linux 使用独立 session/process group；两者都不依赖可能被系统复用的历史 PID 快照。压力门禁记录冷启动、8线程并发设施预热后及两轮完整压力后的资源快照；每轮继续使用原 `+8` 上限，第二轮不得在稳定基线之上继续增长。故障字段和处置见 [`docs/reliability.md`](docs/reliability.md)。

## 工具 (15 个)

| 工具 | 参数 | 说明 |
|---|---|---|
| ⭐ **`search_judicial`** | `province?`, `city?`, `district?`, `page=1`, `limit=20` | **推荐默认调用** — 并行查 阿里+京东, 价格降序合并, 单位归一到元 |
| `ali_search_judicial` | `province?`, `city?`, `district?`, `page=1`, `fcat_v4_names?`, `fcat_v4_ids?`, `circs?`, `tag_ids?`, `zc_biz_types?` | [Advanced 单源] 仅查阿里；分类可直接传中文名，其余编码来自 `ali_get_filter_options` |
| `ali_get_supported_areas` | `province?`, `city?` | 列阿里支持的省/市/区县中文名 |
| `ali_get_filter_options` | (无) | 阿里 9 个 filter 维度的完整可选项 |
| `ali_pc_browser_start` | (无) | 启动非持久化可见 Chrome；用户手动登录或验证 |
| `ali_pc_browser_status` | (无) | 检查 PC 会话登录/验证状态 |
| `ali_pc_get_filter_options` | `category?`, `province?`, `city?` | 从当前真实 PC DOM 动态读取链接、下拉框和输入控件能力 |
| `ali_pc_search_judicial` | `keyword?`, 分类/地区/资产类型/排序/状态/阶段?, 价格?, 开始日期?, `page=1` | [Interactive Experimental] 阿里 PC 完整筛选；页码限 1-5；关键词不可与其他筛选混用 |
| `ali_pc_get_item_detail` | `item_id` | [Interactive Experimental] 读取一个搜索结果的 PC 详情；核验固定详情域名/ID，并等待正文、金额、状态、周期和附件完成异步加载 |
| `ali_pc_browser_close` | (无) | 关闭 PC 会话并销毁进程内登录态 |
| `jd_search_judicial` | `province?`, `city?`, `district?`, `page=1` | [Advanced 单源] 仅查京东 |
| `jd_get_supported_areas` | `province?`, `city?` | 列京东支持的省/市/区县中文名 |
| `beike_browser_status` | (无) | 检查现有江门贝壳 CDP 页面、域名和验证码边界，不读取浏览器凭据或存储 |
| `beike_search_xiaoqu` | `city`, `keyword` | 通过真实键盘事件触发自动补全，只返回标准小区候选；当前仅支持江门市 |
| `beike_get_xiaoqu_market` | `city`, `xiaoqu_id`, `limit=30` | 读取小区详情及第一非 VIEWDATA 主挂牌列表，返回逐套价格和最小/最大/中位数 |

> 阿里和京东是**两个独立标的池, 不重复**: 阿里偏机构端高价资产 (亿级土地/在建工程), 京东偏散户端住宅/股权/小额债权. 默认调 `search_judicial` 拿双端聚合.

### 江门贝壳小区挂牌参考

贝壳工具不会启动、关闭或重启 Chrome，也不会新建页面或浏览器上下文。它只连接
`AUCTION_MCP_BEIKE_CDP_URL`（默认 `http://127.0.0.1:9222`，仅允许本机无凭据 HTTP
地址）并复用已经打开的 `jiangmen.ke.com` 页面；不会调用 Cookie、localStorage、
Token 或 `storage_state` API。使用顺序：

```python
beike_browser_status()
candidates = beike_search_xiaoqu(city="江门市", keyword="江海花园")
# 匹配决定由调用方完成；不要用附近小区或区域均价替代
beike_get_xiaoqu_market(
    city="江门市",
    xiaoqu_id=candidates["candidates"][0]["xiaoqu_id"],
    limit=30,
)
```

挂牌页采用当前贝壳规范路由 `/ershoufang/c{xiaoqu_id}/`；旧能力地图中的
`/xiaoqu/{id}/esf/` 目前会重定向回详情页，Provider 会拒绝把该重定向当成挂牌结果。
只接受唯一的 `ul.sellListContent:not(.VIEWDATA)`，卡片字段缺失、列表不存在或不唯一
都会失败，不会从“猜你喜欢”或广告补足。

故障代码包括 `BEIKE_BROWSER_DISCONNECTED`、`BEIKE_CAPTCHA_REQUIRED`、
`BEIKE_LOGIN_REQUIRED`、`BEIKE_UNSUPPORTED_CITY`、`BEIKE_INVALID_XIAOQU_ID`、
`BEIKE_NO_MATCH`、`BEIKE_DOM_CONTRACT_DRIFT`、`BEIKE_NAVIGATION_TIMEOUT` 和
`BEIKE_PRIMARY_LIST_MISSING`。遇到登录或验证码时保持现有页面，由用户手动处理后再
重试；不得把 CDP 地址作为工具参数，也不得导出浏览器状态。

### 阿里 PC 完整筛选

当用户明确要求关键词、价格区间、开始时间、任意状态/阶段等 PC 页面能力时，按以下生命周期调用：

```python
ali_pc_browser_start()
# 用户只在弹出的 Chrome 中手动完成登录/验证码；随后检查状态
ali_pc_browser_status()
# 先读取当前真实页面提供的精确中文筛选值
ali_pc_get_filter_options(category="住宅用房", province="广东", city="江门")

result = ali_pc_search_judicial(
    category="住宅用房",
    province="广东",
    city="江门",
    max_price_yuan=210000,
    auction_start_from="2026-08-01",
    auction_start_to="2026-09-01",
    status="正在进行",
    page=2,
)

# item_id 必须直接来自已验收搜索结果
ali_pc_get_item_detail(item_id=result["items"][0]["itemId"])

ali_pc_browser_close()
```

PC 适配器只使用浏览器进程内会话：不调用 Cookie 读取接口、不导出 `storage_state`、不指定用户数据目录。遇到登录、滑块、二维码或风控页时返回 `action_required`，不会自动绕过。真实页面会在关键词搜索时清空其他筛选，因此 `keyword` 与分类、地区、价格、日期等组合会 fail-closed。详情工具只允许 8–20 位数字 `item_id` 和固定 `sf-item.taobao.com/sf_item/{item_id}.htm`；正文或附件仍显示“加载中”时拒绝返回不完整数据。

一次性执行固定 P2.4 PC Live 验收（住宅用房 / 广东 / 江门 / 21 万元以下 / 2026-08-01 至 2026-09-01）：

```powershell
.\.venv\Scripts\python.exe scripts\manual_live_pc.py
```

脚本会打开一个全新的非持久化 Chrome 会话；它不会复用现有 Chrome 用户目录或读取其中的 Cookie。若页面要求登录、滑块或二维码，只需在弹出的窗口手动完成后回到终端按 Enter。脚本随后自动查询，并输出不含 Cookie 和原始页面正文的 `PC_LIVE_ACCEPTANCE` JSON。验收通过时自动关闭会话；失败时保留浏览器供人工检查，确认后按 Enter 关闭。

一次登录执行 P2.5 完整筛选能力矩阵（动态能力读取 + 关键词/区县/资产类型/排序/状态/阶段）：

```powershell
.\.venv\Scripts\python.exe scripts\manual_live_pc_matrix.py
```

矩阵只使用页面实时返回的选项，场景之间默认等待 10 秒；遇到登录、验证码、滑块、风控或任一筛选应用失败会立即停止并保留浏览器供检查。

为降低风控概率，默认场景间隔为 10 秒；可只续跑尚未验收的短批次，不会保存 Cookie 或本地检查点：

```powershell
.\.venv\Scripts\python.exe scripts\manual_live_pc_matrix.py --scenarios status,stage --delay-seconds 10
```

P2.6 分页协议发现必须从用户可见的 PowerShell 启动，避免浏览器生命周期绑定到 Codex 临时工具进程：

```powershell
.\.venv\Scripts\python.exe scripts\manual_live_pc_pagination.py
```

脚本会在人工登录后等待用户按 Enter；唯一识别真实“下一页”控件后，只有输入完整口令 `TURN` 才执行一次翻页，并核验两页 URL、页码指示和标的 ID 重叠。无论成功、失败或异常，脚本都会先输出 `PC_PAGINATION_DISCOVERY`，随后无限等待；只有输入完整口令 `CLOSE` 才关闭浏览器，其他输入均继续保持，不设置自动关闭超时。

P2.7 正式 `page=2` Live 验收同样必须从用户可见的 PowerShell 启动：

```powershell
.\.venv\Scripts\python.exe scripts\manual_live_pc_page2.py
```

脚本使用正式 `AliPCBrowserClient.search(page=2)` 链路：从第一页开始，只点击指向预期页码且唯一可见的真实 DOM “下一页”控件，并同时核验页码指示递增、URL `page=2`、结果集合变化及筛选保持。出现登录、滑块、二维码或风控时等待用户人工处理；输出 `PC_PAGE2_ACCEPTANCE` 后无限保持浏览器，只有输入完整口令 `CLOSE` 才关闭。全程不读取、导出或保存 Cookie。

## 用法示例

```python
# 全国 top 20 (两端并行 + 价格降序)
search_judicial(limit=20)

# 省级 / 市级 / 区县级 — 全都传中文名
search_judicial(province="北京")
search_judicial(province="上海", city="上海市")
search_judicial(province="四川", city="成都市", district="武侯区")
```

返回 envelope:
```jsonc
{
  "count": 10,
  "ali_totalCount": 1234,
  "jd_count": 40,
  "sources": ["ali", "jd"],
  "items": [
    {
      "platform": "ali",          // 或 "jd"
      "id": 1049000000000,
      "title": "<拍品标题>",
      "price_yuan": 12345678.0,   // 归一到元 (阿里原值是分, 自动 ÷100)
      "raw": { /* 原始 item, 含 locationCode / shopName / 法院 / 状态 / 时间 等 */ }
    }
  ]
}
```

## ⚠️ 阿里区县编码 vintage 坑

阿里 server 用的是 **pre-2013 GB 2260 编码**, 跟现代 (2020 版) 不一样。2013 年大量县/县级市改区时编码也跟着变了, 但阿里 server 还认旧码。

如果你绕过 `district=` 参数自己拼现代编码塞给 `location_codes`, 阿里**不报错, 静默返回十几万条全国乱掺数据**。所以:

- ✅ **永远用 `district="<区县中文名>"`** 让工具自己解析
- ❌ 不要自己从 `ali_get_supported_areas` 拿 code 再传 `location_codes` (那是 2020 版仅供人类参考)
- 即便误用, **垃圾结果守门**也会捕获并返 `error: ali_returned_unscoped_results`, 不会让你拿到垃圾数据

<details>
<summary>技术实现细节</summary>

项目 bundle 了 `gb2260_200712.json` (`cn/gb2260` 200712 快照) 作为阿里查询专用 vintage。命名变更类的区县 (2013 改名导致 legacy 数据集找不到的, 例如绍兴县→柯桥区) 通过 `learn_district_code_from_city` 从城市级 item title 反查动态学码, 学到后写进程缓存。

**行为约定**:
- 默认 H5/双端聚合排序固定价格降序、状态固定仅"进行中"+"即将开始"；显式 PC 浏览器工具可使用页面提供的其他筛选
- 公开 MCP 工具对层级缺失或无法解析的地区 fail-closed, 返回结构化错误且 provider 调用为 0
- `JDH5Client._build_area_params` 底层仍保留 silent skip, 仅作为内部防御, 不代表公开工具会扩大查询范围
</details>

<details>
<summary>项目结构 + 架构来由</summary>

```
auction-mcp/
├── server.py            # FastMCP server, 15 个 @mcp.tool()
├── ali_h5_client.py     # 阿里 H5 mtop client + 双 vintage 解析 + 守门 + 动态学码
├── ali_pc_browser_client.py # 非持久化 Chrome PC 完整筛选适配器
├── beike_browser_client.py # 附加现有CDP页面的江门贝壳小区Provider
├── jd_h5_client.py      # 京东 m. 版 client + 全国地区树查询
├── auction_mcp_assets/  # wheel内只读运行数据与15工具公开契约
│   ├── gb2260.json      # GB 2260 2020 版 (展示用, 不用于查询)
│   ├── gb2260_200712.json # GB 2260 pre-2013 (阿里查询 vintage)
│   ├── jd_areas.json    # 京东 33 省/455 市/5344 区县地区树
│   └── mcp_contract.json # 15 工具公开参数 Schema 契约
├── pyproject.toml       # wheel/sdist元数据与auction-mcp控制台入口
├── .github/workflows/ci.yml # Windows/Linux × Python 3.10/3.12/3.14 离线 CI
├── scripts/consumer_probe.py # 仓库外 cwd 的真实 MCP 消费端与故障生命周期探针
├── scripts/verify_cleanroom.py # 全新 venv 安装、pip check 与统一门禁入口
├── scripts/verify_artifact.py # 双构建复现、归档审计、wheel安装与真实消费探针
├── scripts/verify_release.py # 编译/依赖/安全/Schema/stdio/消费端/离线测试门禁
├── scripts/manual_live_pc.py # 一次性交互式 PC Live 验收入口
├── scripts/manual_live_pc_matrix.py # 一次登录多场景 PC Live 矩阵
├── scripts/manual_live_pc_pagination.py # 人工确认关闭的 PC 分页协议发现
├── scripts/manual_live_pc_page2.py # 正式 page=2 交互式 PC Live 验收
├── safety_core.py       # 可独立达到100%分支覆盖的fail-closed纯函数核心
├── coverage_contract.json # 覆盖率基线、阈值与关键文件契约
├── scripts/runtime_chaos.py # 原始stdio混沌与527生命周期跨平台压力门禁
├── scripts/coverage_gate.py # 离线语句/分支覆盖率门禁
├── scripts/mutation_gate.py # 12个安全关键确定性变异门禁
├── docs/reliability.md  # 机器诊断字段与故障定位
└── tests/               # 261 项 pytest (240 项离线 + 21 项 Live 默认跳过)
```

### 为什么默认链路是纯 httpx

v1 想走"app 端真机 sign 桥" — IDA 逆向 + Substrate tweak + SSH 隧道, iPad/Android 两条路都被 anti-tamper SDK (unifiedSign + wua + sgext) 全面挡死。

v2 转向走移动浏览器 H5 mtop:
- **阿里**: `h5api.m.taobao.com/h5/mtop.taobao.datafront.invoke.auctionwalle/1.0/`, sign 是公开 MD5: `MD5(token + "&" + t + "&" + appKey + "&" + data)`, 其中 `token = _m_h5_tk` cookie 前 32 字符. 跟 app 端**同一 endpoint 同一数据源**.
- **京东**: `api.m.jd.com/api?functionId=getSearchData`, 风控字段 `h5st` / `x-api-eid-token` **可省略**, server 接受裸 form-encoded body. 250 万+ 司法拍卖标的, 16 类目.
- **地区树**: 阿里走国标 GB 2260 (前 2 省 + 中 2 市 + 末 2 区 zero-pad), 但用 pre-2013 vintage; 京东走自家 `getAreaInfoMap` cascade API 一次性抓全国树落本地.

完全 bypass app 端 anti-tamper.

PC 页面独有的关键词、价格与开始时间参数会被 H5 mtop 静默忽略，因此这些能力没有伪装成 H5 参数，而是隔离在显式的非持久化 Playwright 会话中。
</details>

## Roadmap

- [x] **P3.7 江门贝壳 Provider 正式集成** — 复用现有本机 CDP 页面完成标准小区候选、详情和第一主挂牌列表；历史江海花园16套/中位6306、奥园外滩30套/中位8515及五福村NO_MATCH固化为回归；原12工具Schema不变，总工具数15；登录、验证码、推荐区和浏览器存储均fail-closed
- [x] **P3.6 可发布制品与真实消费端安装闭环** — 两个独立源码树各构建 wheel/sdist 并要求固定 epoch 下同类 SHA-256 逐字节一致；审计制品只包含运行模块、四份只读数据/契约、入口和许可证；第三个全新 venv 从 wheel 安装依赖并 `pip check`，再从仓库外 cwd 通过 `auction-mcp` 入口完成断网 initialize、精确12工具 Schema、五个安全调用、失败生命周期与源码泄漏检查；六个 Windows/Linux × Python 3.10/3.12/3.14 clean-room 任务全部复现
- [x] **P3.4 运行时混沌、压力、覆盖与变异闭环** — 原始stdio客户端验证七类协议/退出/恢复边界；Windows/Linux各运行100次顺序与8×20次并发生命周期；覆盖门禁从 `ec4e050` 真实测得语句75.597%、分支66.25%并冻结不低于75.5%/66.2%，fail-closed核心两者100%；12个地区、参数、Schema、断网和默认值安全变异必须全部被测试杀死；CI扩展为九个有界任务
- [x] **P3.3 clean-room 消费端可靠性** — 每个 CI 矩阵任务从系统 Python 创建仓库外临时 venv，全新安装依赖并执行 `pip check`；独立消费者从非仓库 cwd 完成真实 MCP 握手、12 工具参数 Schema、五个强制断网离线调用、无效输入、异常退出、超时清理和 stderr 边界验证。临时环境在结束后销毁，不复用项目 `.venv`，不下载或启动浏览器
- [x] **P3.2 MCP stdio 启动可靠性** — 发布门禁除进程内工具清单外，使用官方 MCP 客户端真实启动 `server.py`，在 Windows/Linux 和 Python 3.10/3.12/3.14 上验证 stdio 初始化及精确 12 工具 `tools/list`；启动、协议、超时或工具漂移均 fail-closed，全程离线且不启动浏览器
- [x] **P2.13-A 远端消费端依赖修正** — 2026-08-02 在全新 Python 3.14.6 环境中发现 `mcp>=1.0` 会解析到不兼容的 MCP 2.0（已移除 `mcp.server.fastmcp`）；依赖范围收紧为 `mcp>=1.0,<2`，并增加发布依赖契约测试，防止干净安装再次跨越不兼容主版本
- [x] **P2.9-F PC 拍品详情状态修正后 Live 复验** — 2026-08-01 在加载 `84f96b6` 的新非持久化登录会话中，对标的 `1062507630078` 唯一一次正式调用通过：状态规范化为“即将开始”，起拍价、评估价、保证金、加价幅度、拍卖阶段、两个周期、联系人、正文和固定详情 URL 均与真实页面一致，金额、状态、周期、正文、附件和 URL 六项 ready 门禁全部通过；Cookie 未读取、导出或保存，会话关闭后临时配置目录已清理
- [x] **P2.9-E PC 拍品详情状态语义修正** — 2026-08-01 将真实可见“距开始/距离开始/距开拍/即将开拍”严格规范化为“即将开始”，将“距结束/距离结束”规范化为“正在进行”；只有日期、裸倒计时数字或“即将”等歧义文本继续 fail-closed。151 项离线测试通过，20 项 Live 保持跳过
- [x] **P2.9-D PC 拍品详情修正后 Live（验收 FAIL）** — 2026-08-01 唯一一次调用中金额、周期、正文、附件和 URL 五项 ready 门禁均通过，仅 `status_ready=false`；工具正确返回 `pc_detail_content_not_ready`，没有再次输出半完整详情或假通过
- [x] **P2.9-C PC 拍品详情解析修正** — 2026-08-01 针对 P2.9-B 真实失败样本修复：DOM 候选优先选择标签后的实际值；金额支持跳过裸标签并回退“标的询价平均值”；状态从独立可见节点读取；周期只接受带单位的数值；联系人缺失时返回 null；ready 门禁必须同时具备实际金额、状态和两个周期值。143 项离线测试通过，20 项 Live 保持跳过
- [x] **P2.9-B PC 拍品详情首次正式 Live（验收 FAIL）** — 2026-08-01 唯一一次调用正确进入标的 `1062507630078`，URL/正文/异步容器/Cookie 边界通过；但捕获到金额全为 null、联系人误解析为冒号、周期只有标签、状态为 null，否决最小验收器的假通过并进入 P2.9-C 修正
- [x] **P2.8 PC 拍品详情协议发现** — 2026-08-01 从已验收江门住宅列表唯一进入 `sf-item.taobao.com/sf_item/1062507630078.htm`，确认可见 DOM 包含阶段、状态、价格、时间、法院、联系人、位置、正文和附件容器；发现正文/附件存在异步“加载中”边界，必须轮询后再解析。只进入一个详情页，Cookie 未读取、导出或保存
- [x] **P2.7 PC 分页正式实现 Live 验收** — 2026-08-01 在用户可见 PowerShell 的非持久化登录会话中通过正式 `page=2` 链路：唯一点击真实 `a.next` 控件，页码由 1 变为 2，第一页/第二页分别解析 64/66 个标的，重叠 10 个、第二页新增 56 个；住宅用房/广东/江门筛选完全匹配，Cookie 未读取、导出或保存
- [x] **P2.6 PC 分页协议发现** — 2026-08-01 在用户可见 PowerShell 持有的非持久化登录会话中通过：唯一可用控件为 `a.next`，第二页 URL 明确增加 `page=2`，页码指示由 1 变为 2；第一页/第二页分别解析 64/66 个去重标的，重叠 10 个、第二页新增 56 个、合并后共 120 个唯一标的。全程只翻页一次，未读取、导出或保存 Cookie
- [x] **P2.5 PC 完整筛选能力矩阵** — 2026-08-01 完成真实登录态分批 Live 验收：关键词、区县、资产类型、排序、拍卖状态、拍卖阶段六个场景均 `accepted=true`、无筛选不一致；遇到 TMD 滑块后由用户人工验证，并以短批次恢复剩余场景。全程未读取、导出或保存 Cookie
- [x] **P2.4 PC 完整筛选交互式 Live 验收** — 2026-08-01 在用户手动登录的非持久化 Chrome 会话中通过固定查询验收：20/20 条、价格 100360–195097 元、无超限或缺价项、查询参数完全匹配，Cookie 未导出或持久化
- [x] ~~双端聚合 + 价格单位归一~~ (v2.1, 见 `search_judicial`)

## License

MIT
