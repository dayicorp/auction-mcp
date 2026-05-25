# auction-mcp

![MCP](https://img.shields.io/badge/MCP-server-7C3AED)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-47%20passing-brightgreen)
![Stars](https://img.shields.io/github/stars/dayicorp/auction-mcp?style=flat&label=★)

司法拍卖实时查询 MCP server — **阿里拍卖 + 京东拍卖** 双端聚合, 纯 Python httpx, 零外部设备/桥.

## 解决什么问题

司法拍卖数据散落在阿里拍卖和京东拍卖**两个独立池**, 各自有自己的反爬/sign/登录态/编码 vintage 等坑, 一般 LLM agent 想"查个法拍数据"得自己面对:

- 两端 API 各自的 sign 算法 / 反爬 cookie / 风控验证码
- 阿里区县编码用的是 **pre-2013 vintage**, 现代数据集会**静默返全国乱掺垃圾**而不报错
- 京东内部 area ID 跟国标完全没关系, 需要从内部 cascade API 抓全树
- agent 不知道传中文名还是 code, 不知道哪些字段在哪边, 价格单位还不一样 (阿里是分/京东是元)
- 任一端撞验证码 / token 过期 / 限流, 工具会崩

这个 MCP 把上面那些全在工具层封掉, **暴露给 agent 的就是干净的中文名 + 统一字段**:

```python
search_judicial(province="广东", city="深圳市", district="福田区")
# → 双端并行打完, 价格降序, 单位归一到元, 每条带 platform 标源
```

## 核心特性

- **双端聚合, 一次拿全量** — `search_judicial` 并行打阿里+京东, 价格降序合并, 单位归一到元, 加 `platform` 字段标源. agent 调一次就拿到双源数据, 不会漏掉半边池
- **三级地区精准查询** — 全国 31 省 / 342 市 / 3146 区县 (阿里) + 33 省 / 455 市 / 5344 区县 (京东)
- **中文名直传** — agent 传 `province="X省", city="Y市", district="Z区"` 就够了, 不碰任何内部编码
- **反爬守门** — 阿里 server 不认编码时会**静默返全国乱掺垃圾**, 工具自动校验并拒绝, 不会把假数据塞给 agent
- **常驻自愈** — `_m_h5_tk` cookie 过期自动重 bootstrap; baxia 风控 HTML 返结构化错误不崩
- **47 个 pytest 测试** — 单元 (零网络) + 容错 (monkeypatch) + 集成 (打真 API)

## Quick start

注册到 Claude (`~/.claude.json` 或 `claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "auction": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/auction-mcp/server.py"]
    }
  }
}
```

依赖:
```bash
pip install -r requirements.txt    # mcp, httpx, pytest
```

测试运行:
```bash
pytest                  # 23 单元 + 5 容错 (零网络)
pytest --run-live       # 上面 + 集成 (真打 Ali/JD API)
```

## 工具 (6 个)

| 工具 | 参数 | 说明 |
|---|---|---|
| ⭐ **`search_judicial`** | `province?`, `city?`, `district?`, `page=1`, `limit=20` | **推荐默认调用** — 并行查 阿里+京东, 价格降序合并, 单位归一到元, 加 `platform` 字段标源 |
| `ali_search_judicial` | `province?`, `city?`, `district?`, `page=1` | [Advanced 单源] 仅查阿里, 用户明确"只查阿里"时用 |
| `ali_get_supported_areas` | `province?`, `city?` | 列阿里支持的省/市/区县中文名 |
| `ali_get_filter_options` | (无) | 阿里 9 个 filter 维度的完整可选项 (分类/轮次/状态/特性/资产类型 等) |
| `jd_search_judicial` | `province?`, `city?`, `district?`, `page=1` | [Advanced 单源] 仅查京东, 用户明确"只查京东"时用 |
| `jd_get_supported_areas` | `province?`, `city?` | 列京东支持的省/市/区县中文名 |

> 阿里和京东是**两个独立标的池, 不重复**: 阿里偏机构端高价资产 (亿级土地/在建工程),
> 京东偏散户端住宅/股权/小额债权. 单独调一端会让用户只看到一半数据 — 所以默认用 `search_judicial`.

## 用法示例

### 推荐: unified `search_judicial` (默认双端聚合)

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
  "page": 1,
  "ali_totalCount": 1234,
  "jd_count": 40,
  "sources": ["ali", "jd"],
  "items": [
    {
      "platform": "ali",          // 或 "jd"
      "id": 1049000000000,        // ali itemId / jd paimaiId
      "title": "<拍品标题>",
      "price_yuan": 12345678.0,   // 归一到元 (阿里原值是分, 自动 ÷100)
      "raw": { /* 原始 item 完整字段, 含 locationCode / shopName / 法院 / 状态 / 时间 等 */ }
    },
    ...
  ]
}
```

### 单源 advanced (用户明确要"只查 X" 时用)

```python
ali_search_judicial(province="<省>", city="<市>", district="<区>")
jd_search_judicial(province="<省>", city="<市>")
```

### 发现 (查地区树)

```python
ali_get_supported_areas(province="<省>", city="<市>")
# → {city:"...", districts:[...]}

jd_get_supported_areas(province="<省>", city="<市>")
# → {city:"...", districts:[...]}
```

## ⚠️ 重要: 阿里区县编码 vintage 坑

阿里 server 用的是 **pre-2013 vintage GB 2260 编码**, 跟现代 (2020 版) 不一样。2013 年大量县/县级市改区时, **编码也跟着变了** —— 但阿里 server 还认旧码:

| 现代 GB 2260 (2020 版) | 阿里 server 实际接受 |
|---|---|
| 新区码 `33XX0X` | 旧县码 `33XX21` 之类 |

如果你绕过 `district=` 参数自己拼现代编码塞给 `location_codes`, 阿里**不报错, 静默返回十几万条全国乱掺数据**。这就是为什么:

- **推荐**: 永远用 `district="<区县中文名>"` 让工具自己解析 (内置 pre-2013 数据集 + 动态学码兜底)
- **不要**: 自己从 `ali_get_supported_areas` 拿 code 再传 `location_codes` (那是 2020 版仅供人类参考)
- 即便误用, 工具的 **垃圾结果守门** 也会捕获并返 `error: ali_returned_unscoped_results`, 不会让你拿到垃圾数据

技术实现: 项目 bundle 了 `gb2260_200712.json` (`cn/gb2260` 200712 快照) 作为阿里查询专用 vintage; 命名变更类区县 (2013 改名导致 legacy 数据集找不到的) 通过 `learn_district_code_from_city` 从城市级 item title 反查动态学码, 学到后写进程缓存。

## 工具行为约定

- **排序固定** 价格降序, 没暴露参数. 产品设计如此.
- **状态固定** 仅"进行中" + "即将开始" (`statusOrders=[0,1]` / `multiStatus=101,102`), 不会混入已结束/已撤回.
- **未识别层级 silent skip** — 传错 district 名不报错, 降级到 city 级返回; 传错 city 也降级到 province; 不会乱传错码触发 server 静默 fallback.

## 项目结构

```
auction-mcp/
├── server.py                # FastMCP server, 6 个 @mcp.tool()
├── ali_h5_client.py         # 阿里 H5 mtop client + 双 vintage 解析 + 守门 + 动态学码
├── jd_h5_client.py          # 京东 m. 版 client + 全国地区树查询
├── gb2260.json              # GB 2260 2020 版 (人类可读, 不用于查询)
├── gb2260_200712.json       # GB 2260 pre-2013 (阿里 server 实际接受的 vintage)
├── jd_areas.json            # 京东 33 省/455 市/5344 区县地区树
├── requirements.txt         # mcp, httpx, pytest
└── tests/                   # 47 项 pytest 测试
    ├── test_resolve.py      # 单元: 地区名解析 (零网络)
    ├── test_validation.py   # 单元: 守门校验 (零网络)
    ├── test_resilience.py   # 容错: token 自愈 + JSON 容错 (monkeypatch)
    ├── test_live_ali.py     # 集成: 真打阿里 API (--run-live)
    ├── test_live_jd.py      # 集成: 真打京东 API (--run-live)
    └── conftest.py          # --run-live 开关 + sys.path
```

## 架构 — 为什么是纯 httpx

历史: v1 想走"app 端真机 sign 桥" — IDA 逆向 + Substrate tweak + SSH 隧道, iPad/Android 两条路都被 anti-tamper SDK (unifiedSign + wua + sgext) 全面挡死。

v2 转向走**移动浏览器 H5 mtop**:

- **阿里**: `h5api.m.taobao.com/h5/mtop.taobao.datafront.invoke.auctionwalle/1.0/`, sign 是公开 MD5: `MD5(token + "&" + t + "&" + appKey + "&" + data)`, 其中 `token = _m_h5_tk` cookie 前 32 字符. 跟 app 端**同一 endpoint 同一数据源**.
- **京东**: `api.m.jd.com/api?functionId=getSearchData`, 风控字段 `h5st` / `x-api-eid-token` **可省略**, server 接受裸 form-encoded body. 16 类目 (住宅/商业/工业/土地/股权/债权 等).
- **地区树**: 阿里走国标 GB 2260 (前 2 省 + 中 2 市 + 末 2 区 zero-pad), 但用 pre-2013 vintage; 京东走自家 `getAreaInfoMap` cascade API 一次性抓全国树落本地.

完全 bypass app 端 anti-tamper, 只用纯 httpx + MCP stdio.

## Roadmap

- [ ] **阿里拍品详情** — 阿里 `queryHttpsItemDetail` mtop 被 baxia 风控拦 (需 `cna + tfstk + isg` cookie). 已规划方案: 本机 headless Playwright 一次性预热 cookie 注入 httpx, RGV587 时自动重预热. 待实施.
- [ ] 暴露更多筛选维度 (价格区间 / 分类中文名 / 关键词)
- [x] ~~双端聚合 + 价格单位归一~~ (v2.1, 见 `search_judicial`)

## License

MIT
