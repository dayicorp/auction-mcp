# auction-mcp 第二轮审计

审计日期: 2026-09-18 · 前置: 本地冒烟通过 (86 passed, 含 19 项真打线上)
方法: 全量数据集往返 + **真打线上取证** (与第一轮不同, 本次出口网络可达 taobao/jd, 所有结论均有线上实证)
范围: `server.py` / `ali_h5_client.py` / `jd_h5_client.py` / `README.md`

> 第一轮 (`AUDIT.md`, 2026-09-17) 的 13 项我逐条复验过, 11 项确实修好了。
> 本轮的 5 条里有 **3 条是那 13 项的"半修"** —— 同一个根因在另一条代码路径上原样活着, 且都被回归测试的覆盖盲区放过。

---

## 冒烟结果 (审计前置)

| 项目 | 结果 |
|---|---|
| `pytest` 零网络 | 67 passed |
| `pytest --run-live` 全量 | **86 passed**, 4.34s |
| MCP 协议层 (L1 子进程 stdio) | 6 工具注册正确, `tools/call` 通 |
| 手工全链路 (全国/省/市/区县/直辖市区县/双端聚合) | 全部返回真实数据, 守门 `validated: true` |
| 并发 8 路真打 | 0.6s, 无 token 竞态, 无错误 |

线上实测样本: 全国 `ali_totalCount=160571`; 广东 18665; 柯桥区动态学码命中 `330621`;
上海浦东 `310115`; 荆州市 `421002/421023` 未串荆门。**核心链路是健康的**, 下列问题都在边缘路径。

---

## 结论速览

| # | 级别 | 问题 | 位置 | 与第一轮的关系 | 状态 |
|---|---|---|---|---|---|
| A | **P0** | 京东端区县名静默串区: `district="海州"` 返回**东海县**的真数据 | `jd_h5_client.py:49-62` | P0-3 同根因, **京东侧从未修** | ✅ 已修 |
| B | **P0** | `ali_get_supported_areas("湖北","荆州市")` 仍返回**荆门市** | `server.py:437-449` | P0-3 **只修了一半** | ✅ 已修 |
| C | P1 | `search_judicial` 翻页静默丢标的: page=1 丢弃的 30 条在 page=2 里一条都没有 | `server.py:176-200` | 新发现 | ✅ 已修 |
| D | P1 | 省名拼错 → 静默返回全国数据, envelope 顶层看起来是成功 | `server.py:180-196` | 新发现 | ✅ 已修 |
| E | P2 | `jd_search_judicial` 没有 `price_yuan`, 但 README 明写"所有工具都有" | `server.py:509-533` | P1-13 **只修了阿里侧** | ✅ 已修 |
| F | **P0** | *(修复期新发现)* 京东直辖市树是 省→区 两层, `city="上海市"` 匹配不到 → district 连试都不试, 查"上海浦东新区"静默返**全上海** | `jd_h5_client.py:_resolve_area` | P0-12 在京东侧的同类缺口 | ✅ 已修 |

全部 6 项已修复, 每项都有对应回归测试。测试从 **67 offline / 19 live** 增至 **102 offline / 19 live**。

### 修复摘要

| # | 修法 |
|---|---|
| A | `_match_name` 改成三遍独立循环 (精确 → 去后缀后精确 → 前缀), 后缀集合按层级拆 (`_SFX_PROV`/`_SFX_CITY`/`_SFX_DIST`), **区县级不含 `州`**。自然简称错配 24 → 5 (剩下 5 个是真同名歧义, 如 临夏市/临夏县)。 |
| B | 删掉 `server.py` 里那份重复的匹配实现, 改用 `ali_h5_client._pick`。342 个地级市全量往返错配 1 → 0。 |
| C | `limit` 默认 20 → `MAX_LIMIT` (50, 满池); envelope 新增 `dropped` 显式报出截断条数; docstring 写明 limit 是展示上限而非分页窗口, 价格降序只在单页内成立。 |
| D | 两个单源工具新增 `area_scoped` / `area_applied`; 合并层在"有成功的源但没有任何一个把地区收窄过"时返 `area_not_resolved` 且不返 items; 部分层级未应用则正常返数据但标 `area_applied: false`。两端都网络失败时不误判为地区问题。 |
| E | 京东单源 item 补 `price_yuan`, 与阿里单源字段名/单位一致。 |
| F | `_resolve_area` 在 city 匹配失败且省份是直辖市时, 拿 district 去 `cities` 那一层再匹配一次; 层级判定同步识别直辖市的两层结构。 |

---

## A · P0 — 京东端区县名静默串区 (P0-3 的京东版, 从未修)

