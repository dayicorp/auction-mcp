# auction-mcp

![MCP](https://img.shields.io/badge/MCP-server-7C3AED)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-74%20passed%20%7C%2019%20skipped-brightgreen)
![Stars](https://img.shields.io/github/stars/dayicorp/auction-mcp?style=flat&label=★)

司法拍卖实时查询 MCP server — **阿里拍卖 + 京东拍卖** 双端聚合, 纯 Python httpx, 零外部设备/桥.

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
- **93 项 pytest 测试** — 74 项离线通过 + 19 项 Live 默认跳过

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

```bash
pip install -r requirements.txt    # mcp, httpx, pytest
pytest                             # 单元 + 容错 (零网络)
pytest --run-live                  # + 集成 (真打 Ali/JD API)
```

## 工具 (6 个)

| 工具 | 参数 | 说明 |
|---|---|---|
| ⭐ **`search_judicial`** | `province?`, `city?`, `district?`, `page=1`, `limit=20` | **推荐默认调用** — 并行查 阿里+京东, 价格降序合并, 单位归一到元 |
| `ali_search_judicial` | `province?`, `city?`, `district?`, `page=1`, `fcat_v4_ids?`, `zc_biz_types?` | [Advanced 单源] 仅查阿里；分类/资产类型编码来自 `ali_get_filter_options` |
| `ali_get_supported_areas` | `province?`, `city?` | 列阿里支持的省/市/区县中文名 |
| `ali_get_filter_options` | (无) | 阿里 9 个 filter 维度的完整可选项 |
| `jd_search_judicial` | `province?`, `city?`, `district?`, `page=1` | [Advanced 单源] 仅查京东 |
| `jd_get_supported_areas` | `province?`, `city?` | 列京东支持的省/市/区县中文名 |

> 阿里和京东是**两个独立标的池, 不重复**: 阿里偏机构端高价资产 (亿级土地/在建工程), 京东偏散户端住宅/股权/小额债权. 默认调 `search_judicial` 拿双端聚合.

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
- 排序固定 价格降序; 状态固定 仅"进行中"+"即将开始" — 都不暴露参数, 产品设计如此
- 公开 MCP 工具对层级缺失或无法解析的地区 fail-closed, 返回结构化错误且 provider 调用为 0
- `JDH5Client._build_area_params` 底层仍保留 silent skip, 仅作为内部防御, 不代表公开工具会扩大查询范围
</details>

<details>
<summary>项目结构 + 架构来由</summary>

```
auction-mcp/
├── server.py            # FastMCP server, 6 个 @mcp.tool()
├── ali_h5_client.py     # 阿里 H5 mtop client + 双 vintage 解析 + 守门 + 动态学码
├── jd_h5_client.py      # 京东 m. 版 client + 全国地区树查询
├── gb2260.json          # GB 2260 2020 版 (展示用, 不用于查询)
├── gb2260_200712.json   # GB 2260 pre-2013 (阿里 server 实际接受的 vintage)
├── jd_areas.json        # 京东 33 省/455 市/5344 区县地区树
└── tests/               # 93 项 pytest (含 region boundary / resolve / validation / resilience / live)
```

### 为什么是纯 httpx

v1 想走"app 端真机 sign 桥" — IDA 逆向 + Substrate tweak + SSH 隧道, iPad/Android 两条路都被 anti-tamper SDK (unifiedSign + wua + sgext) 全面挡死。

v2 转向走移动浏览器 H5 mtop:
- **阿里**: `h5api.m.taobao.com/h5/mtop.taobao.datafront.invoke.auctionwalle/1.0/`, sign 是公开 MD5: `MD5(token + "&" + t + "&" + appKey + "&" + data)`, 其中 `token = _m_h5_tk` cookie 前 32 字符. 跟 app 端**同一 endpoint 同一数据源**.
- **京东**: `api.m.jd.com/api?functionId=getSearchData`, 风控字段 `h5st` / `x-api-eid-token` **可省略**, server 接受裸 form-encoded body. 250 万+ 司法拍卖标的, 16 类目.
- **地区树**: 阿里走国标 GB 2260 (前 2 省 + 中 2 市 + 末 2 区 zero-pad), 但用 pre-2013 vintage; 京东走自家 `getAreaInfoMap` cascade API 一次性抓全国树落本地.

完全 bypass app 端 anti-tamper.
</details>

## Roadmap

- [ ] **阿里拍品详情** — `queryHttpsItemDetail` mtop 被 baxia 风控拦 (需 `cna + tfstk + isg` cookie). 已规划: 本机 headless Playwright 一次性预热 cookie 注入 httpx, RGV587 时自动重预热.
- [ ] 暴露更多筛选维度 (价格区间 / 分类中文名 / 关键词；Ali Advanced 已支持分类和资产类型编码)
- [x] ~~双端聚合 + 价格单位归一~~ (v2.1, 见 `search_judicial`)

## License

MIT
