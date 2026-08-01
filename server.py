"""MCP server for 京东 + 阿里 司法拍卖 (实时, 无需 iPad sign bridge).

v2 设计原则:
- 默认双端聚合完全本地 Python httpx + MCP stdio, 零依赖外部设备/桥.
- 阿里 PC 完整筛选是显式 Interactive Advanced 链路, 使用非持久化可见 Chrome;
  不读取、导出或保存用户 Cookie, 登录和验证码只由用户手动完成.
- 阿里端走 H5 mtop 网关 (`h5api.m.taobao.com`), sign 是公开 MD5 算法, 不需要 app 端 anti-tamper SDK.
  实现见 ali_h5_client.py. 这个路径跟 app 拿同一个 endpoint (`mtop.taobao.datafront.invoke.auctionwalle`)
  和同一组数据.
- 京东端走公开 `api.m.jd.com/api` (paimai_unifiedSearch 等 functionId 不需要 sign).
- 阿里 location 编码直接用国标 GB 2260 (前 2 省 + 中 2 市 + 末 2 区, server 自动展开 prefix).
"""
from __future__ import annotations
import concurrent.futures as cf, json, logging, os, sys, time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Silence httpx + httpcore INFO logs (would pollute MCP stdio if accidentally to stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from mcp.server.fastmcp import FastMCP

from ali_h5_client import (
    AliH5Client, resolve_area, resolve_area_ali,
    validate_location_scoped, derive_ali_scope_prefix, GB2260,
)
from ali_pc_browser_client import AliPCBrowserClient
from jd_h5_client import JDH5Client, JD_AREAS, resolve_jd_region

# ============================================================ init

mcp = FastMCP(
    name="auction-mcp",
    instructions=(
        "司法拍卖实时查询 MCP — 同时聚合 阿里拍卖 + 京东拍卖 两端.\n"
        "\n"
        "🔑 **默认调用 `search_judicial`** (双端聚合, 一次查询返回两端合并 + 价格降序 + 单位归一).\n"
        "   除非用户明确说 '只查阿里' / '只查京东', 否则**永远用 search_judicial**, 不要单独调\n"
        "   ali_search_judicial / jd_search_judicial — 那俩是 advanced 单源工具, 默认调会让用户只看到\n"
        "   一半数据 (阿里和京东是两个独立标的池, 不重复, 各有自己的优势品类).\n"
        "\n"
        "调用约定:\n"
        "1. 地区参数都用**中文**, 不要自己拼国标编码 (除非用户明确给出).\n"
        "2. 用户说 '查广州拍卖' → province='广东', city='广州市'.\n"
        "   用户说 '杭州房产' → province='浙江', city='杭州市'.\n"
        "   用户说 '查苏州吴江区' → province='江苏', city='苏州市', district='吴江区'.\n"
        "   用户只说省 (如 '查广东') → 只传 province='广东', city 留空.\n"
        "3. 阿里支持 31 省 / 3146 区县, 京东支持 33 省 / 5344 区县, 区县直接传 district 中文名,\n"
        "   工具自动解析两端各自的内部编码. ⚠️ 不要自己从 *_get_supported_areas 拿 code 再传\n"
        "   location_codes — 那是 2020 版仅供人类参考, 阿里 server 用 pre-2013 vintage, 错位会乱掺.\n"
        "4. 默认双端/H5 查询固定='价格降序'+'仅进行中/即将开始'；用户明确要求其他排序或状态时，\n"
        "   使用第 6 条的 PC 浏览器链路，不要把 PC 参数塞进 H5 请求。\n"
        "5. search_judicial 返回每条 item 带 `platform: 'ali'|'jd'` + 归一化 `price_yuan` (元),\n"
        "   方便上层做对比. 单源原生字段也保留 (itemId / paimaiId 等).\n"
        "6. 用户明确要求阿里 PC 完整筛选 (关键词/价格/开始时间/任意状态或阶段) 时，先调用\n"
        "   ali_pc_browser_start；用户在弹出的 Chrome 手动登录后，再调用 ali_pc_search_judicial。\n"
        "   PC 浏览器链路不读取或保存 Cookie，遇到登录/验证码/滑块必须交给用户手动完成。\n"
    ),
)

# 阿里 H5 client (lazy init, 第一次调用时获取 _m_h5_tk cookie)
ali = AliH5Client()

# 京东 m. 版 client (无 sign, 无登录态)
jd = JDH5Client()

# 阿里 PC 浏览器 client (lazy start; 非持久化 context，不导出或保存 Cookie)
ali_pc = AliPCBrowserClient()

# ============================================================ 共享地区层级验证

def _validate_area_structural(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
) -> dict | None:
    """结构完整性验证 — 子级必须携带父级.

    返回 None = 通过; 返回 dict = 错误.
    """
    if city and not province:
        return {"error": "city_requires_province",
                "message": "传 city 必须同时传 province"}
    if district and not city:
        return {"error": "district_requires_city",
                "message": "传 district 必须同时传 city"}
    return None


def _validate_region_resolution(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
) -> dict | None:
    """地区解析验证 — 确认地区参数可被至少一个数据源解析.

    使用 GB 2260 2020 版 + JD 地区树 双源解析.
    无法解析时返回 region_resolution_failed + diagnostics.
    返回 None = 通过.
    """
    if not province:
        return None

    # province 必须可解析 (GB2260 2020 或 JD 地区树)
    p_code = resolve_area(province)
    jd_r = resolve_jd_region(province)
    if p_code is None and jd_r["province"] is None:
        return {"error": "region_resolution_failed",
                "diagnostics": {
                    "resolution": "province",
                    "province": province,
                    "city": city,
                    "district": district,
                }}

    if city:
        # city 必须可解析且归属 province
        c_code = resolve_area(province, city) if p_code else None
        jd_city_ok = False
        if jd_r["province"] is not None:
            jd_r2 = resolve_jd_region(province, city)
            jd_city_ok = jd_r2["city"] is not None
        if (c_code is None or c_code == p_code) and not jd_city_ok:
            return {"error": "region_resolution_failed",
                    "diagnostics": {
                        "resolution": "city",
                        "province": province,
                        "city": city,
                        "district": district,
                    }}

    if district:
        # district 必须可解析且归属 city
        d_code = resolve_area(province, city, district) if p_code else None
        c_code_d = resolve_area(province, city) if p_code else None
        jd_district_ok = False
        if jd_r["province"] is not None:
            jd_r3 = resolve_jd_region(province, city, district)
            jd_district_ok = jd_r3["district"] is not None
        if (d_code is None or d_code == c_code_d) and not jd_district_ok:
            return {"error": "region_resolution_failed",
                    "diagnostics": {
                        "resolution": "district",
                        "province": province,
                        "city": city,
                        "district": district,
                    }}

    return None


def _validate_ali_pc_resolution(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
) -> dict | None:
    """Ali 单源省/市解析预检 — 在任何 Ali provider 调用前拒绝无法解析的 province/city.

    仅验证 province 和 city; district 本身不验证 (允许动态学码 + title_filter 兜底).
    必须使用 Ali 实际采用的 pre-2013 地区表，不能用现代 GB2260/JD 的
    “任一可解析”结果代替，否则会放过 Ali 已静默降级到省级的城市。
    返回 None = 通过; 返回 dict = region_resolution_failed 错误.
    """
    if not province:
        return None

    p_code = resolve_area_ali(province)
    if p_code is None:
        return {"error": "region_resolution_failed",
                "diagnostics": {
                    "resolution": "province",
                    "province": province,
                    "city": city,
                    "district": district,
                }}

    if city:
        c_code = resolve_area_ali(province, city)
        # 北京/天津/上海/重庆的“市”层级与省级共用 XX0000 编码；仅当
        # 省市名称确实指向同一直辖市时允许同码，其他同码均是解析降级。
        municipality_by_prefix = {
            "11": "北京", "12": "天津", "31": "上海", "50": "重庆",
        }
        municipality = municipality_by_prefix.get(p_code[:2])
        municipality_aliases = (
            {municipality, f"{municipality}市"} if municipality else set()
        )
        same_municipality = (
            province in municipality_aliases and city in municipality_aliases
        )
        if c_code is None or (c_code == p_code and not same_municipality):
            return {"error": "region_resolution_failed",
                    "diagnostics": {
                        "resolution": "city",
                        "province": province,
                        "city": city,
                        "district": district,
                    }}

    return None


def _expand_ali_filter_option_value(value: Any) -> list[str]:
    """把 Ali filter option 的单值或 JSON 数组字符串统一展开为编码列表."""
    values: list[Any]
    if isinstance(value, list):
        values = value
    elif isinstance(value, str) and value.strip().startswith("["):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = value
        values = decoded if isinstance(decoded, list) else [decoded]
    else:
        values = [value]
    return [str(item) for item in values if item not in (None, "")]


def _resolve_ali_filter_names(
    var_name: str,
    names: list[str] | None,
) -> tuple[list[str] | None, dict | None]:
    """通过实时 filter options 将 Ali 筛选中文名精确解析为编码，失败时关闭查询."""
    if not names:
        return None, None

    nav = ali_get_filter_options()
    if nav.get("error"):
        return None, {
            "error": "ali_filter_options_failed",
            "diagnostics": {"dimension": var_name, "source": nav},
        }

    dimension = next(
        (item for item in nav.get("dimensions", []) if item.get("varName") == var_name),
        None,
    )
    if dimension is None:
        return None, {
            "error": "ali_filter_dimension_missing",
            "diagnostics": {"dimension": var_name},
        }

    name_to_values: dict[str, list[str]] = {}
    for option in dimension.get("options", []):
        name = str(option.get("name") or "").strip()
        if not name:
            continue
        name_to_values.setdefault(name, []).extend(
            _expand_ali_filter_option_value(option.get("value"))
        )

    requested = [str(name).strip() for name in names if str(name).strip()]
    unknown = [name for name in requested if name not in name_to_values]
    if unknown:
        return None, {
            "error": "ali_filter_resolution_failed",
            "diagnostics": {
                "dimension": var_name,
                "unknown_names": unknown,
                "available_names": list(name_to_values),
            },
        }

    resolved: list[str] = []
    for name in requested:
        for value in name_to_values[name]:
            if value not in resolved:
                resolved.append(value)
    return resolved, None


# ============================================================ tools: 阿里司法拍卖 (H5 mtop)

def _extract_items(raw: dict) -> tuple[list[dict], dict]:
    """从 ali.search_judicial 原始响应抽 items + meta. 返回 (items, {totalCount, page, pageSize})."""
    scenes = (raw.get("data") or {}).get("data", {}).get("scenes") or []
    if not scenes:
        return [], {"totalCount": 0, "page": None, "pageSize": 10}
    sl = (scenes[0].get("schemeList") or [{}])[0]
    cl = sl.get("contentList") or []
    items = []
    for it in cl:
        em = it.get("extraMap", {}) or {}
        items.append({
            "itemId":      it.get("itemId") or em.get("itemId"),
            "title":       em.get("title") or it.get("title"),
            "currentPrice": it.get("currentPrice"),
            "displayInitialPrice": em.get("displayInitialPrice"),
            "displayInitialPriceUnit": em.get("displayInitialPriceUnit"),
            "locationCode": em.get("locationCode") or it.get("locationCode"),
            "shopName":    em.get("shopName") or it.get("shopName"),
            "fcatV4Ids":   em.get("fcatV4Ids"),
            "fcatV4ButtomName": em.get("fcatV4ButtomName"),
            "startTime":   em.get("startTime") or it.get("startTime"),
            "endTime":     em.get("endTime") or it.get("endTime"),
            "status":      em.get("status") or it.get("status"),
            "statusOrder": it.get("statusOrder"),
            "circ":        em.get("circ") or it.get("circ"),
            "bizType":     em.get("bizType") or it.get("bizType"),
            "headerPicUrls": em.get("headerPicUrls"),
            "subscribeCnt": em.get("subscribeCnt") or it.get("subscribeCnt"),
        })
    meta = {"totalCount": sl.get("totalCount"), "page": sl.get("page"),
            "pageSize": sl.get("pageSize") or 10}
    return items, meta


def _normalize_item(platform: str, item: dict) -> dict:
    """Item 归一化: 价格统一为元(float), 加 platform 字段, 保留原始 item 于 raw.

    阿里 currentPrice 单位是**分** (1.25亿 = 125000000000), 京东是**元** (2627513600.0).
    归一化后上层和 LLM agent 不用心智负担去记单位.
    """
    cp = item.get("currentPrice")
    if platform == "ali":
        price_yuan = (cp / 100.0) if isinstance(cp, (int, float)) else None
        item_id = item.get("itemId")
    else:  # jd
        price_yuan = float(cp) if isinstance(cp, (int, float)) else None
        item_id = item.get("paimaiId")
    return {
        "platform":   platform,
        "id":         item_id,
        "title":      item.get("title"),
        "price_yuan": price_yuan,
        "raw":        item,
    }


@mcp.tool()
def search_judicial(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """**统一搜索 (推荐默认调用此工具)** — 同时查 阿里 + 京东 司法拍卖, 并行打两端,
    价格降序合并, 单位归一到元.

    阿里和京东是两个**独立的标的池, 不重复**: 阿里偏机构端高价资产 (亿级土地/在建工程),
    京东偏散户端住宅/股权/小额债权. 单调一端会让用户只看到一半数据.

    Args:
        province / city / district: 地区中文名 (e.g. "广东" / "广州市" / "天河区"). 同单源工具.
        page: 页码 (两端各取 page=N. ali 10 条/页, jd 40 条/页, 合并池 ~50 条/页).
        limit: 合并后返回前 N 条 (默认 20, 上限建议 50).

    Returns:
        {
          count: int,                      # 实际返回 items 数 (≤ limit)
          items: [{platform, id, title, price_yuan, raw}, ...],   # 价格降序
          ali_totalCount: int | None,      # 阿里端总数 (可分页)
          jd_count: int | None,            # 京东端本页 count
          sources: ["ali", "jd"],          # 成功调用的源 (若某端 error 此处不列)
          errors?: {ali?: {...}, jd?: {...}},    # 任一端失败的诊断信息
        }
    """
    # --- 结构验证 + 解析验证: 在 ThreadPoolExecutor 前拒绝非法地区参数 ---
    _verr = _validate_area_structural(province, city, district)
    if _verr:
        return _verr
    _rerr = _validate_region_resolution(province, city, district)
    if _rerr:
        return _rerr

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_ali = ex.submit(ali_search_judicial, province, city, district, page)
        f_jd  = ex.submit(jd_search_judicial,  province, city, district, page)
        try: ali_r = f_ali.result()
        except Exception as e: ali_r = {"error": "ali_unexpected_exception", "exception": str(e)}
        try: jd_r = f_jd.result()
        except Exception as e: jd_r = {"error": "jd_unexpected_exception", "exception": str(e)}

    items, errors, sources = [], {}, []
    if ali_r.get("error"):
        errors["ali"] = {k: v for k, v in ali_r.items() if k != "items"}
    else:
        sources.append("ali")
        for it in (ali_r.get("items") or []):
            items.append(_normalize_item("ali", it))
    if jd_r.get("error"):
        errors["jd"] = {k: v for k, v in jd_r.items() if k != "items"}
    else:
        sources.append("jd")
        for it in (jd_r.get("items") or []):
            items.append(_normalize_item("jd", it))

    # 价格降序 (None 价格沉到末尾)
    items.sort(key=lambda x: x["price_yuan"] if x["price_yuan"] is not None else -float("inf"),
               reverse=True)
    items = items[:limit]

    out: dict[str, Any] = {
        "count": len(items),
        "page":  page,
        "ali_totalCount": ali_r.get("totalCount"),
        "jd_count": jd_r.get("count"),
        "sources": sources,
        "items": items,
    }
    if errors:
        out["errors"] = errors
    return out


@mcp.tool()
def ali_search_judicial(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    page: int = 1,
    location_codes: list[str] | None = None,
    fcat_v4_ids: list[str] | None = None,
    fcat_v4_names: list[str] | None = None,
    circs: list[str] | None = None,
    tag_ids: list[str] | None = None,
    zc_biz_types: list[str] | None = None,
) -> dict:
    """**[Advanced 单源]** 阿里司法拍卖搜索. 默认情况下用 `search_judicial` 同时拿两端, 别单独调这个.

    仅当用户**明确**要"只查阿里" / 想用 location_codes / fcat_v4_ids /
    fcat_v4_names / circs / tag_ids / zc_biz_types 等高级参数时才用.

    **固定 价格降序 + 仅进行中/即将开始** (不可改).

    地区用**中文**传, 工具内部自动解析阿里 server 实际接受的编码 (pre-2013 vintage).

    典型用法:
      - "查广州拍卖"     → province="广东", city="广州市"
      - "杭州房产"      → province="浙江", city="杭州市"
      - "查广东"        → province="广东" (整省, city 留空)
      - **"绍兴柯桥区"** → province="浙江", city="绍兴市", district="柯桥区"
        (内置 pre-2013 数据 + 动态学码, 自动解析为阿里真正接受的编码 330621; 不要自己拼 location_codes)
      - 用户没说地区     → 不传 (全国)

    Args:
        province: 省份中文 (e.g. "广东"). 不传 = 全国
        city:     城市中文 (e.g. "广州市"). 必须配合 province
        district: 区县中文 (e.g. "柯桥区"). 必须配合 province + city
        page:     页码 (10 条/页)
        location_codes: (高级, escape hatch) 直接传编码列表; 仍会跑垃圾结果守门
        fcat_v4_ids:    (高级) 分类编码列表, 见 ali_get_filter_options
        fcat_v4_names:  (高级) 分类中文名列表, 从实时 filter options 精确解析; 不可与 IDs 同传
        circs:          (高级) 拍卖轮次编码列表, 见 ali_get_filter_options
        tag_ids:        (高级) 特性标签编码列表, 见 ali_get_filter_options
        zc_biz_types:   (高级) 资产类型编码列表, 见 ali_get_filter_options 的 zcBizTypes

    Returns:
        正常: {count, page, totalCount, items, validated, [matched_district_code], [_district_fallback]}
        阿里返垃圾(乱掺其他省市): {error: "ali_returned_unscoped_results", diagnostics, items: []}
    """
    # --- 结构验证: 在 provider 调用前拒绝结构非法参数 ---
    _verr = _validate_area_structural(province, city, district)
    if _verr:
        return _verr

    # --- 省/市解析预检: 未知 province/city 在任何 Ali 调用前返回 ---
    if not location_codes:
        _rerr = _validate_ali_pc_resolution(province, city, district)
        if _rerr:
            return _rerr

    # ---------- 分类中文名解析: fail-closed, 不允许与编码混传 ----------
    if fcat_v4_ids and fcat_v4_names:
        return {
            "error": "ali_filter_conflict",
            "diagnostics": {"dimension": "fcatV4Ids", "fields": ["fcat_v4_ids", "fcat_v4_names"]},
        }
    if fcat_v4_names:
        resolved_fcat_ids, filter_error = _resolve_ali_filter_names(
            "fcatV4Ids", fcat_v4_names
        )
        if filter_error:
            return filter_error
        fcat_v4_ids = resolved_fcat_ids

    # ---------- 解析 location_codes ----------
    fallback_used = None       # 客户端 title 过滤兜底标志
    matched_district = None    # 最终命中的区县码 (若 district 路径)
    expected_prefix = None     # 用于守门校验

    if location_codes:
        # 显式编码: 透传; 守门前缀由纯函数统一推导 (省级→2位, 市/区级→4位)
        first = next((c for c in location_codes if c), None)
        expected_prefix = derive_ali_scope_prefix(str(first)) if first else None
    elif district:
        if not city:
            return {"error": "district_requires_city",
                    "message": "传 district 必须同时传 city"}
        # 主: legacy 数据集解析
        ali_code = resolve_area_ali(province, city, district)
        city_code = resolve_area_ali(province, city) or ""
        expected_prefix = derive_ali_scope_prefix(city_code)

        is_district_hit = ali_code and ali_code != city_code and not ali_code.endswith("00")
        if is_district_hit:
            location_codes = [ali_code]
            matched_district = ali_code
        else:
            # 兜底: 从城市级结果学码 (e.g. 柯桥区在 legacy 叫绍兴县, 名字对不上)
            learned = ali.learn_district_code_from_city(city_code, district)
            if learned:
                location_codes = [learned]
                matched_district = learned
            else:
                # 兜底中的兜底: 城市级查 + 客户端按 district 名 title 过滤
                location_codes = [city_code]
                fallback_used = "title_filter"
    elif province or city:
        code = resolve_area_ali(province, city)
        if code:
            location_codes = [code]
            expected_prefix = derive_ali_scope_prefix(code)

    # ---------- 查询 ----------
    r = ali.search_judicial(
        page=page,
        sort="501",
        status_orders=["0", "1"],
        location_codes=location_codes,
        fcat_v4_ids=fcat_v4_ids,
        circs=circs,
        tag_ids=tag_ids,
        zc_biz_types=zc_biz_types,
    )
    ret_first = (r.get("ret") or [""])[0] if isinstance(r.get("ret"), list) else str(r.get("ret") or "")
    if ret_first != "SUCCESS::调用成功":
        # 非业务成功 (含 LOCAL_NON_JSON / token 错误等)
        return {"error": "mtop_call_failed", "ret": r.get("ret"),
                "diagnostics": {k: v for k, v in r.items() if k.startswith("_")}}

    items, meta = _extract_items(r)

    # ---------- 客户端 title 过滤兜底 ----------
    if fallback_used == "title_filter" and district:
        dn = district.rstrip("区县市旗")
        items = [it for it in items if dn and dn in (it.get("title") or "")]
        meta["totalCount"] = None  # 客户端过滤后不知道真 totalCount

    # ---------- 垃圾结果守门 ----------
    validated = True
    if expected_prefix and items:
        v = validate_location_scoped(items, expected_prefix)
        if not v["ok"]:
            return {
                "error": "ali_returned_unscoped_results",
                "diagnostics": {
                    "totalCount": meta.get("totalCount"),
                    "expected_prefix": expected_prefix,
                    "sample_off_prefix_codes": v["sample_off_prefix"],
                    "matched_in_scope": v["matched"],
                    "total_in_response": v["total"],
                    "location_codes_sent": location_codes,
                },
                "items": [],
            }

    out = {
        "totalCount": meta.get("totalCount"),
        "page":       meta.get("page") or page,
        "pageSize":   meta.get("pageSize") or 10,
        "count":      len(items),
        "items":      items,
        "validated":  validated,
    }
    if matched_district: out["matched_district_code"] = matched_district
    if fallback_used:    out["_district_fallback"] = fallback_used
    if r.get("_token_refreshed"): out["_token_refreshed"] = True
    return out


@mcp.tool()
def ali_get_filter_options() -> dict:
    """阿里司法拍卖完整 filter 维度可选项 (9 个维度).

    返回 sort / fcatV4Ids (分类) / provs / citys / locationCodes / circs (轮次) /
         statusOrders (状态) / tagIds (特性) / zcBizTypes (资产类型) 的所有可选 (value, name).
    走 H5 mtop pageSpmcs=filtersf-nav, 实时.

    Returns:
        {dimensions: [{varName, options: [{value, name}]}]}
    """
    r = ali.get_filter_nav()
    if r.get("ret", [""])[0] != "SUCCESS::调用成功":
        return {"ret": r.get("ret"), "error": "filtersf-nav call failed"}

    cl = (((r.get("data") or {}).get("data") or {}).get("scenes") or [{}])[0]\
         .get("schemeList", [{}])[0].get("contentList", [])
    dims = []
    for item in cl:
        opts = []
        for opt in (item.get("data") or []):
            if isinstance(opt, dict):
                opts.append({"value": opt.get("value"), "name": opt.get("name")})
        dims.append({
            "varName": item.get("varName"),
            "show":    item.get("show"),
            "optionDisplayMode": item.get("optionDisplayMode"),
            "count":   len(opts),
            "options": opts,
        })
    return {"dimensions": dims, "count": len(dims)}


# ============================================================ tools: 阿里 PC 登录态浏览器

@mcp.tool()
async def ali_pc_browser_start() -> dict:
    """启动阿里 PC 查询专用的可见 Chrome 会话.

    仅当用户明确要求关键词、价格区间、开始时间、任意排序/状态/阶段等
    PC 完整筛选能力时调用。浏览器使用非持久化 context；适配器不读取
    Cookie、不导出 storage_state、不保存用户配置目录。

    如果返回 login_required，请让用户在弹出的 Chrome 中手动登录；
    如果返回 action_required，请让用户手动处理登录、验证码或滑块。
    """
    return await ali_pc.start()


@mcp.tool()
async def ali_pc_browser_status() -> dict:
    """检查阿里 PC 浏览器会话是否已启动及是否完成用户手动登录."""
    return await ali_pc.status()


@mcp.tool()
async def ali_pc_search_judicial(
    keyword: str | None = None,
    category: str | None = None,
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    asset_type: str | None = None,
    sort: str | None = None,
    status: str | None = None,
    stage: str | None = None,
    min_price_yuan: int | None = None,
    max_price_yuan: int | None = None,
    auction_start_from: str | None = None,
    auction_start_to: str | None = None,
    limit: int = 20,
) -> dict:
    """**[Interactive Experimental]** 通过登录态 PC 页面执行阿里司法拍卖完整筛选.

    使用前必须先调用 `ali_pc_browser_start`，并由用户在弹出的 Chrome 中
    手动完成登录或验证。适配器不会读取、导出或持久化 Cookie。

    Args:
        keyword: 标的物名称/地理位置/执行案号关键词。真实页面会在关键词
                 搜索时清空其他筛选，因此不可与下面任一筛选组合。
        category: 分类中文名，如“住宅用房”“商业用房”。运行时从页面动态解析。
        province/city/district: 页面显示的省、市、区县中文名，按层级提供。
        asset_type: 资产类型中文名，如“诉讼资产”“破产资产”。
        sort: 页面排序中文名，如“当前价格由高到低”。
        status: 拍卖状态中文名，如“正在进行”“即将开始”“已结束”“中止”“撤回”。
        stage: 拍卖阶段中文名，如“一拍”“二拍”“重新拍卖”“变卖”。
        min_price_yuan/max_price_yuan: 价格下限/上限，单位为整数人民币元。
        auction_start_from/auction_start_to: 开始日期范围，YYYY-MM-DD，必须同时提供。
        limit: 最多返回当前页面识别出的拍品数，1-100。

    Returns:
        成功: {source, count, items, url, authenticated_session, cookie_policy}
        需人工操作: {state: action_required, reason, message, url}
        失败: {error, message, diagnostics}
    """
    return await ali_pc.search(
        keyword=keyword,
        category=category,
        province=province,
        city=city,
        district=district,
        asset_type=asset_type,
        sort=sort,
        status=status,
        stage=stage,
        min_price_yuan=min_price_yuan,
        max_price_yuan=max_price_yuan,
        auction_start_from=auction_start_from,
        auction_start_to=auction_start_to,
        limit=limit,
    )


@mcp.tool()
async def ali_pc_browser_close() -> dict:
    """关闭阿里 PC 浏览器会话并销毁进程内登录态."""
    return await ali_pc.close()


@mcp.tool()
def ali_get_supported_areas(province: str | None = None,
                            city: str | None = None) -> dict:
    """查 GB 2260 行政区划. 不传 = 31 省列表 + 资源链接; 传 province = 该省所有市;
    传 province+city = 该市所有区县.

    数据集: modood/Administrative-divisions-of-China (2020 版, 31 省 / 342 市 / 3056 区县).
    """
    if not province:
        return {
            "total": {"provinces": len(GB2260),
                       "cities": sum(len(p.get("children",[])) for p in GB2260),
                       "districts": sum(len(c.get("children",[]))
                                          for p in GB2260 for c in p.get("children",[]))},
            "provinces": [{"name": p["name"], "code": (p["code"]+"0000")[:6]} for p in GB2260],
            "note": "传 province 查市, 再传 city 查区县. ⚠️ 此处编码是 2020 版仅供参考; "
                    "真正查询请用 ali_search_judicial(province, city, district=中文名), "
                    "工具内置 pre-2013 编码自动解析阿里 server 真值, 不要把这里的 code 传给 search.",
        }
    pn = province.rstrip("省市自治区")
    p = next((x for x in GB2260 if pn in x["name"] or x["name"].startswith(pn)), None)
    if not p: return {"error": f"未找到省份 {province!r}"}
    if not city:
        return {
            "province": p["name"],
            "code": (p["code"]+"0000")[:6],
            "city_count": len(p.get("children", [])),
            "cities": [{"name": c["name"], "code": (c["code"]+"0000")[:6]}
                       for c in p.get("children", [])],
        }
    cn = city.rstrip("市地区州盟")
    c = next((x for x in p.get("children",[]) if cn in x["name"] or x["name"].startswith(cn)), None)
    if not c: return {"error": f"在 {p['name']} 找不到 {city!r}"}
    return {
        "province": p["name"], "city": c["name"],
        "code": (c["code"]+"0000")[:6],
        "district_count": len(c.get("children",[])),
        "districts": [{"name": d["name"], "code": (d["code"]+"0000")[:6]}
                      for d in c.get("children",[])],
    }


# ============================================================ tools: 京东司法拍卖 (H5 m. 版)

@mcp.tool()
def jd_search_judicial(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    page: int = 1,
) -> dict:
    """**[Advanced 单源]** 京东司法拍卖搜索. 默认情况下用 `search_judicial` 同时拿两端, 别单独调这个.

    仅当用户**明确**要"只查京东"时才用.

    **固定 价格降序 + 仅进行中/即将开始** (不可改). 走 api.m.jd.com/api functionId=getSearchData
    (公开 endpoint, **无需登录态**).

    支持范围: 全国 33 省 / 455 市 / 5344 区县 (内置 jd_areas.json, 由 getAreaInfoMap 拉取生成).

    典型用法:
      - "查广州拍卖"      → province="广东", city="广州市"
      - "杭州房产"       → province="浙江", city="杭州市"
      - **"苏州吴江区"**  → province="江苏", city="苏州市", district="吴江区"
      - "查广东"         → province="广东" (整省)
      - 用户没说地区      → 不传 (全国 250 万+)

    Args:
        province: 省份中文名 (e.g. "广东"). 不传 = 全国.
        city: 城市中文名 (e.g. "广州市"). 必须配 province.
        district: 区/县中文名 (e.g. "吴江区"). 必须配 city.
        page: 页码 (40 条/页).

    Returns:
        {count, page, items: [...]}
        每条 item: paimaiId / title / currentPriceCN / discountRate / displayStatus 等

    解析行为: 中文名模糊匹配 JD 内置地区树, 匹配不上的层级 silent skip
    (不会乱传错码触发 server 静默 fallback 到全国).
    """
    # --- 结构验证 + JD 地区树解析验证: 在 provider 调用前拒绝非法参数 ---
    _verr = _validate_area_structural(province, city, district)
    if _verr:
        return _verr

    if province:
        _jd_resolved = resolve_jd_region(province, city, district)
        if _jd_resolved["failed_level"]:
            return {"error": "region_resolution_failed",
                    "diagnostics": {
                        "resolution": _jd_resolved["failed_level"],
                        "province": province,
                        "city": city,
                        "district": district,
                    }}

    r = jd.search_judicial(page=page, province=province, city=city, district=district)
    if r.get("code") != 0:
        return {"code": r.get("code"), "msg": r.get("msg"), "error": "JD getSearchData failed"}

    data = r.get("data") or {}
    raw_items = data.get("resultData") or []
    items = []
    for it in raw_items:
        inner = it.get("data") or it
        items.append({
            "paimaiId":      inner.get("paimaiId"),
            "skuId":         inner.get("skuId"),
            "title":         inner.get("title"),
            "currentPrice":  inner.get("currentPrice"),
            "currentPriceCN": inner.get("currentPriceCN"),
            "startPrice":    inner.get("startPrice"),
            "creditCapitalCN": inner.get("creditCapitalCN"),
            "discountRate":  inner.get("discountRate"),
            "province":      inner.get("province"),
            "city":          inner.get("city"),
            "cityId":        inner.get("cityId"),
            "countyId":      inner.get("countyId"),
            "productCateId": inner.get("productCateId"),
            "publishSource": inner.get("publishSource"),
            "displayStatus": inner.get("displayStatus"),
            "endTime":       inner.get("endTime"),
            "remindCount":   inner.get("remindCount"),
            "productImage":  inner.get("productImage"),
            "houseAttributes": inner.get("houseAttributes"),
        })
    return {
        "count":   len(items),
        "page":    page,
        "items":   items,
    }


@mcp.tool()
def jd_get_supported_areas(province: str | None = None,
                           city: str | None = None) -> dict:
    """查 JD 端支持的地区树. 33 省 / 455 市 / 5344 区县 全覆盖.

    用法:
      - 不传: 列 33 省
      - 传 province: 列该省的市
      - 传 province + city: 列该市的区/县
    """
    if not province:
        return {
            "total": {
                "provinces": len(JD_AREAS),
                "cities": sum(len(p["cities"]) for p in JD_AREAS.values()),
                "districts": sum(len(c["counties"]) for p in JD_AREAS.values()
                                  for c in p["cities"].values()),
            },
            "provinces": list(JD_AREAS.keys()),
            "note": "支持模糊匹配 (e.g. '广东'='广东省'); 查市传 province, 查区县再传 city.",
        }
    # 模糊匹配 province
    resolved = resolve_jd_region(province, city)
    if resolved["province"] is None:
        return {"error": f"未找到省份 {province!r}"}
    prov_name, _prov_id = resolved["province"]
    prov = JD_AREAS[prov_name]
    if not city:
        return {
            "province": prov_name,
            "city_count": len(prov["cities"]),
            "cities": list(prov["cities"].keys()),
        }
    if resolved["city"] is None:
        return {"error": f"在 {prov_name} 找不到 {city!r}"}
    city_name, _city_id = resolved["city"]
    c = prov["cities"][city_name]
    return {
        "province": prov_name,
        "city": city_name,
        "district_count": len(c["counties"]),
        "districts": list(c["counties"].keys()),
    }


# ============================================================ entry

if __name__ == "__main__":
    mcp.run()
