"""MCP server for 京东 + 阿里 司法拍卖 (实时, 无需 iPad sign bridge).

v2 设计原则:
- **零依赖外部设备/桥**. 完全本地 Python httpx + MCP stdio.
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
    validate_location_scoped, scope_prefix, _ret0, GB2260,
    _pick, _SFX_PROV, _SFX_CITY,
)
from jd_h5_client import JDH5Client, JD_AREAS, MUNICIPALITIES as JD_MUNICIPALITIES

# 合并结果上限. 阿里 10 条/页 + 京东 40 条/页 = 合并池约 50 条.
MAX_LIMIT = 50


def _clamp_page(page: Any) -> int:
    """页码归一到 ≥1. 负数/0/非数字直接透传给上游会拿到难以解释的结果."""
    try:
        return max(1, int(page))
    except (TypeError, ValueError):
        return 1


# 直辖市省级码 — 省与市是同一级, 省级码就已经等于"市级已收窄", 不该判成 city 未应用.
_MUNICIPALITY_CODES = {"110000", "120000", "310000", "500000"}


def _requested_level(province: Any, city: Any, district: Any) -> int:
    """调用方要求收窄到第几级: 0=全国 1=省 2=市 3=区县."""
    if district: return 3
    if city:     return 2
    if province: return 1
    return 0


def _code_level(code: Any) -> int:
    """由实际发出的 GB 2260 码反推它真的收窄到第几级."""
    if not code:
        return 0
    c = str(code)
    if c.endswith("0000"):
        return 2 if c in _MUNICIPALITY_CODES else 1
    if c.endswith("00"):
        return 2
    return 3


def _clamp_limit(limit: Any) -> int:
    """limit 归一到 [1, MAX_LIMIT].

    未校验时 limit=-1 会让 items[:-1] **静默丢掉最后一条** — 不报错, 只是少一条,
    是最难被发现的一类缺陷。
    """
    try:
        return max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return 20

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
        "4. 用户问 '排序 / 状态' 怎么改: 告诉他不能改 (固定='价格降序'+'仅进行中/即将开始').\n"
        "5. search_judicial 返回每条 item 带 `platform: 'ali'|'jd'` + 归一化 `price_yuan` (元),\n"
        "   方便上层做对比. 单源原生字段也保留 (itemId / paimaiId 等).\n"
        "6. ⚠️ 读价格一律用 `price_yuan` (元). 两端的原生 `currentPrice` 同名不同单位 —\n"
        "   阿里是**分**, 京东是**元**, 差 100 倍, 直接读会把 1.25 亿报成 1250 亿.\n"
        "\n"
        "7. ★ 阿里侧每条 item 自带硬事实 (**匿名可得, 不必再调详情接口**,\n"
        "   2026-09-18 与官方详情接口交叉验证过): bail_yuan(保证金,元) /\n"
        "   increment_yuan(加价幅度,元) / building_area_sqm(建筑面积,㎡) /\n"
        "   land_area_sqm(土地面积,㎡) / land_purpose(**中文土地用途**) /\n"
        "   consult_unit_price_yuan_per_sqm(评估单价,元/㎡) /\n"
        "   consult_price_yuan(**评估总价,元**).\n"
        "   consult_price_yuan 由评估单价 × 建筑面积推出(平台不直接给), 已验证与详情接口一致;\n"
        "   缺任一项时该字段不出现 — 字段不存在不等于评估价为零.\n"
        "   判断是不是工业类资产**以 land_purpose 为准**, 比标题关键词可靠.\n"
        "   重整投资人资格 / 股权 / 债权一类标的通常没有面积与评估单价.\n"
        "7. ⚠️ 汇报前先看 `area_applied`: 某端为 false 表示**该端的结果没有按你要的地区收窄**\n"
        "   (例如省份对但城市名它不认, 结果退到了省级). 别把它当成该地区的数据汇报.\n"
        "   地区两端都没生效时直接返 `error: area_not_resolved`, 不会给你全国数据冒充.\n"
        "8. `limit` 是本页展示上限而非分页窗口 —— 被截掉的标的**不会**出现在下一页,\n"
        "   `dropped` 会告诉你截了多少. 要完整浏览就别改默认值 (50 = 满池).\n"
        "   「价格降序」只在**单页内**成立, 跨页不保证单调.\n"
    ),
)

# 阿里 H5 client (lazy init, 第一次调用时获取 _m_h5_tk cookie)
ali = AliH5Client()

# 京东 m. 版 client (无 sign, 无登录态)
jd = JDH5Client()

# ============================================================ tools: 阿里司法拍卖 (H5 mtop)

def _extra_facts(em: dict) -> dict:
    """extraMap 里的硬事实. 单位 2026-09-18 用两条已知标的与官方详情接口交叉验证:

    - bail / incrementnum 单位是**分**, 与 currentPrice 一致
      (运河路西 bail=7229000000 ↔ queryHttpsItemDetail 的 foregiftPrice 7229 万)
    - hArea(房屋建筑面积) / landArea(土地面积) 单位是**平方米 × 100**
      (南湖路 37 号 landArea=1563050 ↔ 法院公告 15630.50 ㎡)
    - consulteUnitPrice 单位是**元/㎡**; 乘以 hArea/100 即评估总价
      (运河路西 8133.83 × 69433.89 = 5.6476 亿 ↔ 详情接口 consultPrice 5.6476 亿)

    ★ 这些字段**匿名可得**, 不需要登录详情接口. landPurpose 是中文土地用途,
      判断资产类型以它为准, 比标题关键词可靠.
    """
    def cent(v):
        return round(v / 100.0, 2) if isinstance(v, (int, float)) else None

    area = cent(em.get("hArea"))
    unit = em.get("consulteUnitPrice") or None
    out = {
        "bail_yuan":          cent(em.get("bail")),
        "increment_yuan":     cent(em.get("incrementnum")),
        "building_area_sqm":  area,
        "land_area_sqm":      cent(em.get("landArea")),
        "land_purpose":       em.get("landPurpose"),
        "consult_unit_price_yuan_per_sqm": unit,
        # 评估总价: 平台不直接给, 由单价 × 建筑面积推出, 已交叉验证与详情接口一致
        "consult_price_yuan": round(unit * area, 2) if (unit and area) else None,
        "subsidy_price":      em.get("subsidyPrice") or None,
    }
    return {k: v for k, v in out.items() if v not in (None, "", 0)}


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
            **_extra_facts(em),
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
    limit: int = MAX_LIMIT,
) -> dict:
    """**统一搜索 (推荐默认调用此工具)** — 同时查 阿里 + 京东 司法拍卖, 并行打两端,
    价格降序合并, 单位归一到元.

    阿里和京东是两个**独立的标的池, 不重复**: 阿里偏机构端高价资产 (亿级土地/在建工程),
    京东偏散户端住宅/股权/小额债权. 单调一端会让用户只看到一半数据.

    Args:
        province / city / district: 地区中文名 (e.g. "广东" / "广州市" / "天河区"). 同单源工具.
        page: 页码 (两端各取 page=N. ali 10 条/页, jd 40 条/页, 合并池 = 50 条/页).
        limit: 本页返回前 N 条 (默认 50 = 满池, 不丢数据).
            ⚠️ limit 是**本页展示上限**, 不是分页窗口. 传 <50 时被截掉的标的
            **不会**出现在 page+1 (两端各自按自己的页大小翻页), envelope 的 `dropped`
            会告诉你截掉了多少. 要完整浏览就别动这个默认值.

    Returns:
        {
          count: int,                      # 实际返回 items 数 (≤ limit)
          dropped: int,                    # 本页取回但因 limit 被截掉的条数 (这些不会进下一页)
          items: [{platform, id, title, price_yuan, raw}, ...],   # 本页内价格降序
          ali_totalCount: int | None,      # 阿里端总数 (可分页)
          jd_count: int | None,            # 京东端本页 count
          sources: ["ali", "jd"],          # 成功调用的源 (若某端 error 此处不列)
          area_applied?: {ali: bool, jd: bool},  # 传了地区参数时才有: 该源是否完整应用了它
          errors?: {ali?: {...}, jd?: {...}},    # 任一端失败的诊断信息
        }
        地区两端都没生效: {error: "area_not_resolved", message, hint, items: []}

    ⚠️ 跨页的价格序不是全局单调的 (两端页大小不同, page+1 的首条可能贵过 page 的末条).
       「价格降序」的保证只在**单页内**成立.
    """
    page, limit = _clamp_page(page), _clamp_limit(limit)
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

    # ---------- 地区是否真的生效 ----------
    # 两端对"解析不出来"的处理语义相反: 阿里返 area_not_resolved 错误, 京东 silent skip
    # 后由上游返回**全国**数据. 不做下面这段判断的话, search_judicial(province="火星省")
    # 会返回 {"count": 40, "sources": ["jd"]} + 40 条全国标的 —— 顶层看起来完全成功,
    # agent 会直接把全国数据当成"火星省的拍卖"汇报, errors.ali 埋在信封尾部没人看。
    area_applied: dict[str, bool] = {}
    any_scoped = False
    if province or city or district:
        if "ali" in sources:
            area_applied["ali"] = bool(ali_r.get("area_applied"))
            any_scoped = any_scoped or bool(ali_r.get("area_scoped"))
        if "jd" in sources:
            area_applied["jd"] = bool(jd_r.get("area_applied"))
            any_scoped = any_scoped or bool(jd_r.get("area_scoped"))
        # 仅当**有成功的源**却没有任何一个把地区收窄过, 才判定地区彻底没生效.
        # (两端都网络失败时 sources 为空, 那是网络问题不是地区问题, 不在此报错)
        if sources and not any_scoped:
            return {
                "error": "area_not_resolved",
                "message": f"地区参数两端均无法解析: province={province!r} "
                           f"city={city!r} district={district!r}",
                "hint": "用 ali_get_supported_areas / jd_get_supported_areas 查可用的中文名; "
                        "不加地区参数即查全国",
                "sources": sources,
                "errors": errors or None,
                "items": [],
            }

    # 价格降序 (None 价格沉到末尾). 注意: 只在本页内成立, 跨页不保证 —— 见 docstring.
    items.sort(key=lambda x: x["price_yuan"] if x["price_yuan"] is not None else -float("inf"),
               reverse=True)
    fetched = len(items)
    items = items[:limit]

    out: dict[str, Any] = {
        "count": len(items),
        # 本页从上游取回但被 limit 截掉的条数. 这些标的**不会**出现在 page+1,
        # 因为两端各自按自己的页大小翻页, 合并层没有游标. 显式报出来, 不静默丢.
        "dropped": fetched - len(items),
        "page":  page,
        "ali_totalCount": ali_r.get("totalCount"),
        "jd_count": jd_r.get("count"),
        "sources": sources,
        "items": items,
    }
    if area_applied:
        out["area_applied"] = area_applied
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
) -> dict:
    """**[Advanced 单源]** 阿里司法拍卖搜索. 默认情况下用 `search_judicial` 同时拿两端, 别单独调这个.

    仅当用户**明确**要"只查阿里" / 想用 location_codes / fcat_v4_ids 等高级参数时才用.

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

    Returns:
        正常: {count, page, totalCount, items, validated, area_scoped, area_applied,
              [matched_district_code], [_district_fallback]}
              `area_applied` = 是否完整满足了请求的层级 (传了 city 却只收窄到省 → False);
              `area_scoped`  = 是否至少不是全国范围.
              每条 item 除原生字段外带 `price_yuan` (元) —— 原生 `currentPrice` 是**分**,
              跟京东同名字段差 100 倍, 读价格请一律用 price_yuan.
              `validated` = 本次是否真的跑过地区守门校验.
        地区解析不出来: {error: "area_not_resolved", message, hint}
        阿里返垃圾(乱掺其他省市): {error: "ali_returned_unscoped_results", diagnostics, items: []}
        网络/上游异常: {error: "ali_unexpected_exception", exception, exception_type, items: []}
    """
    try:
        return _ali_search_impl(province, city, district, page,
                                location_codes, fcat_v4_ids)
    except Exception as e:
        # 网络/超时/DNS 等异常不该以 traceback 的形式泄漏到 MCP 层,
        # 与 search_judicial 的兜底保持一致的错误语义
        return {"error": "ali_unexpected_exception", "exception": str(e),
                "exception_type": type(e).__name__, "items": []}


def _ali_search_impl(
    province: str | None, city: str | None, district: str | None,
    page: int, location_codes: list[str] | None,
    fcat_v4_ids: list[str] | None,
) -> dict:
    """ali_search_judicial 的实现体 (不含异常兜底)."""
    page = _clamp_page(page)
    # ---------- 解析 location_codes ----------
    fallback_used = None       # 客户端 title 过滤兜底标志
    matched_district = None    # 最终命中的区县码 (若 district 路径)
    expected_prefix = None     # 用于守门校验

    if location_codes:
        # 显式编码: 透传; 守门按该码自身的粒度校验 (escape hatch)
        first = next((c for c in location_codes if c), None)
        expected_prefix = scope_prefix(first)
    elif district:
        if not city:
            return {"error": "district_requires_city",
                    "message": "传 district 必须同时传 city"}
        # 主: legacy 数据集解析
        ali_code = resolve_area_ali(province, city, district)
        city_code = resolve_area_ali(province, city) or ""
        if not city_code:
            # 省份都解析不到, 再往下走只会发出 ['0000'] / [''] 这类无意义编码,
            # 白白打上游还绕过守门 (expected_prefix 为 None)
            return {"error": "area_not_resolved",
                    "message": f"无法解析地区 province={province!r} city={city!r}",
                    "hint": "用 ali_get_supported_areas 查可用的省/市中文名"}

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
        # 守门前缀按实际发出的编码推导, 而不是固定取 city_code[:4]:
        # 直辖市的区县码 (如 310115) 其"市级"前缀是 3101, 而 city_code 是 310000.
        expected_prefix = scope_prefix(location_codes[0])
    elif province or city:
        code = resolve_area_ali(province, city)
        if not code:
            return {"error": "area_not_resolved",
                    "message": f"无法解析地区 province={province!r} city={city!r}",
                    "hint": "用 ali_get_supported_areas 查可用的省/市中文名"}
        location_codes = [code]
        # 省级码 440000 → 前缀 '44' (取 [:4] 会得到 '4400', 而真实区县码是 4401xx/4403xx…,
        # 没有一个以 '4400' 开头, 会把**全部**省级查询误判成乱掺结果)
        expected_prefix = scope_prefix(code)

    # ---------- 查询 ----------
    r = ali.search_judicial(
        page=page,
        sort="501",
        status_orders=["0", "1"],
        location_codes=location_codes,
        fcat_v4_ids=fcat_v4_ids,
    )
    if _ret0(r) != "SUCCESS::调用成功":
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
    validated = False          # 是否真的跑过校验 (无前缀可校验时为 False)
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
        validated = True

    # 价格单位归一: 阿里 currentPrice 是**分**, 与京东的**元**同名不同单位.
    # 单源工具也补一个 price_yuan, 避免上层/LLM 拿着分当元汇报 (差 100 倍).
    for it in items:
        cp = it.get("currentPrice")
        it["price_yuan"] = (cp / 100.0) if isinstance(cp, (int, float)) else None

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

    # 地区实际收窄到哪一级. 阿里在**省**都解析不到时才报 area_not_resolved,
    # 而 province="广东" + city="杭州市" 这类只会静默降级到省级 —— 调用方有权知道。
    req = _requested_level(province, city, district)
    if req:
        applied_level = _code_level(location_codes[0] if location_codes else None)
        if fallback_used == "title_filter":
            applied_level = 3          # 客户端 title 过滤等效收窄到区县
        out["area_scoped"]  = applied_level > 0      # 是否至少不是全国
        out["area_applied"] = applied_level >= req   # 是否完整满足了请求的层级
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
    if _ret0(r) != "SUCCESS::调用成功":
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
    # 复用 ali_h5_client._pick 的三级精确优先匹配, **不要**在这里另写一份.
    # 历史缺陷: 这里曾自己写 `cn in x["name"]` 的任意位置子串匹配, 于是
    # "荆州市" 被 rstrip 剥成 "荆" 后命中 "荆门市", 查荆州列出的是荆门的区县;
    # "定州市" 剥成 "定" 命中 "保定市". 同一份匹配逻辑两处实现是这条缺陷能活下来的直接原因.
    p = _pick(GB2260, province, _SFX_PROV)
    if not p: return {"error": f"未找到省份 {province!r}"}
    if not city:
        return {
            "province": p["name"],
            "code": (p["code"]+"0000")[:6],
            "city_count": len(p.get("children", [])),
            "cities": [{"name": c["name"], "code": (c["code"]+"0000")[:6]}
                       for c in p.get("children", [])],
        }
    c = _pick(p.get("children", []), city, _SFX_CITY)
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
        {count, page, items: [...], area_scoped, area_applied}
        地区层级未被应用时 area_applied=False —— silent skip 不再是静默的.
        每条 item: paimaiId / title / price_yuan / currentPriceCN / discountRate / displayStatus 等
        读价格一律用 `price_yuan` (元) —— 与阿里单源工具字段名/单位一致.

    解析行为: 中文名模糊匹配 JD 内置地区树, 匹配不上的层级 silent skip
    (不会乱传错码触发 server 静默 fallback 到全国).
    """
    page = _clamp_page(page)
    try:
        r = jd.search_judicial(page=page, province=province, city=city, district=district)
    except Exception as e:
        return {"error": "jd_unexpected_exception", "exception": str(e),
                "exception_type": type(e).__name__, "items": []}
    if r.get("code") != 0:
        return {"code": r.get("code"), "msg": r.get("msg"), "error": "JD getSearchData failed"}

    data = r.get("data") or {}
    raw_items = data.get("resultData") or []
    items = []
    for it in raw_items:
        inner = it.get("data") or it
        # 价格单位归一. 京东原生 currentPrice 本来就是**元**, 不需要换算, 但仍必须输出
        # price_yuan —— 否则 "所有工具都有 price_yuan, 读价格一律用它" 这条对 agent 的
        # 约定在单源工具上失效, agent 会退回去直接读 currentPrice, 而那个字段与阿里同名
        # 不同单位 (阿里是分), 正是要根除的心智负担.
        cp = inner.get("currentPrice")
        items.append({
            "paimaiId":      inner.get("paimaiId"),
            "skuId":         inner.get("skuId"),
            "title":         inner.get("title"),
            "currentPrice":  cp,
            "price_yuan":    float(cp) if isinstance(cp, (int, float)) else None,
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
    out: dict[str, Any] = {
        "count":   len(items),
        "page":    page,
        "items":   items,
    }
    # 地区实际收窄到哪一级. 京东的 silent skip 本身是对的 (乱传错码会让 server 静默
    # fallback 到全国), 但它必须**把 skip 这件事回报给调用方** —— 否则
    # jd_search_judicial(province="火星省") 返回的 40 条全国标的看起来和成功查询一模一样。
    req = _requested_level(province, city, district)
    if req:
        p = jd._resolve_area(province, city, district)
        applied_level = (3 if p.get("multiCountyIds") else
                         2 if p.get("multiCityIds") else
                         1 if p.get("multiProvinceIds") else 0)
        if p.get("multiProvinceNames") in JD_MUNICIPALITIES:
            # 直辖市只有 省 → 区 两层: cities 那一层其实就是区县;
            # 而 city="上海市" 与 province="上海" 是同一级, 省级命中即已满足 city.
            if applied_level == 2:
                applied_level = 3
            elif applied_level == 1 and req == 2:
                applied_level = 2
        out["area_scoped"]  = applied_level > 0
        out["area_applied"] = applied_level >= req
    return out


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
    # 模糊匹配 province. 后缀集合必须按层级传, 否则区县级会把 "定州市" 剥成 "定".
    from jd_h5_client import _match_name, _SFX_PROV as _JD_SFX_PROV, _SFX_CITY as _JD_SFX_CITY
    pm = _match_name(province, JD_AREAS, _JD_SFX_PROV)
    if not pm: return {"error": f"未找到省份 {province!r}"}
    prov_name, prov = pm
    if not city:
        return {
            "province": prov_name,
            "city_count": len(prov["cities"]),
            "cities": list(prov["cities"].keys()),
        }
    cm = _match_name(city, prov["cities"], _JD_SFX_CITY)
    if not cm: return {"error": f"在 {prov_name} 找不到 {city!r}"}
    city_name, c = cm
    return {
        "province": prov_name,
        "city": city_name,
        "district_count": len(c["counties"]),
        "districts": list(c["counties"].keys()),
    }


# ============================================================ entry

if __name__ == "__main__":
    mcp.run()
