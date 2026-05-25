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


def _match_name(query: str | None, candidates: dict[str, Any]) -> tuple[str, Any] | None:
    """模糊匹配中文名: 精确 → 前缀 → 包含. 去尾缀 '省/市/区/县/旗' 后再试."""
    if not query:
        return None
    if query in candidates:
        return query, candidates[query]
    q = query.rstrip("省市区县旗自治区盟自治州")
    for k, v in candidates.items():
        ks = k.rstrip("省市区县旗自治区盟自治州")
        if k.startswith(query) or ks == q or ks.startswith(q) or q in ks:
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
        m = _match_name(province, JD_AREAS)
        if not m:
            return out
        prov_name, prov = m
        out["positionProvinceId"] = prov["id"]
        out["multiProvinceIds"]   = prov["id"]
        out["multiProvinceNames"] = prov_name
        if not city:
            return out
        cm = _match_name(city, prov["cities"])
        if not cm:
            return out
        city_name, c = cm
        out["positionCityId"]   = c["id"]
        out["multiCityIds"]     = c["id"]
        out["positionCityNames"] = city_name
        out["multiCityNames"]   = city_name
        if not district:
            return out
        dm = _match_name(district, c["counties"])
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
