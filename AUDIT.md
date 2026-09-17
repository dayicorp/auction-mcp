# auction-mcp 项目审计

审计日期: 2026-09-17 · 审计范围: `server.py` / `ali_h5_client.py` / `jd_h5_client.py` / `tests/` / 打包与文档
方法: 静态走读 + 针对性可执行验证 (全量数据集往返测试 / 桩化边界测试 / 并发时序复现)

> 说明: 本环境出口代理封禁了 taobao.com 与 jd.com (403), 因此 **未能做线上端到端验证**。
> 下列结论全部由离线可复现的逻辑推演与数据集全量测试得出, 每条附复现方式。

---

## 结论速览

| # | 级别 | 问题 | 位置 | 状态 |
|---|---|---|---|---|
| 1 | **P0** | `mcp>=1.0` 会装到 mcp 2.x, `FastMCP` 已被移除 — 全新安装直接起不来 | `requirements.txt:1` | ✅ 已修 |
| 2 | **P0** | 省级查询被自己的守门 100% 误杀, 阿里端恒返 `ali_returned_unscoped_results` | `server.py:263` | ✅ 已修 |
| 3 | **P0** | 查"荆州市"静默返回**荆门市**数据, 且守门结构性无法发现 | `ali_h5_client.py:58-62` | ✅ 已修 |
| 4 | P1 | 另有 23 个区县被解析到相邻行政区 (同一根因) | `ali_h5_client.py:65-69` | ✅ 已修 |
| 5 | P1 | 两个单源工具在网络异常时**裸抛异常**, 只有统一工具做了兜底 | `server.py:189,396` | ✅ 已修 |
| 6 | P1 | 并发请求下 token 自愈存在竞态, 会用 `None` 当 token 签名并互相踩踏 | `ali_h5_client.py:197-223` | ✅ 已修 |
| 7 | P2 | 学码失败无负缓存; 未知省份时会发出 `['0000']` / `['']` 垃圾查询 | `server.py:252-258` | ✅ 已修 |
| 8 | P2 | `limit` / `page` 无校验, `limit=-1` 静默丢掉最后一条 | `server.py:174` | ✅ 已修 |
| 9 | P2 | `validated` 恒为 `True`, 是死变量但被写进文档化返回值 | `server.py:288-311` | ✅ 已修 |
| 10 | P2 | `ali_get_filter_options` 的 `ret` 解析会崩, 与同文件另一处的健壮写法不一致 | `server.py:331` | ✅ 已修 |
| 11 | P3 | README badge "47 tests" 里有 19 个默认不跑 (需 `--run-live` + 网络) | `README.md:6` | ✅ 已修 |
| 12 | **P0** | *(修复期新发现)* 4 个直辖市的**所有**区县查询退化到省级 — 区县挂在 `市辖区`/`县` 中间节点下, 市名匹配不到 | `ali_h5_client.py` | ✅ 已修 |
| 13 | P1 | *(冒烟新发现)* 单源阿里的 `currentPrice` 是**分**, 与京东同名字段差 100 倍, 且 docstring 全文未提单位 | `server.py` | ✅ 已修 |

全部 13 项已修复, 每项都有对应回归测试。测试从 **28 offline / 19 live** 增至 **67 offline / 19 live**。

---|---|---|---|
| 1 | **P0** | `mcp>=1.0` 会装到 mcp 2.x, `FastMCP` 已被移除 — 全新安装直接起不来 | `requirements.txt:1` |
| 2 | **P0** | 省级查询被自己的守门 100% 误杀, 阿里端恒返 `ali_returned_unscoped_results` | `server.py:263` |
| 3 | **P0** | 查"荆州市"静默返回**荆门市**数据, 且守门结构性无法发现 | `ali_h5_client.py:58-62` |
| 4 | P1 | 另有 23 个区县被解析到相邻行政区 (同一根因) | `ali_h5_client.py:65-69` |
| 5 | P1 | 两个单源工具在网络异常时**裸抛异常**, 只有统一工具做了兜底 | `server.py:189,396` |
| 6 | P1 | 并发请求下 token 自愈存在竞态, 会用 `None` 当 token 签名并互相踩踏 | `ali_h5_client.py:197-223` |
| 7 | P2 | 学码失败无负缓存; 未知省份时会发出 `['0000']` / `['']` 垃圾查询 | `server.py:252-258` |
| 8 | P2 | `limit` / `page` 无校验, `limit=-1` 静默丢掉最后一条 | `server.py:174` |
| 9 | P2 | `validated` 恒为 `True`, 是死变量但被写进文档化返回值 | `server.py:288-311` |
| 10 | P2 | `ali_get_filter_options` 的 `ret` 解析会崩, 与同文件另一处的健壮写法不一致 | `server.py:331` |
| 11 | P3 | README badge "47 tests" 里有 19 个默认不跑 (需 `--run-live` + 网络) | `README.md:6` |

