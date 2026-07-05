# auction-mcp 审计报告

- **审计对象**: `dayicorp/auction-mcp`（司法拍卖聚合 MCP server）
- **审计范围**: `server.py`、`ali_h5_client.py`、`jd_h5_client.py`、`tests/`、打包数据与文档
- **审计日期**: 2026-07-05
- **代码版本**: `71fc1cc`（分支 `main`）
- **审计方式**: 静态代码审阅 + 离线测试套件运行（`28 passed / 19 skipped`，live 集成测试未跑）

---

## 1. 结论速览

代码工程质量在同类"逆向 + 抓取"项目里属于**偏高**的一档：错误处理、单位归一、垃圾结果守门、token 自愈、测试覆盖都做得比较用心，可读性也好。

但项目存在一个**决定性的非技术风险**：它的核心价值主张就是**系统性绕过阿里巴巴与京东的反爬 / 反篡改风控**，并把司法拍卖标的数据（含法院、当事人相关信息）重新聚合分发。这不是一个可以靠改代码消除的 bug，而是关乎项目能否合法存在的前提。技术层面的问题相比之下都是次要的。

| 维度 | 评级 | 说明 |
|---|---|---|
| 合规 / 法律 | 🔴 高 | 明确绕过风控 + 抓取分发第三方数据，ToS 与法律风险贯穿全项目 |
| 安全 | 🟡 中 | 无凭据泄露；主要是共享客户端的并发状态竞争 |
| 并发 / 可靠性 | 🟡 中 | 全局单例 client 的 token/cookie 自愈逻辑无锁 |
| 正确性 | 🟢 低 | 逻辑基本正确，个别边角（模糊匹配误命中、`validated` 恒真）需留意 |
| 工程 / 测试 | 🟡 中 | 测试写得好，但**无 CI**、badge 为手写、核心断言依赖 live API |

---

## 2. 项目概述

一个 FastMCP（stdio）server，暴露 6 个工具，把阿里拍卖与京东司法拍卖两个独立标的池聚合成一个干净接口：

- `search_judicial`（推荐默认）— 用线程池并行打两端，价格降序合并，单位归一到元。
- `ali_search_judicial` / `jd_search_judicial` — 单源高级工具。
- `ali_get_filter_options` / `ali_get_supported_areas` / `jd_get_supported_areas` — 元数据。

两条数据通路：

- **阿里**: 走 H5 mtop 网关 `h5api.m.taobao.com`，sign 用公开 MD5（`MD5(token&t&appKey&data)`，token 取自 `_m_h5_tk` cookie），刻意绕过 app 端 `unifiedSign + wua + sgext`。
- **京东**: 走 `api.m.jd.com/api?functionId=getSearchData`，省略风控字段 `h5st` / `x-api-eid-token`，server 仍返回完整数据。

一个值得称赞的设计亮点：**GB 2260 双 vintage 解析**。阿里 server 实际认的是 pre-2013 编码（柯桥区→绍兴县 330621），传现代 2020 码会**静默返回全国乱掺垃圾**。项目用 `resolve_area_ali`（legacy）+ `learn_district_code_from_city`（动态学码）+ `validate_location_scoped`（≥80% 前缀命中守门）三层组合把这个坑封住，是整个仓库最扎实的部分。

---

## 3. 合规与法律风险 🔴（最高优先级）

这是本次审计的核心结论，独立于代码质量。

**3.1 系统性绕过技术保护措施。** 代码与文档反复、明确地将"绕过反爬 / 反篡改"作为卖点：

- `ali_h5_client.py` 文件头：*"完全 bypass app 端 unifiedSign + wua + sgext anti-tamper"*
- `jd_h5_client.py`：*"完全绕过 h5st 风控 ... 风控字段 h5st / x-api-eid-token **可省略**"*
- README「架构来由」：*"完全 bypass app 端 anti-tamper"*、IDA 逆向 / Substrate tweak / Frida 的历史痕迹（见 `.gitignore` 的 `tweak/*.dylib`、`android/`、`*.i64`）。

绕过网站/App 的访问控制与技术措施、大规模抓取数据，在中国法域下可能触及《反不正当竞争法》第十二条、《数据安全法》、以及"非法获取计算机信息系统数据"相关刑事条款；同时明确违反淘宝/京东开放平台与网站服务条款。这类风险**无法通过修改代码消除**。

