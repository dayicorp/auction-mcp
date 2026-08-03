"""Fail-closed auction-to-market analysis orchestration primitives.

The module deliberately separates deterministic analysis from browser providers.
It never logs in, reads browser storage, registers for an auction, or bids.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html as html_module
import re
from statistics import median
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


ITEM_ID_PATTERN = re.compile(r"^[0-9]{8,20}$")
ITEM_URL_PATTERN = re.compile(
    r"^https://sf-item\.taobao\.com/sf_item/([0-9]{8,20})\.htm$"
)
NOTICE_PATH_PATTERN = re.compile(r"^/notice_detail/([0-9]{5,20})\.htm$")
VISIBLE_TEXT_PATTERN = re.compile(r"[^\x00-\x1f\x7f]+")
NUMBER_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)")


class AssetAnalysisError(RuntimeError):
    """A public, bounded failure that stops analysis without guessing."""

    def __init__(
        self,
        code: str,
        message: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}

    def as_result(self, *, stage: str) -> dict[str, Any]:
        return {
            "status": "STOPPED",
            "stage": stage,
            "decision": "NEEDS_REVIEW",
            "maximum_bid_yuan": None,
            "error": {
                "code": self.code,
                "message": self.message,
                "diagnostics": self.diagnostics,
            },
        }


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise AssetAnalysisError(
            "ASSET_INVALID_INPUT", f"{field} 必须是有限数字"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AssetAnalysisError(
            "ASSET_INVALID_INPUT", f"{field} 必须是有限数字"
        ) from exc
    if not parsed.is_finite():
        raise AssetAnalysisError(
            "ASSET_INVALID_INPUT", f"{field} 必须是有限数字"
        )
    return parsed


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_item_reference(item_ref: str | None) -> str | None:
    """Accept only a numeric item ID or the canonical fixed-host detail URL."""
    if item_ref is None:
        return None
    value = str(item_ref).strip()
    if ITEM_ID_PATTERN.fullmatch(value):
        return value
    match = ITEM_URL_PATTERN.fullmatch(value)
    if match:
        return match.group(1)
    raise AssetAnalysisError(
        "ASSET_INVALID_ITEM_REFERENCE",
        "item_ref 仅允许8至20位数字ID或固定阿里详情URL",
    )


def validate_text(value: str | None, *, field: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise AssetAnalysisError("ASSET_INVALID_INPUT", f"缺少{field}")
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if not normalized or len(normalized) > 160 or not VISIBLE_TEXT_PATTERN.fullmatch(normalized):
        raise AssetAnalysisError(
            "ASSET_INVALID_INPUT", f"{field}必须是1至160个可见字符"
        )
    return normalized


def _compact(value: Any) -> str:
    return re.sub(r"[\s()（）【】\[\],，。:：·\-]", "", str(value or "")).lower()


def _extract_first_number(value: Any) -> Decimal | None:
    match = NUMBER_PATTERN.search(str(value or "").replace(",", ""))
    return Decimal(match.group(1)) if match else None


def validate_notice_url(url: str, item_id: str) -> str:
    """Reject redirects and arbitrary URLs before the public announcement fetch."""
    parsed = urlparse(str(url or ""))
    query_item_ids = parse_qs(parsed.query).get("item_id", [])
    if (
        parsed.scheme != "https"
        or parsed.hostname != "sf.taobao.com"
        or not NOTICE_PATH_PATTERN.fullmatch(parsed.path)
        or query_item_ids != [item_id]
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise AssetAnalysisError(
            "ASSET_NOTICE_URL_REJECTED",
            "法院公告URL不符合固定阿里公告契约",
        )
    return parsed.geturl()


def _html_to_text(content: bytes, content_type: str = "") -> str:
    charset_match = re.search(r"charset=([A-Za-z0-9_-]+)", content_type, re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings.extend(["gb18030", "utf-8"])
    decoded = None
    for encoding in encodings:
        try:
            decoded = content.decode(encoding)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if decoded is None:
        raise AssetAnalysisError(
            "ASSET_NOTICE_ENCODING_FAILED", "法院公告字符集无法安全解析"
        )
    decoded = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        decoded,
        flags=re.I | re.S,
    )
    decoded = re.sub(
        r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>",
        "\n",
        decoded,
        flags=re.I,
    )
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    decoded = html_module.unescape(decoded)
    return "\n".join(
        line
        for raw_line in decoded.splitlines()
        if (line := re.sub(r"\s+", " ", raw_line).strip())
    )


def parse_notice_text(text: str, source_url: str) -> dict[str, Any]:
    """Extract bounded risk facts without returning phones or bank accounts."""
    compact = _compact(text)
    if len(compact) < 300 or "拍卖" not in compact:
        raise AssetAnalysisError(
            "ASSET_NOTICE_CONTRACT_DRIFT", "法院公告正文缺失或结构发生漂移"
        )

    def contains(*phrases: str) -> bool:
        return any(_compact(phrase) in compact for phrase in phrases)

    def date_after(label: str) -> str | None:
        normalized_label = _compact(label)
        label_pos = compact.find(normalized_label)
        if label_pos < 0:
            return None
        nearby = compact[label_pos : label_pos + 180]
        match = re.search(
            r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:(\d{1,2})时)?",
            nearby,
        )
        if not match:
            return None
        year = int(match.group(1))
        hour = int(match.group(4) or 0)
        return f"{year:04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d} {hour:02d}:00"

    disclosed_occupancy = contains("占用", "居住情况", "实际居住")
    disclosed_lease = contains("租赁", "租约", "承租")
    return {
        "source_url": source_url,
        "as_is_delivery": contains("现状交付", "以实物现状为准"),
        "no_defect_warranty": contains("不承担拍卖标的瑕疵保证"),
        "court_clear_handover": contains("依法对涉案标的进行清场移交"),
        "possible_arrears_buyer_risk": contains("物业管理费", "水、电、气等欠费"),
        "transfer_not_guaranteed": contains("对标的物的过户不作承诺"),
        "household_registration_not_handled": contains("不负责户口的迁入、迁出"),
        "buyer_advances_seller_tax": contains("由买受人先行垫付"),
        "regret_deposit_forfeited": contains("悔拍", "保证金不予以退还"),
        "occupancy_disclosed": disclosed_occupancy,
        "lease_disclosed": disclosed_lease,
        "viewing_deadline": date_after("预约登记截至"),
        "balance_deadline": date_after("拍卖余款请在"),
    }


async def fetch_public_notice(
    announcement_url: str,
    item_id: str,
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    url = validate_notice_url(announcement_url, item_id)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "auction-mcp/0.1 public-notice-reader"},
        ) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise AssetAnalysisError(
            "ASSET_NOTICE_TIMEOUT", "法院公告读取超时"
        ) from exc
    except httpx.HTTPError as exc:
        raise AssetAnalysisError(
            "ASSET_NOTICE_FETCH_FAILED",
            "法院公告读取失败",
            {"exception_type": type(exc).__name__},
        ) from exc
    if response.status_code != 200:
        raise AssetAnalysisError(
            "ASSET_NOTICE_FETCH_FAILED",
            "法院公告返回非成功状态",
            {"status_code": response.status_code},
        )
    text = _html_to_text(response.content, response.headers.get("content-type", ""))
    return parse_notice_text(text, url)


def select_exact_community(
    candidates: list[dict[str, Any]], community_keyword: str
) -> dict[str, Any]:
    expected = _compact(community_keyword)
    exact = [item for item in candidates if _compact(item.get("name")) == expected]
    if len(exact) != 1:
        raise AssetAnalysisError(
            "ASSET_COMMUNITY_MATCH_AMBIGUOUS",
            "贝壳小区没有唯一精确匹配，禁止猜测",
            {"candidate_count": len(candidates), "exact_match_count": len(exact)},
        )
    return exact[0]


def select_exact_auction_item(
    search_result: dict[str, Any], expected_address: str
) -> str:
    items = search_result.get("items") or search_result.get("results") or []
    expected = _compact(expected_address)
    matches: list[str] = []
    for item in items:
        title = _compact(item.get("title"))
        item_id = str(item.get("itemId") or item.get("id") or "")
        if expected and expected in title and ITEM_ID_PATTERN.fullmatch(item_id):
            matches.append(item_id)
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise AssetAnalysisError(
            "ASSET_ITEM_MATCH_AMBIGUOUS",
            "地址未解析到唯一阿里标的，禁止猜测",
            {"exact_match_count": len(unique)},
        )
    return unique[0]


def _auction_area(detail: dict[str, Any]) -> Decimal:
    compact = _compact(detail.get("detailText"))
    match = re.search(r"建筑(?:总)?面积([0-9]+(?:\.[0-9]+)?)平方米", compact)
    if not match:
        raise AssetAnalysisError(
            "ASSET_REQUIRED_FIELD_MISSING", "阿里详情缺少可核验建筑面积"
        )
    return Decimal(match.group(1))


def _auction_year(detail: dict[str, Any]) -> int | None:
    match = re.search(r"(19|20)\d{2}年建成", _compact(detail.get("detailText")))
    return int(match.group(0)[:4]) if match else None


def _address_matches(auction_location: Any, beike_address: Any) -> bool:
    auction = _compact(auction_location)
    beike = _compact(beike_address)
    beike = re.sub(r"^.*?区", "", beike, count=1)
    return len(beike) >= 5 and beike in auction


def _percent_discount(reference: Decimal | None, price: Decimal) -> float | None:
    if reference is None or reference <= 0:
        return None
    return _money((reference - price) / reference * Decimal("100"))


def build_asset_analysis(
    *,
    item_id: str,
    detail: dict[str, Any],
    notice: dict[str, Any],
    community: dict[str, Any],
    market: dict[str, Any],
    expected_address: str | None = None,
    screenshot_title: str | None = None,
    screenshot_area_sqm: float | None = None,
    screenshot_starting_price_yuan: int | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Build a traceable report and stop short of an unsupported bid ceiling."""
    area = _auction_area(detail)
    starting = _decimal(detail.get("startingPriceYuan"), field="startingPriceYuan")
    appraisal_raw = detail.get("appraisalPriceYuan")
    appraisal = (
        _decimal(appraisal_raw, field="appraisalPriceYuan")
        if appraisal_raw is not None
        else None
    )
    location = str(detail.get("location") or detail.get("title") or "")
    title = str(detail.get("title") or "")
    if not title or not location or starting <= 0:
        raise AssetAnalysisError(
            "ASSET_REQUIRED_FIELD_MISSING", "阿里详情缺少标题、位置或起拍价"
        )

    evidence_mismatches: list[str] = []
    if expected_address and _compact(expected_address) not in _compact(location + title):
        evidence_mismatches.append("expected_address")
    if screenshot_title and _compact(screenshot_title) not in _compact(title + location):
        evidence_mismatches.append("screenshot_title")
    if screenshot_area_sqm is not None:
        screenshot_area = _decimal(screenshot_area_sqm, field="screenshot_area_sqm")
        if abs(screenshot_area - area) > Decimal("0.05"):
            evidence_mismatches.append("screenshot_area_sqm")
    if screenshot_starting_price_yuan is not None:
        screenshot_price = _decimal(
            screenshot_starting_price_yuan,
            field="screenshot_starting_price_yuan",
        )
        if screenshot_price != starting:
            evidence_mismatches.append("screenshot_starting_price_yuan")
    if evidence_mismatches:
        raise AssetAnalysisError(
            "ASSET_INPUT_EVIDENCE_MISMATCH",
            "截图或调用方证据与阿里详情不一致",
            {"mismatched_fields": evidence_mismatches},
        )

    detail_block = market.get("detail") or {}
    if _compact(detail_block.get("name")) != _compact(community.get("name")):
        raise AssetAnalysisError(
            "ASSET_COMMUNITY_DETAIL_MISMATCH", "贝壳候选与详情页小区名称不一致"
        )
    if not _address_matches(location, detail_block.get("address")):
        raise AssetAnalysisError(
            "ASSET_ADDRESS_MISMATCH", "阿里标的地址与贝壳小区地址不一致"
        )

    year = _auction_year(detail)
    all_listings = market.get("listings") or []
    if not all_listings:
        raise AssetAnalysisError(
            "ASSET_MARKET_EVIDENCE_MISSING", "贝壳没有有效第一主挂牌样本"
        )
    suspected: list[dict[str, Any]] = []
    independent: list[dict[str, Any]] = []
    for listing in all_listings:
        listing_area = _decimal(listing.get("area_sqm"), field="listing.area_sqm")
        same_area = abs(listing_area - area) <= Decimal("0.05")
        same_year = year is not None and f"{year}年" in str(listing.get("house_info") or "")
        enriched = {**listing, "suspected_same_asset": bool(same_area and same_year)}
        if enriched["suspected_same_asset"]:
            suspected.append(enriched)
        else:
            independent.append(enriched)
    if not independent:
        raise AssetAnalysisError(
            "ASSET_INDEPENDENT_COMPS_MISSING",
            "去除疑似同套挂牌后没有独立市场样本",
        )

    independent_prices = [
        _decimal(item.get("unit_price_yuan"), field="listing.unit_price_yuan")
        for item in independent
    ]
    independent_median = Decimal(str(median(independent_prices)))
    page_average = _decimal(
        detail_block.get("listing_average_unit_price_yuan"),
        field="listing_average_unit_price_yuan",
    )
    market_low = min(page_average, independent_median)
    market_high = max(page_average, independent_median)
    page_value = page_average * area
    independent_value = independent_median * area

    detail_compact = _compact(detail.get("detailText"))
    no_key = "钥匙无" in detail_compact
    court_seizure = "查封" in detail_compact
    critical_unknowns = []
    if not notice.get("occupancy_disclosed"):
        critical_unknowns.append("current_occupancy")
    if not notice.get("lease_disclosed"):
        critical_unknowns.append("lease_status")
    if notice.get("possible_arrears_buyer_risk"):
        critical_unknowns.append("arrears_amount")
    if no_key:
        critical_unknowns.append("interior_condition")

    decision_reasons = [
        "挂牌数据是要约价格，不是成交价格",
        "疑似同套挂牌已从独立样本中排除",
    ]
    if critical_unknowns:
        decision_reasons.append("关键法律或交付事实仍未核实")
    if notice.get("transfer_not_guaranteed"):
        decision_reasons.append("公告不承诺一定完成过户")
    decision = "PAUSE_DUE_DILIGENCE" if critical_unknowns else "MANUAL_REVIEW_REQUIRED"
    decision_zh = "暂缓，完成线下尽调后再决定" if critical_unknowns else "需要人工复核"

    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat()
    report = {
        "status": "OK",
        "analysis_version": 1,
        "retrieved_at": retrieved,
        "item": {
            "item_id": item_id,
            "title": title,
            "location": location,
            "stage": detail.get("stage"),
            "auction_start_at": detail.get("auctionStartAt"),
            "starting_price_yuan": _money(starting),
            "appraisal_price_yuan": _money(appraisal) if appraisal is not None else None,
            "deposit_yuan": detail.get("depositYuan"),
            "increment_yuan": detail.get("incrementYuan"),
            "area_sqm": float(area),
            "build_year": year,
            "no_key": no_key,
            "court_seizure": court_seizure,
            "source_url": detail.get("url"),
        },
        "community_match": {
            "decision": "MATCHED",
            "confidence": "HIGH",
            "xiaoqu_id": community.get("xiaoqu_id"),
            "name": community.get("name"),
            "region": community.get("region"),
            "address": detail_block.get("address"),
            "source_url": detail_block.get("source_url"),
        },
        "market": {
            "listing_count": len(all_listings),
            "independent_listing_count": len(independent),
            "suspected_same_asset_count": len(suspected),
            "suspected_same_asset_listings": suspected,
            "page_average_unit_price_yuan": _money(page_average),
            "independent_median_unit_price_yuan": _money(independent_median),
            "asking_evidence_unit_price_range_yuan": [
                _money(market_low),
                _money(market_high),
            ],
            "page_average_implied_value_yuan": _money(page_value),
            "independent_median_implied_value_yuan": _money(independent_value),
            "starting_discount_vs_page_average_percent": _percent_discount(
                page_value, starting
            ),
            "starting_discount_vs_independent_median_percent": _percent_discount(
                independent_value, starting
            ),
            "starting_discount_vs_appraisal_percent": _percent_discount(
                appraisal, starting
            ),
            "price_basis": "current_asking_prices_not_transactions",
            "listing_source_url": market.get("listing_source_url"),
        },
        "notice": notice,
        "risk": {
            "critical_unknowns": critical_unknowns,
            "as_is_delivery": bool(notice.get("as_is_delivery")),
            "court_clear_handover_stated": bool(notice.get("court_clear_handover")),
            "possible_arrears_buyer_risk": bool(
                notice.get("possible_arrears_buyer_risk")
            ),
            "transfer_not_guaranteed": bool(notice.get("transfer_not_guaranteed")),
            "buyer_advances_seller_tax": bool(
                notice.get("buyer_advances_seller_tax")
            ),
        },
        "decision": decision,
        "decision_zh": decision_zh,
        "decision_reasons": decision_reasons,
        "maximum_bid_yuan": None,
        "maximum_bid_reason": "税费、欠费、维修、融资、持有和交付成本未全部核实",
        "security": {
            "login_automated": False,
            "credential_storage_accessed": False,
            "registration_or_bid_performed": False,
        },
    }
    report["markdown_report"] = render_markdown_report(report)
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    item = report["item"]
    community = report["community_match"]
    market = report["market"]
    risks = "、".join(report["risk"]["critical_unknowns"]) or "无"
    return "\n".join(
        [
            f"# {item['title']}",
            "",
            f"- 决策：{report['decision_zh']}",
            f"- 起拍价：{item['starting_price_yuan']:.2f}元",
            f"- 建筑面积：{item['area_sqm']:.2f}㎡",
            f"- 匹配小区：{community['name']}（{community['confidence']}）",
            f"- 独立挂牌中位单价：{market['independent_median_unit_price_yuan']:.2f}元/㎡",
            f"- 疑似同套挂牌：{market['suspected_same_asset_count']}套，已排除",
            f"- 关键未知项：{risks}",
            "- 最高出价：UNKNOWN（成本与交付事实不足，禁止猜测）",
        ]
    )
