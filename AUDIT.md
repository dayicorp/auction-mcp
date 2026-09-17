# auction-mcp 项目审计

审计日期: 2026-09-17 · 审计范围: `server.py` / `ali_h5_client.py` / `jd_h5_client.py` / `tests/` / 打包与文档
方法: 静态走读 + 针对性可执行验证 (全量数据集往返测试 / 桩化边界测试 / 并发时序复现)

> 说明: 本环境出口代理封禁了 taobao.com 与 jd.com (403), 因此 **未能做线上端到端验证**。
> 下列结论全部由离线可复现的逻辑推演与数据集全量测试得出, 每条附复现方式。

---

## 结论速览

| # | 级别 | 问题 | 位置 |
|---|---|---|---|
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
