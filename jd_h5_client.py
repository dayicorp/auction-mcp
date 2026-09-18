"""京东司法拍卖 m. 版客户端 — 完全绕过 h5st 风控.

发现路径 (Playwright 实测):
  1. iPhone UA 访问 https://auction.jd.com/sifa.html → 跳到 m. 版 JDReact 容器
     https://pmthr.m.jd.com/dynamic?appletsCode=judicature_search_home&jdreactkey=JDReactPaimaiIndexThree
  2. 该 page 调 api.m.jd.com/api?functionId=getSearchData (POST form-urlencoded)
  3. 业务参数在 form body 的 `body` 字段 (JSON 字符串)
  4. 风控字段 h5st / x-api-eid-token **可省略**, server 仍返回 code=0 + 完整数据
  5. 地区树由 functionId=getAreaInfoMap 提供 (无 sign 无登录), 已一次性抓取冻结到 jd_areas.json.

数据量: 总计 250 万+ 司法拍卖标的. 每页默认 40 条.
地区: 33 省 / 455 市 / 5344 区县 (jd_areas.json), 区县级用 multiCountyIds 过滤.
"""
from __future__ import annotations
import json, os, time
from typing import Any
import httpx

API = "https://api.m.jd.com/api"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
REFERER = "https://pmthr.m.jd.com/dynamic?appletsCode=judicature_search_home"


# ============================================================ 地区树
# jd_areas.json 由 getAreaInfoMap 三层级联拉取生成 (一次性, 离线冻结).
# 结构: [{id, name, children: [{id, name, children: [{id, name}]}]}]
_AREAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jd_areas.json")
with open(_AREAS_PATH, "r", encoding="utf-8") as _f:
    _AREAS_TREE: list[dict[str, Any]] = json.load(_f)