---

## P0-1 · 全新安装直接起不来

`requirements.txt` 写的是 `mcp>=1.0`, 而 mcp 2.x 已把 `FastMCP` 改名为 `MCPServer`, `mcp.server.fastmcp` 整个模块被移除。今天按 README 的 `pip install -r requirements.txt` 走一遍, `server.py:21` 的 import 立刻失败:

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x, where
FastMCP was renamed to MCPServer ... see the migration guide ... or pin 'mcp<2'
```

连测试都无法收集 (`tests/test_live_*.py` 会 import `server`)。**这是新用户的第一道门, 目前是关着的。**

- 复现: 干净 venv 里 `pip install -r requirements.txt && pytest`
- 最小修复: `requirements.txt` 改 `mcp>=1.0,<2`
- 建议: 把 `pytest` 从运行期依赖挪到 `requirements-dev.txt`; 顺带补个 `pyproject.toml`

改成 `mcp<2` 后 (实测装到 1.30.0), 离线测试 **28 passed, 19 skipped**, 全绿。

## P0-2 · 省级查询被自己的守门 100% 误杀

`server.py:259-263` 对"只传 province"和"传 province+city"用了同一套前缀截取:

```python
elif province or city:
    code = resolve_area_ali(province, city)   # 省级 → "440000"
    expected_prefix = code[:4]                # → "4400"
```

省级编码形如 `XX0000`, 截 4 位得到 `XX00`。而真实区县码是 `4401xx`/`4403xx`/`4413xx`……**没有任何一个真实区县码以 `4400` 开头**。于是 `validate_location_scoped` 必然算出 0% 命中, 直接走进 `error: ali_returned_unscoped_results`:

```
广东: code=440000 -> expected_prefix='4400'
广东省级查询 guard 结果: {'ok': False, 'matched': 0, 'total': 6,
  'sample_off_prefix': ['440304','440305','440106','440113','441302','440703']}