第一轮 P0-3 的根因是 `str.rstrip` 吃的是**字符集合**而非后缀串, 导致 `"荆州市"` 被剥成 `"荆"`,
再靠子串匹配串到兄弟行政区。阿里侧用 `_pick()` 的三级精确优先匹配修掉了, **京东侧原封不动**:

```python
# jd_h5_client.py:49-62
def _match_name(query, candidates):
    if query in candidates:                      # 1. 精确 ✓
        return query, candidates[query]
    q = query.rstrip("省市区县旗自治区盟自治州")   # ← 字符集合含 州/自/治, "定州"→"定"
    for k, v in candidates.items():
        ks = k.rstrip("省市区县旗自治区盟自治州")
        if k.startswith(query) or ks == q or ks.startswith(q) or q in ks:
            return k, v                          # ← 四个条件挤在同一遍循环, 迭代顺序说了算
    return None
```

两个独立缺陷叠加:

1. **后缀字符集合过宽** —— 阿里侧区县用的是 `_SFX_DIST = "区县市旗"` (**不含 `州`**), 所以 `"定州市"` 只被剥成 `"定州"`, 安全; 京东侧一个大集合通吃三级, `州` 也被剥掉。
2. **四个条件同一遍循环** —— 只满足最弱条件 (`q in ks`, 任意位置子串) 的候选若排在前面, 就压过后面本该精确命中的候选。阿里侧的 `_pick` 是三遍独立循环 (精确 → 去后缀后精确 → 前缀), 强条件全局优先。

### 线上取证

```
jd._resolve_area("河南","新乡市","辉县")  → countyId=459  name='卫辉市'   (正解: 辉县市=460)
jd._resolve_area("江苏","连云港市","海州") → countyId=922  name='东海县'   (正解: 海州区)
jd._resolve_area("河北","保定市","定州")   → countyId=210  name='定兴县'   (正解: 定州市)
jd._resolve_area("湖北","黄冈市","黄州")   → countyId=1447 name='黄梅县'   (正解: 黄州区)
```

真打线上, 拿到的是**另一个县的真实标的**, 无任何报错:

```
jd_search_judicial(江苏, 连云港市, district="海州")   count=40  首条「东海县牛山镇海陵东路99号水晶公园…」
jd_search_judicial(江苏, 连云港市, district="海州区") count=40  首条「连云港市新浦区汇金世贸广场…」
jd_search_judicial(河北, 保定市,  district="定州")   count= 3  首条「坐落于定兴县旧107国道东侧御颐园小区…」
jd_search_judicial(河北, 保定市,  district="定州市") count= 4  首条「河北省定州市东亭镇东亭村…」
```

**京东端没有任何守门** —— `validate_location_scoped` 是阿里专用, 且这里发出的是一个**合法的**
countyId, 守门就算搬过来也拦不住。唯一的防线就是匹配本身要对。

### 影响面

全量往返测试 (5344 区县, 用"人类会打的自然简称"= 去掉末尾一个后缀字):

- **24 个区县错配**。其中 5 个是真正歧义的同名对 (和田县/和田市、伊宁县/伊宁市、临夏县/临夏市、新竹市/新竹县、嘉义市/嘉义县) —— 这类无解, 可接受;
- **剩余 19 个是纯粹的子串匹配误伤**: 定州→定兴县、滦州→滦南县、潞州→潞城区、辉县→卫辉市、海州→东海县、莱州→莱阳市、颍州→颍上县、襄州→襄城区、黄州→黄梅县、吉州→吉安县、华州→华阴市、秦州→秦安县、肃州→肃北蒙古族自治县、汤旺→汤旺河区、科尔沁→科尔沁左翼中旗、高新→高新西区 等。

对照: 阿里侧同样输入只有 **4 个**错配, 且全是上述"真正歧义"那类。

> `"辉县"`、`"黄州"`、`"海州"` 都是这些地方的**标准口语名**, 不是生僻写法 —— LLM agent 从用户的
> "查一下辉县的法拍房" 里抽出来的就是 `"辉县"`。

### 为什么测试没抓到

`tests/test_resolve.py` 对阿里侧有 `test_resolve_ali_full_dataset_roundtrip_is_lossless`
(3146 区县逐个 resolve 回原码), **京东侧没有对应的全量往返测试**, 只有 4 个手写用例
(`test_jd_resolve_district_wujiang` 等), 全都传的是**全名**, 走 `if query in candidates` 那条精确分支, 永远碰不到下面的模糊循环。

### 建议修法

把 `_pick` 的三级结构搬过来, 并按层级拆分后缀字符集 (省 `省市自治区` / 市 `市地区州盟` / 区县 **`区县市旗`**, 区县级**不要含 `州`**)。
配一条京东侧的全量往返测试, 并把上面 19 个误伤点固化成 parametrize 用例。

---

## B · P0 — `ali_get_supported_areas` 里的荆州/荆门仍在 (P0-3 只修了一半)

