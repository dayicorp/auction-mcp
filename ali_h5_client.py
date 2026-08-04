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
import hashlib, json, time
from typing import Any
import httpx

from auction_mcp_assets import load_json

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
GB2260: list[dict[str, Any]] = load_json("gb2260.json")
GB2260_LEGACY: list[dict[str, Any]] = load_json("gb2260_200712.json")


def _pad_code(code: str) -> str:
    """GB 2260 码 zero-pad 到 6 位 (省 2→6, 市 4→6, 区 6→6)."""
    return (code + "0000")[:6]


def _resolve_in(dataset: list[dict[str, Any]],
                province: str | None, city: str | None,
                district: str | None) -> str | None:
    """通用的省/市/区中文名 → GB 2260 6 位编码解析器. dataset 决定 vintage."""
    if not province:
        return None
    pn = province.rstrip("省市自治区")
    p_match = next((p for p in dataset
                    if pn in p["name"] or p["name"].startswith(pn)), None)
    if not p_match:
        return None
    if not city:
        return _pad_code(p_match["code"])
    cn = city.rstrip("市地区州盟自治州")
    c_match = next((c for c in p_match.get("children", [])
                    if cn in c["name"] or c["name"].startswith(cn)), None)
    if not c_match:
        return _pad_code(p_match["code"])
    if not district:
        return _pad_code(c_match["code"])
    dn = district.rstrip("区县市旗")
    d_match = next((d for d in c_match.get("children", [])
                    if dn in d["name"] or d["name"].startswith(dn)), None)
    if not d_match:
        return _pad_code(c_match["code"])
    return _pad_code(d_match["code"])


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

# 进程级缓存: (city_4digit_prefix, district_name) -> Ali-vintage 区县编码
_DISTRICT_CODE_CACHE: dict[tuple[str, str], str] = {}


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

def derive_ali_scope_prefix(code: str | None) -> str | None:
    """从 Ali 地区编码推导守门校验前缀 (纯函数, 无副作用).

    规则:
      - 2 位 (XX) 或 6 位省级 (XX0000) → 返回 2 位省前缀 "XX"
      - 4 位 (XXXX) 或 6 位市级 (XXXX00) → 返回 4 位城市前缀 "XXXX"
      - 6 位区县级 (XXXXXX, 末两位非零) → 返回前 4 位城市范围 "XXXX"
      - None / 空串 / 非纯数字 / 长度不是 2、4、6 → 返回 None
      - 超过 6 位的编码禁止静默截断, 返回 None
    """
    if not code:
        return None
    s = str(code).strip()
    if not s or not s.isdigit():
        return None
    if len(s) == 2:
        return s
    if len(s) == 4:
        return s
    if len(s) == 6:
        if s[2:] == "0000":
            return s[:2]
        return s[:4]
    # 长度不是 2/4/6: 禁止静默截断
    return None


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

    def _bootstrap_token(self):
        """Hit any mtop endpoint to make server set _m_h5_tk cookie."""
        if self._tk_token:
            return
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
        self._tk_token = tk_full.split("_", 1)[0]

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
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        resp = self._do_call(api, version, data_str, method)
        ret0 = ""
        try:
            ret0 = (resp.get("ret") or [""])[0]
        except Exception:
            pass
        if any(m in ret0 for m in self._TOKEN_ERROR_MARKERS):
            # 单次自愈重试: 清 token + cookie, 重 bootstrap, 再调一次
            self._tk_token = None
            try:
                self.s.cookies.delete("_m_h5_tk")
                self.s.cookies.delete("_m_h5_tk_enc")
            except Exception:
                pass
            self._bootstrap_token()
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
        # 缓存键: 4 位城市前缀 + 去后缀的区县名
        key = (city_code[:4], district_name.rstrip("区县市旗"))
        if key in _DISTRICT_CODE_CACHE:
            return _DISTRICT_CODE_CACHE[key]

        # 待匹配的目标 (短名 / 全名都接受)
        targets = {district_name, district_name.rstrip("区县市旗")}
        targets = {t for t in targets if t}

        loc = _pad_code(city_code)
        for page in range(1, max_pages + 1):
            r = self.search_judicial(page=page, location_codes=[loc])
            scenes = ((r.get("data") or {}).get("data") or {}).get("scenes") or []
            if not scenes:
                break
            sl = (scenes[0].get("schemeList") or [{}])[0]
            content_list = sl.get("contentList") or []
            if not content_list:
                break
            for it in content_list:
                em = it.get("extraMap") or {}
                title = em.get("title") or it.get("title") or ""
                if not any(t and t in title for t in targets):
                    continue
                lc = em.get("locationCode") or it.get("locationCode")
                if lc is None:
                    continue
                lcs = str(lc)
                _DISTRICT_CODE_CACHE[key] = lcs
                return lcs
            # 没分页信息时 contentList 不满一页就停
            if len(content_list) < 10:
                break
        return None

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