对比 2 位前缀 '33': True
```

讽刺的是 `validate_location_scoped` 的 docstring 明确写了"**或 2 位省份前缀**", `tests/test_validation.py::test_validation_2digit_province_prefix` 也专门测了 2 位前缀 —— **能力是有的, 调用方从来没用上**。

影响面不小: README 首屏推荐的 `search_judicial(province="北京")` 属于这条路径。因为统一工具会吞掉单端错误, 用户表面上拿得到结果, 实际上**阿里那一半恒定缺失**, 只剩京东 —— 恰好违背了本项目"双端聚合, 一次拿全量"的核心卖点。

- 修复: `expected_prefix = code[:2] if code.endswith("0000") else code[:4]`
  (`location_codes` 显式透传那条分支 `server.py:234` 同样需要处理)

## P0-3 · 查"荆州市"返回的是"荆门市"

`ali_h5_client.py:58-62` 的城市匹配:

```python
cn = city.rstrip("市地区州盟自治州")
c_match = next((c for c in ... if cn in c["name"] or c["name"].startswith(cn)), None)
```

`rstrip` 收的是**字符集合**而非后缀串, 所以 `"荆州市"` 会被连着剥掉 `市` 和 `州`, 只剩 `"荆"`。再拿 `"荆"` 去子串匹配, 湖北省列表里 **荆门市 (420800) 排在 荆州市 (421000) 前面**, 于是先命中荆门:

```
resolve_area_ali('湖北','荆州市') = 420800   # 应为 421000
```

**最危险的地方是守门抓不住**: `expected_prefix` 由同一个错码算出 (`4208`), 阿里如实返回荆门的标的, 全部以 `4208` 开头 —— 守门判定"完全合规"。用户得到一份**看起来完全正常、实际是隔壁城市**的司法拍卖数据, 没有任何告警。对一个用于资产尽调的工具, 这比报错严重得多。

`resolve_area` (2020 版, 供 `ali_get_supported_areas` 展示) 有同样的问题。

## P1-4 · 另有 23 个区县被解析到相邻行政区

同一根因在区县级 (`dn = district.rstrip("区县市旗")` + 子串匹配 + 无精确优先)。对 legacy 数据集全量 3146 个区县做往返测试, 23 个解析错误:

```
河北 石家庄 井陉县   应130121 → 130107 (井陉矿区)
河南 鹤壁   淇县     应410622 → 410611 (淇滨区)
河南 新乡   辉县市   应410782 → 410781 (卫辉市)
湖南 岳阳   岳阳县   应430621 → 430602 (岳阳楼区)
广东 梅州   梅县     应441421 → 441402 (梅江区)
内蒙 鄂尔多斯 鄂托克旗 应150624 → 150623 (鄂托克前旗)
湖北 荆州   (全部 10 个区县)      → 420800 (整城串到荆门)
… 共 23 条
```

典型模式是 **"X县" 被吃成 "X" 后先命中同名的 "X区"/"X矿区"/"X前旗"**。同样是静默返回错区数据。

**对照组**: 京东端 `_match_name` 因为**第一步就做精确匹配** (`if query in candidates`), 全量 33 省 / 455 市 / 5344 区县往返 **零误配**, 去后缀简称 (如"苏州"→"苏州市") 也是零误配。两端写法不一致, 阿里端缺的正是这一步。

**验证过的修复**: 把阿里端也改成 `精确 → 去后缀后精确 → 前缀` 三级 (放弃无序子串匹配):

```
[legacy] 修复后 市级误解析 0/344, 区县级误解析 0/3146
[2020]   修复后 市级误解析 0/342, 区县级误解析 0/3056
柯桥区 → 330600 ✓ (仍正确回退到城市级, 交给动态学码)
上虞区 → 330682 ✓   荆州市 → 421000 ✓   广州/广东省简称 ✓
```

**24 处错误全部归零, 且现有测试断言的柯桥/上虞/简称行为均未改变。**

## P1-5 · 单源工具网络异常裸抛

`search_judicial` 用 try/except 把两端异常都包成了结构化错误, 但 `ali_search_judicial` / `jd_search_judicial` 作为独立注册的 MCP 工具**没有任何兜底**。本次代理 403 就是现成的样本:

```
ali_search_judicial 未捕获异常: ProxyError: 403 Forbidden
jd_search_judicial  未捕获异常: ProxyError: 403 Forbidden
search_judicial 统一工具: {'sources': [], 'errors': {'ali': {...}, 'jd': {...}}}  ← 正常降级
```

超时、DNS、连接重置同理。对常驻 MCP server 而言, 这会让 agent 侧拿到一个 traceback 而不是可读的错误语义。建议把统一工具里那段 try/except 下沉到两个单源工具内部。

## P1-6 · 并发下 token 自愈竞态

`ali = AliH5Client()` 是模块级单例, 而 FastMCP 会把同步工具丢进 worker 线程池 —— 两个并发请求会同时操作同一个 `_tk_token`。自愈分支 (`ali_h5_client.py:213-219`) 先把 token 置 `None`, 再花一次网络往返去 bootstrap; 这个窗口内另一个线程的 `_bootstrap_token()` 因为"已有 token 就 return"的短路逻辑不会等待, 直接拿 `None` 去签名。确定性复现:

```
并发线程在自愈窗口内读到的 token: None
它算出的 sign 基于字符串: None&1700000000000&12574478&...
→ 该请求必然 Sign Error, 并会再次触发自愈, 形成互相踩踏
```

结果是"一个 token 过期"被放大成"多个请求连环失败 + 重复 bootstrap"。建议给 bootstrap/自愈加一把 `threading.Lock`, 并在锁内二次确认 token 是否已被别的线程刷新过 (double-checked locking)。

## P2-7 · 学码无负缓存 + 未知省份发垃圾查询

`learn_district_code_from_city` 只在**命中时**写 `_DISTRICT_CODE_CACHE`, 失败不记。于是每次查同一个学不到的区县, 都要重新翻最多 5 页 (`max_pages=5`) 真实请求。对常驻服务是稳定的重复开销, 也白白增加风控画像。

更糟的是未知省份路径 (`server.py:252-258`): 此时 `city_code` 是空串, 代码仍继续往下走:

```
未知省份+district → 触发的底层查询次数: 2
  发出的 location_codes: [['0000'], ['']]