第一轮报告把 P0-3 标成 ✅ 已修, 修的是 `ali_h5_client._pick`。但 `server.ali_get_supported_areas`
**自己另写了一份匹配逻辑**, 从未跟着改:

```python
# server.py:437-449
pn = province.rstrip("省市自治区")
p = next((x for x in GB2260 if pn in x["name"] or x["name"].startswith(pn)), None)
...
cn = city.rstrip("市地区州盟")                       # "荆州市" → "荆"
c = next((x for x in p.get("children",[])
          if cn in x["name"] or x["name"].startswith(cn)), None)   # "荆" in "荆门市" → True
```

`cn in x["name"]` 是**任意位置子串**, 比第一轮修掉的那版还宽 (那版至少是前缀语义)。

### 线上取证

```
ali_get_supported_areas("湖北", "荆州市") → city='荆门市' code=420800 区县数=5
                                            districts: ['东宝区','掇刀区','沙洋县','钟祥市','京山市']
ali_get_supported_areas("湖北", "荆门市") → city='荆门市' code=420800 区县数=5   ← 两者完全相同
ali_get_supported_areas("河北", "定州市") → city='保定市' code=130600 区县数=26  ← "定" in "保定市"
```

全量往返 (342 个地级市) 确认**错配恰好 1 例**: 荆州市 → 荆门市 —— 就是第一轮点名的那一例, 换了条路径。省级 31/31 全对。

### 影响

这是**给 agent 查"有哪些区县可选"的发现型工具**。agent 查荆州, 拿到一份荆门的区县名单
(东宝区/掇刀区/沙洋县…), 然后拿着 `district="沙洋县"` 去查"荆州的法拍" —— 后面的查询链路全是干净的,
但输入已经错了, 且错在一个不会报错的地方。

### 建议修法

删掉这份重复实现, 直接复用 `ali_h5_client._pick(candidates, query, _SFX_CITY)`。
一份匹配逻辑两处实现, 是这条缺陷能活下来的直接原因。

---

## C · P1 — `search_judicial` 翻页静默丢标的

阿里 10 条/页, 京东 40 条/页, 合并池 50 条, 但默认 `limit=20`:

```python
# server.py:198-200
items.sort(key=..., reverse=True)
items = items[:limit]        # ← 丢掉的 30 条, page=2 不会补回来
```

`page=2` 时两端各自翻到自己的第 2 页 (阿里 11-20 条, 京东 41-80 条), page=1 被 `[:limit]` 截掉的那 30 条**永久消失**。

### 线上取证 (广东省, limit=20)

```
page=1  count=20  价格 1,495,900,000 … 177,320,000
page=2  count=20  价格   287,860,000 …  38,186,710
        page2 首条价 (2.88亿) > page1 末条价 (1.77亿)   ← 价格降序在翻页处断裂
        page1 ∩ page2 = 0 条重叠
page=1 实际从上游取回 50 条, 只返 20 条 → 丢弃 30 条
        这 30 条里出现在 page=2 的: 0 条
```

两个后果:

1. **数据丢失** —— 用户按页浏览, 每页永久看不到 30 条标的 (默认参数下丢 60%)。
2. **排序承诺失效** —— 工具 docstring 和 server instructions 都承诺"价格降序", 但跨页看是锯齿状的 (1.77亿 之后跳回 2.88亿)。agent 若据此判断"已经看完所有 2 亿以上的标的", 结论是错的。

### 建议修法

三选一, 按产品意图定:

- **最小改动**: `limit` 默认值改成 `MAX_LIMIT` (50), 让单页不丢东西; 并在 docstring 写明"limit 小于 50 时本页未返回的标的不会出现在下一页"。
- **正确但要改契约**: 引入合并层的游标 —— 记录两端各自消费到第几条, 下一页从断点续取。
- **务实**: 保持现状但把 `limit` 定位成"截断展示", 同时在 envelope 里加 `dropped: 30` 字段, 让上层知道有东西被截掉了。

---

## D · P1 — 省名拼错 → 静默返回全国数据

两端对"地区解析不出来"的处理语义相反:

- 阿里: 返回 `{"error": "area_not_resolved"}` (第一轮 P2-7 特意加的)
- 京东: `_resolve_area` **silent skip**, 什么都不传 → 上游返回全国

`search_judicial` 把两者合并后, envelope 顶层呈现的是**成功**:

### 线上取证

```
search_judicial(province="火星省")
  {"count": 3, "page": 1, "ali_totalCount": null, "jd_count": 40,
   "sources": ["jd"],
   "errors": {"ali": {"error": "area_not_resolved", ...}}}
  items:
    [jd] 重庆市渝中区两路口菜市场（渝中组团C11-1号）地块…
    [jd] 被执行人持有的鄂尔多斯市昊华精煤有限责任公司10%股权
    [jd] 南京市建邺区会展中心东北角…
```

