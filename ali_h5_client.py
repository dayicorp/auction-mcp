"""阿里拍卖 H5 (移动版) 客户端 — 完全绕过 app 端 anti-tamper.

发现路径:
  1. 移动浏览器 UA 访问 sf.taobao.com → redirect 到
     pages-fast.m.taobao.com/wow/.../sf-home (移动版司法首页)
  2. 页面 XHR 调 mtop.taobao.datafront.invoke.auctionwalle (跟 app 端同一个 endpoint!)
     但走 H5 网关 h5api.m.taobao.com, sign 是普通 MD5.
  3. Sign 算法 (Playwright 验证): MD5(token + "&" + t + "&" + appKey + "&" + data)
     - token = _m_h5_tk cookie 的 "_" 前 32 字符
     - t     = ms timestamp
     - appKey = 12574478 (H5 mtop appkey)
     - data  = POST body 的 data 字段值 (URL-decoded)
  4. _m_h5_tk cookie 通过普通 GET 请求拿到, server 自动 set-cookie.

完全 bypass app 端 unifiedSign + wua + sgext anti-tamper.
"""
from __future__ import annotations
import concurrent.futures as cf, hashlib, json, os, threading, time
from typing import Any
import httpx

# GB 2260 国标行政区划数据 - 两份, 用途分明:
#
# GB2260        — 2020 版 (modood/Administrative-divisions-of-China),
#                  31 省 / 342 市 / 3056 区县. 给 ali_get_supported_areas 用,
#                  作人类可读的"现在叫什么"展示.
#
# GB2260_LEGACY — pre-2013 (cn/gb2260 200712 快照),
#                  34 省 / 344 市 / 3146 区县. **阿里 server 实际接受的就是这个 vintage**.
#                  例: 柯桥区(2020=330603) 在阿里实际是 绍兴县(330621); 上虞区(2020=330604) 实际是 上虞市(330682).
#                  2020 码直接传给阿里 → 静默返回全国乱掺垃圾, 故必须用 legacy 解析查询.
_GB2260_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gb2260.json")
_GB2260_LEGACY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gb2260_200712.json")
with open(_GB2260_PATH, "r", encoding="utf-8") as _f:
    GB2260: list[dict[str, Any]] = json.load(_f)
with open(_GB2260_LEGACY_PATH, "r", encoding="utf-8") as _f:
    GB2260_LEGACY: list[dict[str, Any]] = json.load(_f)


def _pad_code(code: str) -> str:
    """GB 2260 码 zero-pad 到 6 位 (省 2→6, 市 4→6, 区 6→6)."""
    return (code + "0000")[:6]


# 行政区划后缀. 注意 str.rstrip 吃的是**字符集合**而非后缀串, 所以 "荆州市" 会被连剥
# 市/州 只剩 "荆" — 这正是历史上 "荆州市" 静默串到 "荆门市" 的根因. 因此下面的匹配
# 必须 **精确优先**, 只把去后缀当作最后的模糊兜底.
_SFX_PROV = "省市自治区"
_SFX_CITY = "市地区州盟自治州"
_SFX_DIST = "区县市旗"


def _pick(candidates: list[dict[str, Any]], query: str,
          suffixes: str) -> dict[str, Any] | None:
    """按 精确 → 去后缀后精确 → 前缀 三级匹配. 不做无序子串匹配.

    子串匹配 ("荆" in "荆门市") 会让短名命中排在前面的**兄弟行政区**, 且因为上层的
    expected_prefix 由同一个错码导出, 守门结构性发现不了. 三级匹配把这类静默串区消灭。
    """
    if not query:
        return None
    for c in candidates:                                    # 1. 精确
        if c["name"] == query:
            return c
    q = query.rstrip(suffixes)
    if q:
        for c in candidates:                                # 2. 去后缀后精确
            if c["name"].rstrip(suffixes) == q:
                return c
    for c in candidates:                                    # 3. 前缀 (模糊兜底)
        if c["name"].startswith(query) or (q and c["name"].startswith(q)):
            return c
    return None