**3.2 数据再分发与个人信息。** 司法拍卖标的常含当事人/被执行人、法院、案号等信息。将其批量抓取并通过 MCP 二次分发，触及《个人信息保护法》与数据来源合法性问题。

**3.3 无授权 / 免责声明。** 仓库只有 MIT `LICENSE`（针对本项目代码），没有任何关于数据来源、使用范围、授权边界的免责声明或 `DISCLAIMER`，也未标注"仅供学习研究"。

**建议**：
- 由法务评估项目定位。若继续，至少增加显著的 `DISCLAIMER`，明确数据来源、非商用/研究用途、以及用户自担合规责任。
- 优先探索官方渠道：中国执行信息公开网、人民法院诉讼资产网、淘宝/京东官方开放平台的合规数据接口。
- 至少加入速率限制与 `User-Agent` 诚实声明，避免对目标站点造成访问压力（当前**完全无限流**，见 §5.2）。

---

## 4. 安全发现 🟡

**4.1 无硬编码密钥（好）。** `appKey=12574478` 是公开 H5 mtop appkey；无 token、无密码、无个人凭据入库。`.gitignore` 对 `*.env` / `cookies.json` / `*.pem` / `*.har` / `*.mitm` 的屏蔽较为周全。

**4.2 `sys.path.insert(0, ...)`（`server.py:15`）— 低危。** 把脚本所在目录插到 `sys.path` 最前，理论上可被同目录同名模块劫持 import。stdio 本地场景影响很小，但属于可去除的习惯性风险，建议改用包相对导入或 `-m` 运行。

**4.3 外部响应完全信任。** 两端返回的 JSON 被直接展开进结果 `raw` 字段透传给 LLM/上层。虽然当前只做读取，但 title/shopName 等字段来自不可信来源，下游若据此渲染或执行需自行做转义/清洗——建议在文档中明确"raw 为不可信外部数据"。

**4.4 无请求签名回放/防重防护** — 不适用（这是抓取客户端，不是被调用服务），仅记录。

---

## 5. 并发与可靠性 🟡

**5.1 全局单例 client 的自愈逻辑存在数据竞争（中危）。** `server.py` 在模块级创建 `ali = AliH5Client()` / `jd = JDH5Client()` 全局单例，被所有工具调用共享。`AliH5Client.call_mtop` 在 token 失效时会执行：

```python
self._tk_token = None
self.s.cookies.delete("_m_h5_tk")
self.s.cookies.delete("_m_h5_tk_enc")
self._bootstrap_token()
```

这段**读-改-写共享状态的逻辑没有任何锁**。`search_judicial` 本身用 `ThreadPoolExecutor` 并发打两端（阿里/京东是不同实例，那一处安全），但若 MCP 宿主并发派发多个工具调用（或未来 unified 复用），两个线程同时进入阿里的自愈路径会互相清空对方刚 bootstrap 的 token，导致抖动式失败或反复重取。**建议**给 `_bootstrap_token` + 自愈块加一把 `threading.Lock`。

**5.2 完全无速率限制 / 退避（中危，兼合规）。** 没有任何 QPS 限制、请求间隔或指数退避。`learn_district_code_from_city` 单次兜底最多再打 5 页。高频调用既容易触发目标站风控（`baxia` punish / `x5sec`），也放大 §3 的合规风险。建议引入令牌桶/最小间隔。

**5.3 import 期硬失败。** 三个数据文件在模块导入时 `json.load`，且 `ali`/`jd` 单例在 import 时构造。任一 JSON 损坏或首个 bootstrap 网络异常，会让 `import server` 直接抛错而非返回结构化错误。建议对数据加载与首次 bootstrap 做惰性化 + try/except 兜底。

**5.4 进程级缓存无失效（低危）。** `_DISTRICT_CODE_CACHE` 只增不减、无 TTL；阿里编码若变更需重启进程。对常驻服务是可接受的取舍，记录备忘。

---

## 6. 正确性 🟢

**6.1 `validated` 字段恒为 `True`（低危，易误导）。** `ali_search_judicial` 里 `validated = True` 后，唯一会"不通过"的分支直接 `return {"error": ...}`，因此正常返回的 envelope 里 `validated` **永远是 True**。字段本身无害但语义上是死值，容易让调用方误以为它承载了校验结果。建议移除，或改为真实反映（如无 `expected_prefix` 时标 `None`/`"skipped"`）。

