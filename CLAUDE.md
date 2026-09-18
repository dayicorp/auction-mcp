# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

司法拍卖实时查询 MCP server。聚合**阿里拍卖 + 京东拍卖**两个独立标的池, 纯 Python httpx + MCP stdio,
零外部设备/桥 (v1 的 app 端真机 sign 桥方案已废弃, 见 README 的架构来由折叠段)。

## 常用命令

```bash
pip install -r requirements.txt        # 运行期: mcp (<2), httpx
pip install -r requirements-dev.txt    # + pytest

pytest                                 # 102 项零网络 (单元/容错/冒烟)
pytest --run-live                      # + 19 项真打 Ali/JD 线上 API (共 121)
pytest tests/test_resolve.py::test_resolve_ali_jingzhou_is_not_jingmen   # 单个测试
pytest -k municipality                 # 按名字筛

python3 server.py                      # 起 MCP server (stdio)
python3 ali_h5_client.py 2             # 阿里 client 直连自检, 参数是页码
```

`mcp` 必须钉 `<2` —— mcp 2.x 移除了 `mcp.server.fastmcp` 并把 `FastMCP` 改名 `MCPServer`,
装到 2.x 会在 `server.py` import 阶段直接炸。改依赖时别放开这个上界。

`tests/conftest.py` 提供 `--run-live` 开关; 标了 `@pytest.mark.live` 的默认跳过。

## 架构

三层, 每层职责严格分开:

- **`server.py`** — FastMCP, 6 个 `@mcp.tool()`。负责参数归一 (`_clamp_page`/`_clamp_limit`)、
  地区解析编排、守门决策、错误信封、跨端合并。`search_judicial` 用 `ThreadPoolExecutor`
  并行调两个单源工具, 任一端失败降级为 `errors.{ali,jd}` 而不整体失败。
- **`ali_h5_client.py`** — 阿里 H5 mtop client (`h5api.m.taobao.com`)。
  sign = `MD5(token + "&" + t + "&" + appKey + "&" + data)`, token 取 `_m_h5_tk` cookie 前 32 字符。
  同时承载**两份 GB 2260 数据集**的解析器和守门函数 (纯函数, 被 server 和测试直接引用)。
- **`jd_h5_client.py`** — 京东 `api.m.jd.com/api?functionId=getSearchData`。
  风控字段 `h5st` / `x-api-eid-token` 可省略, server 接受裸 form-encoded body。
  地区树已一次性抓取冻结到 `jd_areas.json`。

### 数据文件的用途分工 (改动前务必分清)

| 文件 | vintage | 用途 |
|---|---|---|
| `gb2260_200712.json` → `GB2260_LEGACY` | pre-2013 | **阿里 server 实际接受的编码**, 所有阿里查询走它 (`resolve_area_ali`) |
| `gb2260.json` → `GB2260` | 2020 版 | 只给 `ali_get_supported_areas` 做人类可读展示 (`resolve_area`), **绝不用于查询** |
| `jd_areas.json` → `JD_AREAS` | 京东自有 id | 京东 `multiProvinceIds`/`multiCityIds`/`multiCountyIds` |

## 这个仓库特有的坑 (违反会静默出错, 不报错)

以下每一条都对应 `AUDIT.md` / `AUDIT-2.md` 里已修的缺陷 + 一条回归测试。改相关代码前先读那条测试。

1. **阿里编码 vintage 错位 → 静默返全国乱掺垃圾**。传 2020 版码 (如柯桥 330603) 给阿里,
   server 不报错, 直接返十几万条散落全国的数据。因此:
   - 查询侧一律走 `resolve_area_ali` (legacy);
   - `validate_location_scoped()` 是**最后一道守门**, 按 `expected_prefix` 校验 ≥80% 的
     `locationCode` 落在范围内, 不过就返 `error: ali_returned_unscoped_results`。
   - legacy 数据集没覆盖的改名区县 (绍兴县→柯桥区) 靠 `learn_district_code_from_city()`
     翻城市级结果的 title 反查动态学码, 命中与"确认学不到"都进 `_DISTRICT_CODE_CACHE` (含负缓存)。

2. **`scope_prefix()` 的粒度不是固定 `code[:4]`**。省级 `440000` → `'44'`, 市级/区县级 → 前 4 位。
   历史上一律取 `[:4]` 得到 `'4400'`, 而真实区县码是 `4401xx/4403xx…`, 于是**省级查询 100% 被
   自己的守门误杀**。