def _municipality_district(p_match: dict[str, Any],
                           district: str) -> dict[str, Any] | None:
    """直辖市: 区县挂在 '市辖区' / '县' 这层中间节点下, 需要跨中间节点找.

    北京/上海/天津/重庆 的 legacy 结构是 省 → 市辖区|县 → 区县, 市名 ("上海市") 匹配不到
    中间节点, 若不特殊处理, 区县级查询会一路退化到省级码。
    """
    for sub in p_match.get("children", []):
        hit = _pick(sub.get("children", []), district, _SFX_DIST)
        if hit:
            return hit
    return None


def _resolve_in(dataset: list[dict[str, Any]],
                province: str | None, city: str | None,
                district: str | None) -> str | None:
    """通用的省/市/区中文名 → GB 2260 6 位编码解析器. dataset 决定 vintage.

    解析不到的层级会**逐级降级**返回上一级编码 (区→市→省), 上层据此判断是否需要走
    动态学码兜底. 省份都解析不到才返回 None.
    """
    if not province:
        return None
    p_match = _pick(dataset, province, _SFX_PROV)
    if not p_match:
        return None
    if not city:
        return _pad_code(p_match["code"])

    c_match = _pick(p_match.get("children", []), city, _SFX_CITY)
    if not c_match:
        # 直辖市: "上海市" 匹配不到 "市辖区"/"县" 这层中间节点, 但区县确实在下面
        if district:
            hit = _municipality_district(p_match, district)
            if hit:
                return _pad_code(hit["code"])
        return _pad_code(p_match["code"])

    if not district:
        return _pad_code(c_match["code"])
    d_match = _pick(c_match.get("children", []), district, _SFX_DIST)
    if not d_match:
        return _pad_code(c_match["code"])
    return _pad_code(d_match["code"])


def scope_prefix(code: str | None) -> str | None:
    """由"实际发给阿里的编码"推出守门该用的前缀粒度.

    省级 440000 → '44';  市级 440100 → '4401';  区县级 330621 → '3306'.

    守门要拦的是"阿里不认编码时静默返回的**全国乱掺**数据", 因此区县级查询退到
    市级粒度校验即可, 既够用又不会误杀。历史缺陷正是省级也一律取 code[:4] 得到
    '4400', 而真实区县码是 4401xx/4403xx…, 没有一个以 '4400' 开头, 于是省级查询
    100% 被自己的守门误杀。
    """
    if not code:
        return None
    c = _pad_code(str(code))
    if len(c) < 6:
        return None
    if c.endswith("0000"):
        return c[:2]
    return c[:4]


def resolve_area(province: str | None = None, city: str | None = None,
                 district: str | None = None) -> str | None:
    """中文省/市/区 → GB 2260 6 位编码 (2020 版). 给 ali_get_supported_areas 用 (人类可读)."""
    return _resolve_in(GB2260, province, city, district)


def resolve_area_ali(province: str | None = None, city: str | None = None,
                     district: str | None = None) -> str | None:
    """中文省/市/区 → 阿里 server 实际接受的 GB 2260 编码 (pre-2013 vintage).
    柯桥区→330621, 上虞区→330682, 诸暨市→330681 等. 用于 ali_search_judicial 的真实查询."""
    return _resolve_in(GB2260_LEGACY, province, city, district)


# ============================================================ 守门 + 兜底

# 进程级缓存: (city_4digit_prefix, district_name) -> Ali-vintage 区县编码.
# 值为 None 表示**确认学不到** (负缓存) — 没有它的话, 每次重查同一个学不到的区县
# 都要重新翻 max_pages 页真实请求 (实测 6 次上游 / 728ms), 纯属重复付费。
_DISTRICT_CODE_CACHE: dict[tuple[str, str], str | None] = {}