def _build_lookup(tree: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{prov_name: {id, cities: {city_name: {id, counties: {cty_name: id}}}}}"""
    out: dict[str, dict[str, Any]] = {}
    for p in tree:
        cities = {}
        for c in p.get("children", []):
            counties = {d["name"]: d["id"] for d in c.get("children", [])}
            cities[c["name"]] = {"id": c["id"], "counties": counties}
        out[p["name"]] = {"id": p["id"], "cities": cities}
    return out


# 主映射表. 33 省全覆盖, 比早期手写的 10 省版本远更完整 + 实测真值.
JD_AREAS: dict[str, dict[str, Any]] = _build_lookup(_AREAS_TREE)


# 行政区划后缀, **按层级分开**. 与 ali_h5_client 的 _SFX_* 保持一致.
#
# 注意 str.rstrip 吃的是**字符集合**而非后缀串: 用一个大集合通吃三级时,
# "定州市" 会被连剥 市/州 只剩 "定", "辉县市" 剩 "辉" —— 再配上无序子串匹配就会
# 串到兄弟行政区去 (实测 海州→东海县 / 定州→定兴县 / 黄州→黄梅县).
# 区县级的集合**刻意不含 '州'**, 这样 "定州市"/"海州区" 只剥到 "定州"/"海州", 天然唯一.
_SFX_PROV = "省市自治区"
_SFX_CITY = "市地区州盟自治州"
_SFX_DIST = "区县市旗"

# 直辖市. 京东地区树对这 4 个是 省 → 区 两层 (没有"市"这一级), 与其他省份的
# 省 → 市 → 区县 三层不同, 解析和层级判定都要特殊处理.
MUNICIPALITIES = frozenset({"北京", "上海", "天津", "重庆"})


def _match_name(query: str | None, candidates: dict[str, Any],
                suffixes: str = _SFX_DIST) -> tuple[str, Any] | None:
    """按 精确 → 去后缀后精确 → 前缀 三级匹配中文名. **不做无序子串匹配**.

    三遍独立循环而非一遍内 or 掉四个条件: 强条件必须全局优先, 否则只满足最弱条件的
    候选只要排在前面就会压过后面本该精确命中的那个 —— 这正是京东端 24 个区县静默串区的根因.

    suffixes 按层级传 (_SFX_PROV / _SFX_CITY / _SFX_DIST), 默认区县级.
    """
    if not query:
        return None
    if query in candidates:                                   # 1. 精确
        return query, candidates[query]
    q = query.rstrip(suffixes)
    if q:
        for k, v in candidates.items():                       # 2. 去后缀后精确
            if k.rstrip(suffixes) == q:
                return k, v
    for k, v in candidates.items():                           # 3. 前缀 (模糊兜底)
        if k.startswith(query) or (q and k.startswith(q)):
            return k, v
    return None


# ============================================================ 客户端

class JDH5Client:
    """京东 m. 版 mtop 客户端 — 无 sign 无登录态."""

    def __init__(self):
        self.s = httpx.Client(
            headers={
                "User-Agent": MOBILE_UA,
                "Accept": "application/json",
                "Referer": REFERER,
                "Origin": "https://pmthr.m.jd.com",
            },
            timeout=20.0,
            follow_redirects=True,
        )

    def _resolve_area(self, province: str | None = None, city: str | None = None,
                       district: str | None = None) -> dict[str, Any]:
        """中文 省/市/区县 → JD 搜索 params (multiProvinceIds / multiCityIds / multiCountyIds + Names).

        不认识的层级 silent skip (不报错, 也不乱传错码触发 server 静默全国 fallback).
        district 必须配 city, city 必须配 province.
        """
        out: dict[str, Any] = {}
        if not province:
            return out
        m = _match_name(province, JD_AREAS, _SFX_PROV)
        if not m:
            return out
        prov_name, prov = m
        out["positionProvinceId"] = prov["id"]
        out["multiProvinceIds"]   = prov["id"]
        out["multiProvinceNames"] = prov_name
        if not city:
            return out
        cm = _match_name(city, prov["cities"], _SFX_CITY)
        if not cm:
            # 直辖市: 京东的树是 省 → **区**, 根本没有"市"这一级, 所以 "上海市" 匹配不到
            # 任何候选。不特殊处理的话 district 连试都不会试一次, 查"上海市浦东新区"
            # 会静默返回**全上海** (实测混进黄浦区的标的)。用户给的 district 其实就挂在这一层。
            if district and prov_name in MUNICIPALITIES:
                dm2 = _match_name(district, prov["cities"], _SFX_DIST)
                if dm2:
                    d_name, d = dm2
                    out["positionCityId"]    = d["id"]
                    out["multiCityIds"]      = d["id"]
                    out["positionCityNames"] = d_name
                    out["multiCityNames"]    = d_name
            return out
        city_name, c = cm
        out["positionCityId"]   = c["id"]
        out["multiCityIds"]     = c["id"]
        out["positionCityNames"] = city_name
        out["multiCityNames"]   = city_name
        if not district:
            return out
        dm = _match_name(district, c["counties"], _SFX_DIST)
        if not dm:
            return out
        county_name, county_id = dm
        out["multiCountyIds"]   = county_id
        out["multiCountyNames"] = county_name
        return out

    # 默认产品行为, 不暴露:
    #   sortField=7        → 当前价格由高到低 (价格降序)
    #   multiStatus=101,102 → 仅 进行中(101) + 预告中/即将开始(102)
    #   实测: 不传 multiStatus 时结果混入已结束/已撤回 (displayStatus 5/6),
    #         传 "101,102" (逗号字符串, 非数组) 才正确过滤. 数组格式被 server 忽略.
    DEFAULT_SORT = "7"
    DEFAULT_STATUS = "101,102"

    def search_judicial(self, page: int = 1, province: str | None = None,
                        city: str | None = None,
                        district: str | None = None) -> dict[str, Any]:
        """京东司法拍卖搜索. 固定 价格降序 + 仅进行中/即将开始.

        Args:
            page: 页码 (40 条/页)
            province: 省份中文名 (e.g. "广东"). 全 33 省支持.
            city: 城市中文名 (e.g. "广州市"). 必须配 province.
            district: 区/县中文名 (e.g. "吴江区"). 必须配 city. 5344 个区县全覆盖.
        """
        search_params: dict[str, Any] = {
            "reqSource": 1,
            "appletsCode": "judicature_search_home",
            "sortField": self.DEFAULT_SORT,
            "multiStatus": self.DEFAULT_STATUS,
        }
        search_params.update(self._resolve_area(province, city, district))

        biz_body = {
            "page": page,
            "tabParam": "all",
            "pageParam": "judicature_search_home",
            "isWaterfallInit": False,
            "searchParamsObj": search_params,
            "callbackParam": {},
            "mergeSearchCondition": {},
        }
        params = {
            "appid": "paimai",
            "functionId": "getSearchData",
            "loginType": "2",
            "time": str(int(time.time() * 1000)),
        }
        form = {
            "body": json.dumps(biz_body, separators=(",", ":"), ensure_ascii=False),
            "appid": "paimai", "functionId": "getSearchData",
            "isM": "true", "clientVersion": "paimai-h5-1.0.0",
            "client": "paimai-h5", "t": str(int(time.time() * 1000)),
        }
        r = self.s.post(API, params=params, data=form)
        try:
            return r.json()
        except (ValueError, json.JSONDecodeError):
            # 撞 403 / WAF / 非 JSON 错误页 - 不抛, 返结构化错误
            return {
                "code": -1,
                "msg": "LOCAL_NON_JSON",
                "_status": r.status_code,
                "_raw_preview": (r.text or "")[:200],
            }