重复同一查询 → 又发了 2 次网络请求 (无负缓存)
```

`['0000']` 和 `['']` 都是无意义编码, 且因 `expected_prefix` 为 `None` 而**绕过守门**, 最后只剩一层 title 过滤。建议: 省/市解析不出来时直接返回结构化错误 (对齐已有的 `district_requires_city`), 并给学码加负缓存 (可带 TTL)。

## P2-8 · limit / page 缺校验

```
limit=0          → count=0
limit=-1         → count=4     ← 静默丢掉最后一条 (items[:-1])
limit=1000000000 → count=5
page=-5          → 原样透传给上游
```

`limit=-1` 这种是典型的切片语义泄漏: 不报错, 只是悄悄少一条。建议 `limit = max(1, min(int(limit), 50))` (README 已写"上限建议 50"), `page = max(1, int(page))`。

## P2-9 · `validated` 是死变量

`server.py:288` 置 `True` 之后再无写入 —— 校验失败的分支是提前 `return` 的。因此 `"validated": validated` **恒为 True**, 却被写进了 docstring 的返回契约 (`server.py:223`), 会让调用方误以为这是个真实信号。要么删掉, 要么让它承载真实信息 (如 `validated: false` 表示本次未做校验)。

## P2-10 · `ali_get_filter_options` 的 ret 解析会崩

同一个文件里两种写法:

```python
server.py:273  ret_first = (r.get("ret") or [""])[0] if isinstance(r.get("ret"), list) else str(...)  # 健壮
server.py:331  if r.get("ret", [""])[0] != "SUCCESS::调用成功":                                        # 脆弱
```

`ret` 为 `None` 时后者直接崩:

```
ali_get_filter_options 崩溃: TypeError: 'NoneType' object is not subscriptable
ali_search_judicial 同场景: {'error': 'mtop_call_failed', 'ret': None, ...}   ← 正常降级
```

建议抽一个 `_ret0(resp)` helper 统一两处 (`ali_h5_client.py:208` 是第三处)。

## P3-11 · 测试 badge 口径

badge 写 "tests 47 passing", 实际默认只跑 28 个; 另外 19 个标了 `@pytest.mark.live`, 需要 `--run-live` 且能直连阿里/京东。建议标成 "28 offline + 19 live" 之类, 避免读者以为 CI 全绿覆盖了 47 项。

---

## 做得好的地方

不止是找问题, 以下几点是实测确认过的优点:

- **京东地区解析零缺陷** — 全量 33 省 / 455 市 / 5344 区县往返零误配, 去后缀简称同样零误配。精确匹配优先这一步是对的, 阿里端应当照抄。
- **三份数据集的数量声称全部属实** — GB2260 2020 版 31/342/3056, legacy 34/344/3146, 京东 33/455/5344, 与 README 和代码注释逐项一致。这类数字通常是文档腐烂重灾区, 这里没有。
- **守门机制的设计判断是对的** — "上游不报错而静默返垃圾"确实是这类抓取最阴险的失败模式, 主动校验 + 结构化错误的思路很好, 问题只出在调用方传错了前缀粒度 (P0-2), 而非机制本身。
- **非 JSON 风控页容错扎实** — 阿里 baxia HTML / 京东 403 都转成了结构化错误而非异常, 且有针对性测试覆盖。
- **测试有真实回归价值** — 京东 cityId/provinceId 的硬编码真值断言 (吴江 39628 / 重庆 4 / 山东 13) 锁住的正是最容易悄悄漂移的东西; 柯桥"不该在 legacy 命中"这条**反向断言**尤其见功力。
- **单位归一的取舍是对的** — 阿里分 / 京东元的差异在工具层抹平, 而不是甩给 LLM 记, 这是正确的抽象位置。
- **vintage 错位问题的发现与文档化** — pre-2013 编码这个坑本身很难发现, README 里的警示写得清楚且有说服力。

---

## 建议的修复顺序

1. `requirements.txt` 钉 `mcp<2` — 一行, 解除"装不上"
2. 省级前缀 `code[:2]` — 一行, 恢复阿里端省级查询
3. 阿里端名称匹配改精确优先 — 已验证 24 处错误归零, 且不破坏现有测试
4. 单源工具补 try/except; token 自愈加锁
5. limit/page 校验; 学码负缓存; `validated` 与 `ret` 解析清理
6. 补测试: 省级前缀回归、荆州/井陉类同名邻区回归、全量数据集往返 (这三类都可零网络覆盖)

---

# 附录: 冒烟测试与优化建议 (2026-09-17)

线上仍被组织出口策略封禁 (`connect_rejected` 403 → `h5api.m.taobao.com` / `api.m.jd.com`), 故冒烟分两层, 均零网络:

- **L1 协议层** — 以真实 MCP client 起 `server.py` 子进程, 走完整 stdio `initialize` / `tools/list` / `tools/call`
- **L2 管线层** — `httpx.MockTransport` 扮演**完全配合**的上游 (如实按请求编码返回该地区正确数据), 跑 解析→组包→解析响应→归一→合并→信封

已固化为 `tests/test_smoke.py`。全量: **34 passed / 19 skipped(live) / 3 xfailed**。

## 冒烟结果

**通过**: 6 工具全部注册且 schema 正常; 零网络工具 (`*_get_supported_areas`) 端到端可调; 阿里出站 sign 为 32 位 MD5、`sort=501`、`statusOrders=["0","1"]` 正确; 京东出站中文名已解析成真实 id (广东=19 / 广州=1601); 双端聚合、分→元归一、价格降序、单端故障降级全部正确。

**两个 P0 在"上游完全配合"的前提下复现** —— 证明缺陷 100% 在客户端:

| 场景 | 上游返回 | 客户端结果 |
|---|---|---|
| `province="广东"` | 6 条**合法广东**标的 | `error: ali_returned_unscoped_results`, 全量丢弃 |
| `city="荆州市"` | 按发来的 `['420800']` 如实返回荆门标的 | `validated: true`, 3 条**荆门**数据零告警 |

协议层还坐实了 P1-5: `ali_search_judicial` / `jd_search_judicial` 在 MCP 层直接 `isError=True` 吐 Python 异常串, 而 `search_judicial` 正常降级。

## 实测开销 (模拟单次上游往返 120ms)

```
冷启动首次 ali_search(省市级)          2 次上游   245ms  串行
热态      ali_search(省市级)          1 次上游   121ms
search_judicial 双端(省市级)           2 次上游   123ms  并行 ✓
ali_search(district 学码失败)          6 次上游   728ms  串行 ✗
  同一 district 再查                   6 次上游   728ms  ← 无负缓存, 每次重复付