def _ret0(resp: dict[str, Any]) -> str:
    """安全取 mtop 响应的首个 ret 串. ret 可能是 list / str / None, 一律归一成 str."""
    ret = resp.get("ret") if isinstance(resp, dict) else None
    if isinstance(ret, list):
        return str(ret[0]) if ret else ""
    return str(ret) if ret else ""


def validate_location_scoped(items: list[dict[str, Any]],
                              expected_prefix: str,
                              min_ratio: float = 0.8) -> dict[str, Any]:
    """校验 Ali 返回的 items 是否真在指定城市/省级前缀下.

    expected_prefix: 4 位城市前缀 (e.g. '3306' 绍兴) 或 2 位省份前缀 (e.g. '33' 浙江).
    规则: ≥ min_ratio (默认 80%) 的 items 的 locationCode 以 expected_prefix 开头则 ok.
    空 items 视为 ok (没东西可乱).

    Ali 不认编码时静默返回全国乱掺数据 (e.g. 传 2020 版柯桥 330603, 返回 13万条 locationCode 散落
    150602内蒙/440304深圳 etc.). 此函数识别该场景, 上层据此决定降级/报错.
    """
    if not items:
        return {"ok": True, "matched": 0, "total": 0, "sample_off_prefix": []}
    matched, off = 0, []
    for it in items:
        em = it.get("extraMap") or {}
        lc = em.get("locationCode") or it.get("locationCode")
        if lc is None:
            continue
        lcs = str(lc)
        if lcs.startswith(expected_prefix):
            matched += 1
        else:
            if len(off) < 10:
                off.append(lcs)
    total = len(items)
    ok = (matched / total) >= min_ratio if total else True
    return {"ok": ok, "matched": matched, "total": total, "sample_off_prefix": off}

H5_GATEWAY   = "https://h5api.m.taobao.com"
H5_APPKEY    = "12574478"
MOBILE_UA    = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
HOME_URL     = "https://sf.taobao.com/"