3. **地区名匹配必须精确优先, 不做无序子串匹配**。`str.rstrip` 吃的是字符集合不是后缀串,
   `"荆州市".rstrip("市地区州盟")` 会剥到只剩 `"荆"`, 子串匹配就串到**荆门市**去了 ——
   而且因为 `expected_prefix` 由同一个错码导出, 守门结构性发现不了。
   三级匹配顺序: 精确 → 去后缀后精确 → 前缀, **三遍独立循环**, 强条件全局优先
   (挤在一遍里 `or` 掉就等于让迭代顺序说了算)。
   后缀字符集必须**按层级拆**, 区县级的 `_SFX_DIST = "区县市旗"` **刻意不含 `州`** ——
   否则 "定州市"→"定"、"海州区"→"海", 又会串到 定兴县/东海县。
   ⚠️ 这套逻辑有**两份实现**: `ali_h5_client._pick` 和 `jd_h5_client._match_name`。
   改一处必须同步另一处; 历史上三条 P0 都是"只修了一份"造成的。
   `server.py` 里**不要**再写第三份 —— 展示工具一律复用这两个。

4. **直辖市在两端都是特殊结构, 且形状不同** —— 不特判的话, 4 个直辖市的**所有**区县查询
   都会静默退化到全市/全省:
   - 阿里 legacy: 省 → `市辖区`|`县` → 区县, `"上海市"` 匹配不到中间节点,
     靠 `_municipality_district()` 跨节点找;
   - 京东: 省 → **区** 两层, 根本没有"市"这一级, 靠 `MUNICIPALITIES` 集合特判 ——
     city 匹配失败时拿 district 去 `cities` 那层再试一次。

5. **价格单位: 阿里 `currentPrice` 是分, 京东是元, 差 100 倍**。所有工具额外输出归一到元的
   `price_yuan`, 新增读价格的代码一律用它。单源工具也补这个字段, 别只在聚合层补。

6. **并发下的 token 自愈**。FastMCP 把同步工具丢进线程池, 共享一个 `AliH5Client`。
   `_tk_lock` + 双重检查 + `_invalidate_token(stale)` 的幂等判断缺一不可, 否则会有线程拿
   `None` 当 token 签名, 把一次过期放大成连环 Sign Error。

7. **`area_applied` / `area_scoped` 是"地区到底生效了没"的唯一真相**。两端对解析失败的
   处理语义相反 —— 阿里报 `area_not_resolved`, 京东 silent skip 后上游返**全国**数据。
   合并层的规则: 有成功的源但没有任何一个把地区收窄过 → 返 `area_not_resolved` 且不返 items;
   只是部分层级没应用 (省对但城市名不认) → 正常返数据但标 `area_applied: false`。
   两端都网络失败时 `sources` 为空, 那是网络问题, **不得**误判成地区问题。
   加任何新的地区解析路径时, 都要同步让它报出这两个字段。

8. **`limit` 是本页展示上限, 不是分页窗口**。两端页大小不同 (ali 10 / jd 40), 合并层没有游标,
   所以被 `limit` 截掉的标的**不会**出现在 `page+1`。默认值必须保持 `MAX_LIMIT` (= 满池 50),
   截断时用 `dropped` 显式报出条数。「价格降序」只在**单页内**成立, 跨页不保证单调 ——
   改这块前先想清楚要不要真做游标分页。

9. **产品行为固定, 不暴露参数**: 排序恒为价格降序 (ali `sort=501` / jd `sortField=7`),
   状态恒为"进行中+即将开始" (ali `statusOrders=["0","1"]` / jd `multiStatus="101,102"`,
   京东这里必须是逗号字符串, 传数组会被 server 忽略)。
   未识别的地区层级 **silent skip 逐级降级** (区→市→省), 不报错。

## 测试分层

- `test_resolve.py` — 地区解析纯函数, 含**两端各自的全量数据集往返测试**:
  阿里 3146 区县逐个 resolve 回原码; 京东 5344 区县逐个用**自然简称**(去末尾一个后缀字)
  解析回自己。新增解析逻辑必须让这两条都仍过。
  ⚠️ 只用**全名**写用例是无效覆盖 —— 全名走的是 `query in candidates` 精确分支,
  永远碰不到下面的模糊匹配, 京东端 24 处串区就是这样逃过 4 个手写用例的。
- `test_validation.py` — 守门 `validate_location_scoped` 的阈值/边界。
- `test_resilience.py` — mtop token 自愈、并发时序、非 JSON 风控页容错 (monkeypatch 假 response)。
- `test_smoke.py` — 两层冒烟: **L1** 真起 `server.py` 子进程走 MCP stdio 协议 (initialize /
  tools/list / tools/call); **L2** 用 `httpx.MockTransport` 扮演"完全配合"的上游跑全链路。
  L2 的 mock 上游**如实按请求的编码返回正确数据**, 所以任何错误结果只可能是客户端自身缺陷 ——
  加新的管线级回归测试放这里。
- `test_live_*.py` — 真打线上, 默认跳过。

## Roadmap 里的已知阻塞

阿里拍品详情 (`queryHttpsItemDetail`) 被 baxia 风控拦截, 需要 `cna + tfstk + isg` cookie。
规划方案是本机 headless Playwright 一次性预热 cookie 注入 httpx, RGV587 时自动重预热。