对照全国基线 `search_judicial()` 的京东首条 —— **完全一致**。即"火星省的拍卖"返回的就是全国数据。

同样路径还有:

```
search_judicial(district="福田区")   # 只传 district 不传 city
  → errors.ali = district_requires_city, sources=["jd"], items = 全国 top40
```

一个 LLM agent 看到 `count: 3` + `sources: ["jd"]` + 三条真实标的, 最自然的行为就是直接汇报
"火星省有这 3 个标的" —— `errors` 字段埋在 envelope 尾部, 且名字听起来像"部分降级"而不是"你的地区参数根本没生效"。

### 建议修法

在合并层加一条判断: **如果地区参数非空, 但所有成功的源都没能把它解析成实际的地区过滤**, 就不要返回 items,
而是返回 `{"error": "area_not_resolved", "message": "...", "hint": "..."}`。
至少也要在 envelope 顶层加 `area_applied: false` 这样一个**平级于 count 的显著标志**, 而不是只留在 `errors` 里。

顺带: 京东侧的 silent skip 本身是为了"不乱传错码触发 server 静默 fallback 到全国"而设计的,
方向没问题, 但它需要**把 skip 这件事回报给调用方**, 现在是完全静默的。

---

## E · P2 — `jd_search_judicial` 没有 `price_yuan`, 文档却说有

第一轮 P1-13 修的是"阿里单源 `currentPrice` 是分", 做法是给阿里单源补 `price_yuan`
(`server.py:295-297`)。京东单源的 item 组装 (`server.py:509-533`) **没补**。

但 README 和 server instructions 都是**无条件**的表述:

> README: "**所有工具**都额外输出归一到元的 `price_yuan` —— 读价格一律用它, 别直接读 `currentPrice`."
>
> server instructions #6: "⚠️ 读价格一律用 `price_yuan` (元)."

### 取证

```
jd_search_judicial(江苏, 苏州市) item keys:
  ['city','cityId','countyId','creditCapitalCN','currentPrice','currentPriceCN','discountRate',
   'displayStatus','endTime','houseAttributes','paimaiId','productCateId','productImage',
   'province','publishSource','remindCount','skuId','startPrice','title']
  price_yuan 存在? False
ali_search_judicial(江苏, 苏州市)  price_yuan 存在? True
```

严格照文档走的 agent 在京东单源工具上读到的是 `None`/缺字段。京东的 `currentPrice` 本来就是元,
所以不会报错成 100 倍 —— 但会变成"读不到价格", 或者 agent 退回去读 `currentPrice` 时**把两端字段
等同看待的心智又回来了**, 而这正是 P1-13 想根除的。

### 建议修法

`jd_search_judicial` 的 item 组装里补一行 `"price_yuan": float(cp) if isinstance(cp,(int,float)) else None`,
与 `_normalize_item` 的京东分支保持一致。一行的事, 但它是文档可信度问题, 不是功能问题。

---

## 次要观察 (P3, 不构成缺陷)

- **envelope 字段语义不对称**: `ali_totalCount` 是全量总数 (广东 18665), `jd_count` 是**本页条数** (恒 40)。命名对称但语义不同, 容易被读成"京东只有 40 条"。京东响应里 `data.mergeSearchCondition.thirdCateItemList[].count` 有分类级计数可以求和, 若要补一个近似总数可以从那里取。
- **学码负缓存无 TTL**: `_DISTRICT_CODE_CACHE` 里 `None` 永久有效, 该区县日后上新标的也学不到了。长驻进程下值得加个过期。
- **仓库卫生良好**: `git ls-files` 干净, `.DS_Store`/`__pycache__`/`.venv` 均未入库; `import server` 212ms, 两份 GB2260 + 京东地区树共 ~940KB 的加载开销可接受。
- **README badge 数字准确**: "67 offline + 19 live" 与实测 `86 passed` 一致。

---

## 修复优先级建议

1. **B** (P0) —— 一行复用 `_pick`, 无风险, 直接消掉第一轮遗留。
2. **A** (P0) —— 影响面最大且完全静默; 连带补京东侧全量往返测试, 这是覆盖盲区本身。
3. **D** (P1) —— 语义问题, 改动在合并层, 需要先定"要报错还是要标志位"。
4. **C** (P1) —— 需要先定产品意图 (截断展示 vs 真分页), 再动代码。
5. **E** (P2) —— 一行。

A、B、E 三条的共同教训: **同一份逻辑有两处实现时, 修了一处就会把另一处留成定时炸弹**。
三条都是第一轮修复的"半修", 且三条都因为测试只覆盖了被修的那条路径而逃过回归。