class AliH5Client:
    """Stateless-ish client. Holds httpx Session (cookies)."""

    def __init__(self):
        self.s = httpx.Client(
            headers={"User-Agent": MOBILE_UA, "Accept": "application/json"},
            follow_redirects=True,
            timeout=20.0,
        )
        self._tk_token: str | None = None
        # 保护 _tk_token 的获取与作废. FastMCP 把同步工具丢进 worker 线程池, 并发请求
        # 会同时操作这一个 client; 没有锁的话, 自愈期间 (清 token → 一次网络往返) 另一个
        # 线程会拿 None 去签名, 把"一次 token 过期"放大成连环 Sign Error + 重复 bootstrap.
        self._tk_lock = threading.Lock()

    def _bootstrap_token(self):
        """Hit any mtop endpoint to make server set _m_h5_tk cookie.

        双重检查加锁: 锁外快速放行热路径, 锁内再确认一次 — 避免并发线程重复 bootstrap.
        """
        if self._tk_token:
            return
        with self._tk_lock:
            if self._tk_token:      # 等锁期间已被别的线程刷新
                return
            self._tk_token = self._fetch_token()

    def _fetch_token(self) -> str:
        """真正去拿 _m_h5_tk cookie. 调用方须持有 _tk_lock."""
        # touching any mtop endpoint (even a 'TOKEN_EMPTY' error) makes server set _m_h5_tk
        url = f"{H5_GATEWAY}/h5/mtop.taobao.datafront.invoke.auctionwalle/1.0/"
        params = {
            "jsv": "2.7.5", "appKey": H5_APPKEY, "t": str(int(time.time() * 1000)),
            "sign": "0" * 32, "api": "mtop.taobao.datafront.invoke.auctionwalle",
            "v": "1.0", "type": "originaljson", "dataType": "json",
        }
        self.s.get(url, params=params)  # will error w/ TOKEN_EMPTY but sets cookie
        tk_full = self.s.cookies.get("_m_h5_tk")
        if not tk_full or "_" not in tk_full:
            # try home page as fallback
            self.s.get(HOME_URL)
            tk_full = self.s.cookies.get("_m_h5_tk")
        if not tk_full or "_" not in tk_full:
            raise RuntimeError("failed to obtain _m_h5_tk cookie")
        return tk_full.split("_", 1)[0]

    def _invalidate_token(self, stale: str | None):
        """作废一个已确认失效的 token 并立即换新. 幂等: 若已被别的线程换过就不重复做.

        stale 是调用方签名时用的那个 token — 只有它仍是当前值才动手, 否则说明另一个
        线程已经完成自愈, 直接复用其结果即可。
        """
        with self._tk_lock:
            if self._tk_token != stale:
                return                       # 已被别的线程刷新过
            self._tk_token = None
            try:
                self.s.cookies.delete("_m_h5_tk")
                self.s.cookies.delete("_m_h5_tk_enc")
            except Exception:
                pass
            self._tk_token = self._fetch_token()

    def _sign(self, t_ms: str, data_str: str) -> str:
        raw = f"{self._tk_token}&{t_ms}&{H5_APPKEY}&{data_str}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # mtop 错误 ret 串, 触发 token 重 bootstrap 后单次重试
    _TOKEN_ERROR_MARKERS = (
        "TOKEN_EMPTY", "TOKEN_EXPIRED",
        "ILLEGAL_ACCESS::Sign Error!", "ILLEGAL_REQUEST",
    )

    def _do_call(self, api: str, version: str, data_str: str,
                 method: str) -> dict[str, Any]:
        """单次 mtop 调用 + JSON/HTML 容错. 不做 token 重试 (上层负责)."""
        t = str(int(time.time() * 1000))
        sign = self._sign(t, data_str)
        url = f"{H5_GATEWAY}/h5/{api}/{version}/"
        params = {
            "jsv": "2.7.5", "appKey": H5_APPKEY, "t": t, "sign": sign,
            "api": api, "v": version,
            "type": "originaljson", "dataType": "json",
        }
        if method == "POST":
            r = self.s.post(url, params=params, data={"data": data_str})
        else:
            params["data"] = data_str
            r = self.s.get(url, params=params)
        try:
            return r.json()
        except (ValueError, json.JSONDecodeError):
            # 撞 baxia punish HTML (e.g. 83KB 验证码页) / x5sec 跳转脚本 / 网关错误
            txt = r.text or ""
            return {
                "ret": ["LOCAL_NON_JSON::响应非 JSON, 多为风控页或网关异常"],
                "_status": r.status_code,
                "_raw_preview": txt[:200],
            }

    def call_mtop(self, api: str, version: str, data: dict[str, Any],
                  method: str = "POST") -> dict[str, Any]:
        """mtop 调用入口. 包含:
        - JSON / 非 JSON 响应容错 (返 LOCAL_NON_JSON, 不抛)
        - token 过期/sign 错时, 清缓存重 bootstrap 单次重试 (服务过夜后 _m_h5_tk 会过期, 此处自愈)
        """
        self._bootstrap_token()
        used_token = self._tk_token          # 本次签名用的 token, 自愈时据此判断是否已被换过
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        resp = self._do_call(api, version, data_str, method)
        ret0 = _ret0(resp)
        if any(m in ret0 for m in self._TOKEN_ERROR_MARKERS):
            # 单次自愈重试: 作废旧 token (幂等, 并发安全) + 重新获取, 再调一次
            self._invalidate_token(used_token)
            resp = self._do_call(api, version, data_str, method)
            # 标记一下让上层/测试知道发生过自愈 (不影响业务字段)
            resp["_token_refreshed"] = True
        return resp

    # --------------------------- 拍卖业务 API ---------------------------
    # filter 维度可选值见 memory ali-h5-mtop-recipe.md.
    # 默认: sort=501 (当前价由高到低), statusOrders=[0,1] (进行中+即将开始)
    DEFAULT_SORT = "501"
    DEFAULT_STATUS_ORDERS = ["0", "1"]

    def search_judicial(self, page: int = 1,
                        sort: str | None = None,
                        status_orders: list[str] | None = None,
                        fcat_v4_ids: list[str] | None = None,    # 分类
                        location_codes: list[str] | None = None, # 区县编码, 如 ["330621"]=柯桥
                        provs: list[str] | None = None,
                        citys: list[str] | None = None,
                        circs: list[str] | None = None,          # 拍卖轮次
                        tag_ids: list[str] | None = None,        # 特性标签
                        zc_biz_types: list[str] | None = None,   # 资产类型
                        prov: str = "", city: str = "", location_code: str = "",
                        ) -> dict[str, Any]:
        """司法拍卖列表 — 完整 filter 支持.

        - sort: 排序值. 默认 "501" 当前价由高到低. 可选: 500/1/501/502/503/504/507
        - status_orders: 拍卖状态. 默认 ["0", "1"] 进行中+即将开始
        - fcat_v4_ids: 分类编码列表 (e.g. ["206060601"] 住宅)
        - location_codes: 区县编码列表
        """
        # apply defaults
        if sort is None: sort = self.DEFAULT_SORT
        if status_orders is None: status_orders = list(self.DEFAULT_STATUS_ORDERS)

        filters: dict[str, Any] = {"sort": sort}
        if status_orders:   filters["statusOrders"]  = status_orders
        if fcat_v4_ids:     filters["fcatV4Ids"]     = fcat_v4_ids
        if location_codes:  filters["locationCodes"] = location_codes
        if provs:           filters["provs"]         = provs
        if citys:           filters["citys"]         = citys
        if circs:           filters["circs"]         = circs
        if tag_ids:         filters["tagIds"]        = tag_ids
        if zc_biz_types:    filters["zcBizTypes"]    = zc_biz_types

        filters_str = json.dumps(filters, separators=(",", ":"), ensure_ascii=False)
        df_variables = {
            "page":      page,
            "pageSpmb":  "sf-home",
            "pageSpmcs": "searchlistsf-items",
            "context": {
                "_c_searchlistsf-items": filters_str,
                "prov":         prov, "city": city, "locationCode": location_code,
                "userInfo": json.dumps({"prov": prov, "city": city, "locationCode": location_code},
                                       separators=(",", ":"), ensure_ascii=False),
                "piPageType":  "original",
            },
        }
        data = {
            "dfApp":              "auctionwalle",
            "dfApiName":          "auctionwalle.page.getScenes",
            "dfVariables":        json.dumps(df_variables, separators=(",", ":"), ensure_ascii=False),
            "dfUniqueId":         "sf-home_searchlistsf-items",
            "dfVariablesRecover": "{}",
        }
        return self.call_mtop("mtop.taobao.datafront.invoke.auctionwalle", "1.0", data)

    def learn_district_code_from_city(self, city_code: str, district_name: str,
                                       max_pages: int = 5) -> str | None:
        """兜底: 从城市级搜索结果里反查区县的真实 (Ali-vintage) locationCode.

        legacy 数据集没覆盖到 / Ali 自己又微调时使用. 翻 max_pages 页城市级结果,
        找第一条 title 里命中 district_name 的 item, 取其 locationCode 写缓存.

        Args:
            city_code: 4 位城市前缀 (e.g. '3306' 绍兴), 内部会 zero-pad 到 6 位
            district_name: 区县中文名 (e.g. '柯桥区' 或 '柯桥')
            max_pages: 最多翻几页 (默认 5, 每页 10 条; 50 条仍不命中则放弃)

        Returns: 命中的 locationCode (6 位字符串) 或 None
        """
        if not city_code:
            return None
        # 缓存键: 4 位城市前缀 + 去后缀的区县名. 命中与"确认学不到"都缓存.
        key = (city_code[:4], district_name.rstrip(_SFX_DIST))
        if key in _DISTRICT_CODE_CACHE:
            return _DISTRICT_CODE_CACHE[key]

        # 待匹配的目标 (短名 / 全名都接受)
        targets = {district_name, district_name.rstrip(_SFX_DIST)}
        targets = {t for t in targets if t}

        loc = _pad_code(city_code)

        def fetch(page: int) -> list[dict[str, Any]]:
            r = self.search_judicial(page=page, location_codes=[loc])
            scenes = ((r.get("data") or {}).get("data") or {}).get("scenes") or []
            if not scenes:
                return []
            sl = (scenes[0].get("schemeList") or [{}])[0]
            return sl.get("contentList") or []

        def scan(content_list: list[dict[str, Any]]) -> str | None:
            for it in content_list:
                em = it.get("extraMap") or {}
                title = em.get("title") or it.get("title") or ""
                if not any(t and t in title for t in targets):
                    continue
                lc = em.get("locationCode") or it.get("locationCode")
                if lc is not None:
                    return str(lc)
            return None

        # 第一页单独取: 多数情况首页就能命中, 且能据此判断是否还有后续页,
        # 避免为一个只有几条标的的小城市白白并发打满 max_pages.
        first = fetch(1)
        hit = scan(first)
        if hit is None and len(first) >= 10 and max_pages > 1:
            # 剩余页并行取 — 原先是串行 for 循环, 实测 5 页 = 5 个往返
            with cf.ThreadPoolExecutor(max_workers=min(max_pages - 1, 4)) as ex:
                futures = [ex.submit(fetch, p) for p in range(2, max_pages + 1)]
                for f in futures:
                    try:
                        hit = hit or scan(f.result())
                    except Exception:
                        continue
        _DISTRICT_CODE_CACHE[key] = hit      # hit 为 None 时即负缓存
        return hit

    def get_filter_nav(self) -> dict[str, Any]:
        """拉所有 filter 维度的可选项 (sort/fcatV4Ids/circs/statusOrders/tagIds/zcBizTypes)."""
        df_variables = {
            "page": 1, "pageSpmb": "sf-home", "pageSpmcs": "filtersf-nav",
            "context": {
                "_c_filtersf-nav": "{}",
                "prov": "", "city": "", "locationCode": "", "piPageType": "original",
            },
        }
        data = {
            "dfApp": "auctionwalle",
            "dfApiName": "auctionwalle.page.getScenes",
            "dfVariables": json.dumps(df_variables, separators=(",", ":"), ensure_ascii=False),
            "dfUniqueId": "sf-home_filtersf-nav",
            "dfVariablesRecover": "{}",
        }
        return self.call_mtop("mtop.taobao.datafront.invoke.auctionwalle", "1.0", data)


def main():
    import sys
    c = AliH5Client()
    page = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"[*] fetching司法拍卖 page={page} ...")
    out = c.search_judicial(page=page)
    print(f"[*] ret: {out.get('ret')}")
    if "data" in out:
        scenes = (out["data"].get("data") or {}).get("scenes") or []
        if scenes:
            sl = (scenes[0].get("schemeList") or [{}])[0]
            cl = sl.get("contentList") or []
            print(f"[*] totalCount: {sl.get('totalCount')}  page: {sl.get('page')}  items_in_page: {len(cl)}")
            for i, item in enumerate(cl[:5]):
                em = item.get("extraMap", {}) or {}
                print(f"   [{i}] corp={em.get('corpType')!r:24} fcat={em.get('fcatV4ButtomName')!r:12}")
            if len(cl) > 5:
                print(f"   ... +{len(cl) - 5} more")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    main()