**6.2 中文名模糊匹配可能误命中（低危）。** `_resolve_in`（阿里）与 `_match_name`（京东）都用 `子串 / 前缀` 匹配并去尾缀"省市区县旗"。短名或同前缀地名（例如某些"东城/东区"、"河东/河"类）在极端情况下可能命中错误层级；JD 端 `_match_name` 的 `q in ks` 包含匹配尤其宽松。当前靠阿里侧的前缀守门兜底，但**京东侧没有等价的结果守门**——若 JD 模糊匹配到错误城市，不会被发现。建议 JD 端也加一层"返回结果省/市名与请求一致"的轻校验。

**6.3 未使用的导入。** `server.py:24` 导入了 `resolve_area` 但未使用（server 内地区展示用的是 `GB2260` 直接遍历，查询用 `resolve_area_ali`）。清理即可。

**6.4 京东 `code` 判断。** `jd_search_judicial` 以 `r.get("code") != 0` 判失败，非 JSON 时客户端返 `code=-1`，链路一致，正确。

---

## 7. 测试与 CI 🟡

**7.1 测试质量好，但缺 CI。** 28 个离线单元/容错测试设计到位（守门阈值边界、token 自愈不死循环、LOCAL_NON_JSON 不误判、双 vintage 解析对照）。但仓库**没有 `.github/workflows`**，没有任何持续集成。README 的 `tests 47 passing` badge 是**手写静态值**，非 CI 产出，容易与真实状态脱节。建议加一个 GitHub Actions，至少跑离线套件（live 用 flag 隔离）。

**7.2 关键行为依赖 live API。** 最核心的用户价值（柯桥动态学码、守门捕获 2020 码垃圾、双端聚合）全部在 `@pytest.mark.live` 里，默认 CI 跑不到。目标站点接口/风控一旦变动，离线测试**全绿但功能已坏**。建议为 mtop/JD 响应录制 fixture，做离线回放，把 §5、§6 的行为纳入常态回归。

**7.3 数值断言随行情漂移。** live 测试用范围断言（如 `50 < totalCount < 500`）已是合理做法，但阈值仍可能随真实拍卖量变化而 flaky，记录备忘。

---

## 8. 数据与许可

打包了三份第三方数据：`gb2260.json`（modood/Administrative-divisions-of-China，2020 版）、`gb2260_200712.json`（cn/gb2260 快照）、`jd_areas.json`（京东 `getAreaInfoMap` 抓取产物）。README 已注明来源，但仓库以 MIT 声明整体许可，未单独标注这些数据集各自的许可与署名要求。建议在 `LICENSE` 或 `NOTICE` 中补充来源与许可归属，`jd_areas.json` 的可分发性尤其需确认（属抓取产物，见 §3.2）。

---

## 9. 优先级建议清单

**P0 — 阻断性**
1. 法务评估项目合规定位（§3）；补 `DISCLAIMER`，明确数据来源、用途边界、免责。
2. 引入速率限制 / 请求间隔 / 退避（§5.2），既降合规风险也防目标站封禁。

**P1 — 应尽快**
3. 给阿里 client 的 token 自愈块加锁，消除并发竞争（§5.1）。
4. 加 GitHub Actions 跑离线测试；把手写 badge 换成 CI 产出或删除（§7.1）。
5. 关键功能加录制 fixture 的离线回放，摆脱对 live API 的依赖（§7.2）。

**P2 — 改进**
6. 惰性加载数据 + 首次 bootstrap try/except，避免 import 期硬崩（§5.3）。
7. 京东侧补结果层地区守门，对齐阿里（§6.2）。
8. 移除死值 `validated` 字段与未用的 `resolve_area` 导入（§6.1 / §6.3）。
9. 补第三方数据集的许可归属（§8）；文档标注 `raw` 为不可信外部数据（§4.3）。

---

## 附录：验证记录

```
$ python -m pytest -q          # 离线套件
28 passed, 19 skipped in 1.16s  # 19 skipped = @pytest.mark.live，需 --run-live
```

live 集成测试（真打阿里/京东线上）本次未执行。