search_judicial(district 学码失败)     7 次上游   729ms  ← 并行优势被完全吃掉
```

**先说不用优化的**: 三份地区数据 (共 921KB) 冷启动解析仅 **10.2ms**, 查表 **5–15µs/次**。相比一次上游往返可忽略, **不要在这上面做缓存或索引优化**。唯一值得留意的是常驻内存 **13.5MB**, 对 stdio server 偏重但不致命。

## 优化建议 (按性价比排序)

1. **学码加负缓存** — 失败结果也写 `_DISTRICT_CODE_CACHE` (可带 TTL)。第 2 次起 728ms → 121ms, **省 83%**。一处改动, 收益最大。
2. **学码 5 页并行** — 当前 `for page in 1..5` 纯串行。改 `ThreadPoolExecutor` 并发取页, 首次 728ms → ~245ms。与 1 叠加后该路径基本无感。
3. **学到的码落盘** — 在 `jd_areas.json` 旁维护 `ali_district_codes.json`, 把学到的 (及确认学不到的) 区县码持久化。stdio server 每次被客户端拉起都是新进程, 进程级缓存跨会话全丢; 落盘后长期趋近 **0 次额外请求**。
4. **冷启动预热 token** — 首次查询要多付一次 bootstrap (245ms vs 121ms)。server 启动时后台线程预热 `_bootstrap_token()`, 用户第一次调用即热态。
5. **token 自愈加锁** — 见 P1-6。并发下会用 `None` 签名并互相踩踏, 把"一次 token 过期"放大成连环失败 + 重复 bootstrap。double-checked locking 即可。
6. **单源工具补 try/except** — 见 P1-5。把统一工具那段兜底下沉, 让三个工具的错误语义一致。
7. **`ali_search_judicial` 的价格单位** — 单源返回的 `currentPrice` 是**分**, 京东同名字段是**元**, 相差 100 倍, 而该工具 docstring **全文未提单位**。LLM 直接汇报会把 1.25 亿读成 1250 亿。建议单源也输出 `price_yuan`, 或至少在 docstring 和字段名上标明 (`currentPrice_fen`)。
8. **分页语义** — 阿里 10 条/页、京东 40 条/页, 合并后按价格降序。`limit` 可到 50, 但阿里每页最多只贡献 10 条, 且跨页不是全局有序。文档里点明, 或让统一工具按 limit 自动多取阿里几页。


---

# 附录二: 修复记录 (2026-09-17)

13 项全部修复, 全量 **67 passed / 19 skipped(live)**, 且在全新 venv 里从零安装验证通过。

## 根因性修复

**名称匹配改三级 (P0-3 / P1-4 / 直辖市)** — `str.rstrip` 吃的是**字符集合**不是后缀串, `"荆州市"` 被连剥 `市`+`州` 只剩 `"荆"`, 再做无序子串匹配就先命中了排在前面的兄弟行政区。改为 `_pick()`: 精确 → 去后缀后精确 → 前缀, 放弃子串匹配。顺带补 `_municipality_district()` 跨 `市辖区`/`县` 中间节点查找。

全量往返从 **24 处误解析 → 0**, 两份数据集皆然, 且柯桥回退/上虞/简称等原有断言不变:

```
[legacy] 市级误解析 0/344, 区县级误解析 0/3146
[2020]   市级误解析 0/342, 区县级误解析 0/3056
上海 浦东新区 → 310115   北京 朝阳区 → 110105   (原先都退化到省级)
```

**守门前缀按编码粒度推导 (P0-2)** — 新增 `scope_prefix()`: 省级 `440000`→`'44'`, 市级 `440100`→`'4401'`, 区县级 `330621`→`'3306'`(退到市级粒度校验, 够拦全国乱掺又不误杀)。原先一律 `code[:4]` 得到 `'4400'`, 而真实区县码是 `4401xx`/`4403xx`…, 没有一个以 `'4400'` 开头。district 分支也改为按**实际发出的编码**推导, 直辖市区县 `310115` 因此能算出 `'3101'` 而非 `city_code` 的 `'3100'`。

**token 自愈加锁 (P1-6)** — 拆出 `_fetch_token()` (网络边界) 与 `_invalidate_token(stale)` (幂等作废)。`_bootstrap_token` 用双重检查加锁; 作废时只有"当前 token 仍是我签名用的那个"才动手, 否则复用别的线程已刷新的结果。8 线程并发冷启动只取 1 次 token, 5 线程撞同一个 stale token 只换 1 次。

## 其余修复

| 项 | 做法 |
|---|---|
| P0-1 | `requirements.txt` 钉 `mcp>=1.0,<2`; `pytest` 拆到 `requirements-dev.txt` |
| P1-5 | 单源工具实现体拆成 `_ali_search_impl`, 外层统一兜成 `{error, exception, exception_type}` |
| P2-7 | 学码失败也写缓存 (值为 `None`); 地区解析不出来直接返 `area_not_resolved`, 不再发 `['0000']`/`['']` |
| P2-8 | `_clamp_page` / `_clamp_limit`, limit 归一到 `[1, 50]` |
| P2-9 | `validated` 改为真实信号 (是否跑过校验) |
| P2-10 | 抽 `_ret0()` 统一三处 `ret` 解析 |
| P3-11 | badge 改 "67 offline + 19 live" |
| 新-13 | 单源阿里结果补 `price_yuan`; README 与 server instructions 均标明单位陷阱 |

## 性能 (模拟单次上游往返 120ms)

```
                                    修复前            修复后
ali_search(district 学码失败)    6 次 728ms 串行  →  6 次 368ms 并行   -49%
  同一 district 再查              6 次 728ms      →  1 次 121ms        -83%
search_judicial(district)        7 次 729ms      →  2 次 123ms        -83%
```

学码第一页仍单独取 (多数情况首页即命中, 且据此判断是否还有后续页), 剩余页才并行, 避免为只有几条标的的小城市白白打满 `max_pages`。

## 刻意未做

- **学到的码落盘** — 收益确实大 (stdio server 每次拉起都是新进程, 进程级缓存跨会话全丢), 但要引入运行时写文件与缓存失效策略, 超出本次修复范围。
- **冷启动预热 token** — 会在 import 期发起网络请求, 破坏离线测试与纯函数导入, 得先有惰性/可关开关再做。
- **分页语义** — 阿里每页最多贡献 10 条而 `limit` 可到 50, 跨页非全局有序。已在文档点明, 未改行为。

## 线上验证仍缺

本环境全程无法直连 `h5api.m.taobao.com` / `api.m.jd.com` (组织出口策略 403)。所有修复都由全量数据集往返、MockTransport 全链路、MCP 协议层三重离线验证覆盖, 但**真实上游的响应结构与编码 vintage 未能再确认一次**。建议在可直连的环境跑一次 `pytest --run-live`, 重点看省级查询与直辖市区县这两条新通的路径。
