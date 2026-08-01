"""阿里司法拍卖 PC 浏览器适配器.

该链路用于 H5 mtop 不支持、但 PC 页面已验证可用的关键词、价格与
开始时间筛选。浏览器上下文是非持久化的；本模块不读取 Cookie、不导出
storage_state，也不指定用户数据目录。登录与验证码始终交给用户手动完成。
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


PC_HOME_URL = "https://sf.taobao.com/"
PC_KEYWORD_URL = "https://sf.taobao.com/item_list.htm"
PC_DETAIL_URL_TEMPLATE = "https://sf-item.taobao.com/sf_item/{item_id}.htm"
_PC_ALLOWED_HOST = "sf.taobao.com"
_PC_DETAIL_ALLOWED_HOST = "sf-item.taobao.com"
_PUNISH_MARKERS = ("/_____tmd_____/punish", "x5secdata=")
_VERIFICATION_MARKERS = ("安全验证", "滑动验证", "请完成验证", "请拖动滑块")
_DETAIL_LOADING_MARKERS = ("标的物详情加载中", "附件加载中")
_SELECT_DIMENSION_MARKERS = {
    "sort": "默认排序",
    "status": "拍卖状态",
    "stage": "拍卖阶段",
    "price_range": "价格区间",
}
_SENSITIVE_QUERY_KEYS = {"x5secdata"}
_PC_MAX_PAGE = 5
_PC_CLICKABLE_SELECTOR = 'a,button,[role="button"]'
_PC_PAGER_SNAPSHOT_SCRIPT = r"""() => {
    const clickables = Array.from(document.querySelectorAll('a,button,[role="button"]'));
    const controls = clickables.map((element, index) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const text = (element.innerText || element.textContent || '')
            .trim().replace(/\s+/g, ' ');
        const aria = (element.getAttribute('aria-label') || '').trim();
        const title = (element.getAttribute('title') || '').trim();
        const className = String(element.className || '');
        const id = element.id || '';
        const haystack = [text, aria, title, className, id].join(' ');
        return {
            index,
            tag: element.tagName.toLowerCase(),
            text,
            aria,
            title,
            className,
            id,
            href: element.href || null,
            visible: style.display !== 'none' && style.visibility !== 'hidden'
                && rect.width > 0 && rect.height > 0,
            disabled: Boolean(element.disabled)
                || element.getAttribute('aria-disabled') === 'true'
                || /disabled/i.test(className),
            relevant: /下一页|下页|next|pagination|pager|page-next|next-next/i.test(haystack),
        };
    }).filter(item => item.relevant).slice(0, 30);
    const current = Array.from(document.querySelectorAll(
        '[aria-current="page"],.active,.current,.next-current,.ui-page-current'
    )).map(element => ({
        text: (element.innerText || element.textContent || '').trim(),
        className: String(element.className || ''),
    })).filter(item => /^\d+$/.test(item.text)).slice(0, 10);
    return {controls, current};
}"""
_PC_DETAIL_SNAPSHOT_SCRIPT = r"""() => {
    const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
    const visible = element => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
    };
    const bodyText = clean(document.body ? document.body.innerText : '').slice(0, 20000);
    const candidates = Array.from(document.querySelectorAll('span,li,p,div,td,dd'))
        .filter(visible).map(element => clean(element.innerText || element.textContent))
        .filter(text => text && text.length <= 300);
    const labels = [
        '当前价', '变卖价', '起拍价', '评估价', '保证金', '加价幅度',
        '延时周期', '竞价周期', '联系方式', '标的公告'
    ];
    const labelBlocks = {};
    for (const label of labels) {
        const matches = candidates.filter(text => text.includes(label))
            .sort((left, right) => left.length - right.length);
        labelBlocks[label] = matches.length ? matches[0] : null;
    }
    const titleNode = Array.from(document.querySelectorAll('h1'))
        .find(element => visible(element) && clean(element.innerText));
    const detailNode = document.querySelector('#J_ItemDetailContent');
    const attachmentNode = document.querySelector('#J_ItemAttachContent');
    const detailText = detailNode && visible(detailNode)
        ? clean(detailNode.innerText || detailNode.textContent).slice(0, 10000)
        : '';
    const attachmentText = attachmentNode && visible(attachmentNode)
        ? clean(attachmentNode.innerText || attachmentNode.textContent).slice(0, 3000)
        : '';
    const attachments = attachmentNode
        ? Array.from(attachmentNode.querySelectorAll('a[href]')).filter(visible)
            .map(anchor => ({
                text: clean(anchor.innerText || anchor.textContent).slice(0, 120),
                url: anchor.href || null,
            })).filter(item => item.url).slice(0, 20)
        : [];
    const announcement = Array.from(document.querySelectorAll('a[href]'))
        .filter(visible).map(anchor => ({
            text: clean(anchor.innerText || anchor.textContent),
            url: anchor.href || null,
        })).find(item => item.text.includes('查看公告') || item.text.includes('标的公告'));
    const images = Array.from(document.querySelectorAll('img'))
        .filter(visible).map(image => image.currentSrc || image.src || null)
        .filter(url => /^https?:\/\//.test(url || '')).slice(0, 20);
    return {
        title: titleNode ? clean(titleNode.innerText || titleNode.textContent) : '',
        bodyText,
        labelBlocks,
        detailPresent: Boolean(detailNode),
        detailText,
        detailLoading: detailText.includes('标的物详情加载中'),
        attachmentPresent: Boolean(attachmentNode),
        attachmentText,
        attachmentLoading: attachmentText.includes('附件加载中'),
        attachments,
        announcementUrl: announcement ? announcement.url : null,
        images,
    };
}"""


def _redact_url(url: str | None) -> str | None:
    if not url:
        return url
    parts = urlsplit(url)
    query = [
        (key, "REDACTED" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _validate_pc_page(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _PC_MAX_PAGE:
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            f"page 必须是 1 到 {_PC_MAX_PAGE} 之间的整数",
        )
    return value


def _validate_pc_item_id(value: Any) -> str:
    if isinstance(value, bool):
        raise AliPCBrowserError(
            "pc_detail_validation_failed",
            "item_id 必须是 8 到 20 位数字",
        )
    item_id = str(value).strip()
    if not re.fullmatch(r"\d{8,20}", item_id):
        raise AliPCBrowserError(
            "pc_detail_validation_failed",
            "item_id 必须是 8 到 20 位数字",
        )
    return item_id


def build_pc_detail_url(item_id: Any) -> str:
    return PC_DETAIL_URL_TEMPLATE.format(item_id=_validate_pc_item_id(item_id))


def _pc_detail_url_matches(url: str, item_id: str) -> bool:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == _PC_DETAIL_ALLOWED_HOST
        and port is None
        and parts.username is None
        and parts.password is None
        and parts.path.rstrip("/") == f"/sf_item/{item_id}.htm"
    )


def _yuan_from_text(text: str | None, label: str) -> int | float | None:
    if not text or label not in text:
        return None
    segment = text.split(label, 1)[1]
    match = re.search(r"[:：\s¥￥]*([0-9][0-9,]*(?:\.\d+)?)\s*(万|亿)?", segment)
    if not match:
        return None
    amount = Decimal(match.group(1).replace(",", ""))
    multiplier = {None: Decimal(1), "万": Decimal(10_000), "亿": Decimal(100_000_000)}[
        match.group(2)
    ]
    value = amount * multiplier
    return int(value) if value == value.to_integral_value() else float(value)


def _normalize_pc_detail_snapshot(
    snapshot: dict[str, Any],
    *,
    item_id: str,
    url: str,
    page_title: str,
) -> dict[str, Any]:
    body = str(snapshot.get("bodyText") or "")
    detail_text = str(snapshot.get("detailText") or "")
    label_blocks = snapshot.get("labelBlocks") or {}

    def amount(label: str) -> int | float | None:
        return _yuan_from_text(label_blocks.get(label), label)

    current_price = amount("当前价")
    if current_price is None:
        current_price = amount("变卖价")
    counts = {
        "registrationCount": None,
        "reminderCount": None,
        "viewCount": None,
    }
    for key, pattern in (
        ("registrationCount", r"(\d+)\s*人报名"),
        ("reminderCount", r"(\d+)\s*人(?:设置)?提醒"),
        ("viewCount", r"(\d+)\s*次围观"),
    ):
        match = re.search(pattern, body)
        if match:
            counts[key] = int(match.group(1))

    start_match = re.search(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:[^\d]|$)", body)
    court_match = re.search(r"([\u4e00-\u9fff]{2,30}(?:人民法院|法院))", body)
    phone_match = re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", body)
    contact_block = str(label_blocks.get("联系方式") or "")
    contact_match = re.search(r"联系方式\s*[:：]?\s*([^\s，,；;]+)", contact_block)
    location_match = re.search(r"标的物位置\s*([^。；;]+)", detail_text)
    status = next(
        (candidate for candidate in ("正在进行", "即将开始", "已结束", "中止", "撤回")
         if candidate in body),
        None,
    )
    stage = next(
        (candidate for candidate in ("重新拍卖", "二拍", "一拍", "变卖") if candidate in body),
        None,
    )
    attachments = [
        {
            "text": str(entry.get("text") or "").strip() or None,
            "url": _redact_url(entry.get("url")),
        }
        for entry in snapshot.get("attachments", [])
        if entry.get("url")
    ]
    return {
        "source": "ali_pc_browser",
        "itemId": item_id,
        "url": _redact_url(url),
        "title": str(snapshot.get("title") or page_title).strip(),
        "status": status,
        "stage": stage,
        "auctionStartAt": start_match.group(1) if start_match else None,
        "currentPriceYuan": current_price,
        "startingPriceYuan": amount("起拍价"),
        "appraisalPriceYuan": amount("评估价"),
        "depositYuan": amount("保证金"),
        "incrementYuan": amount("加价幅度"),
        **counts,
        "delayPeriod": label_blocks.get("延时周期"),
        "biddingPeriod": label_blocks.get("竞价周期"),
        "court": court_match.group(1) if court_match else None,
        "contact": {
            "name": contact_match.group(1) if contact_match else None,
            "phone": phone_match.group(1) if phone_match else None,
        },
        "announcementUrl": _redact_url(snapshot.get("announcementUrl")),
        "location": location_match.group(1).strip() if location_match else None,
        "images": list(
            dict.fromkeys(
                redacted
                for image_url in snapshot.get("images", [])
                if (redacted := _redact_url(image_url))
            )
        ),
        "detailText": detail_text,
        "attachments": attachments,
    }


def _pagination_current_page(snapshot: dict[str, Any]) -> int | None:
    values = {
        int(str(entry.get("text")))
        for entry in snapshot.get("current", [])
        if str(entry.get("text") or "").isdigit()
    }
    return next(iter(values)) if len(values) == 1 else None


def _pagination_next_candidates(
    snapshot: dict[str, Any],
    expected_page: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in snapshot.get("controls", []):
        if not item.get("visible") or item.get("disabled"):
            continue
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("text", "aria", "title", "className", "id")
        )
        is_next = bool(
            re.search(r"下一页|下页|\bnext\b|page-next|next-next", haystack, re.I)
        )
        is_previous = bool(
            re.search(r"上一页|上页|\bprev\b|previous|page-prev|prev-prev", haystack, re.I)
        )
        if not is_next or is_previous:
            continue
        href = str(item.get("href") or "")
        parts = urlsplit(href)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if (
            parts.scheme == "https"
            and parts.hostname == _PC_ALLOWED_HOST
            and query.get("page") == str(expected_page)
        ):
            candidates.append(item)
    return candidates


def _select_dimension(entry: dict) -> str | None:
    identity = f"{entry.get('id') or ''} {entry.get('name') or ''}".lower()
    if "auctionstatus" in identity:
        return "status"
    if "auctionstage" in identity or "auction_stage" in identity:
        return "stage"
    if "pricerange" in identity or "price_range" in identity:
        return "price_range"
    options = {str(option).strip() for option in entry.get("options", [])}
    return next(
        (
            dimension for dimension, marker in _SELECT_DIMENSION_MARKERS.items()
            if marker in options
        ),
        None,
    )


class AliPCBrowserError(RuntimeError):
    """带稳定错误码的 PC 浏览器适配器错误."""

    def __init__(self, code: str, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}

    def as_dict(self) -> dict:
        return {
            "error": self.code,
            "message": self.message,
            "diagnostics": self.diagnostics,
        }


def _decimal_yuan(value: int | float | str | Decimal | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AliPCBrowserError("pc_filter_validation_failed", f"{field} 必须是有效金额") from exc
    if not amount.is_finite() or amount < 0:
        raise AliPCBrowserError("pc_filter_validation_failed", f"{field} 必须是非负金额")
    if amount != amount.to_integral_value():
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            f"{field} 单位为元，当前仅接受整数金额",
        )
    return str(int(amount))


def _iso_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            f"{field} 必须使用 YYYY-MM-DD 格式",
        ) from exc
    return parsed.isoformat()


def build_pc_search_url(
    base_url: str = PC_HOME_URL,
    *,
    keyword: str | None = None,
    min_price_yuan: int | float | str | Decimal | None = None,
    max_price_yuan: int | float | str | Decimal | None = None,
    auction_start_from: str | None = None,
    auction_start_to: str | None = None,
) -> str:
    """把已由真实 PC 页面验证的筛选参数合并到 URL.

    关键词搜索会清空页面已有分类与地区筛选，因此与价格/日期或非首页
    base_url 组合时 fail-closed，防止声称不存在的组合能力。
    """
    parts = urlsplit(base_url)
    if parts.scheme != "https" or parts.hostname != _PC_ALLOWED_HOST:
        raise AliPCBrowserError(
            "pc_navigation_blocked",
            "PC 适配器只允许构造 https://sf.taobao.com 页面 URL",
            {"url": base_url},
        )

    keyword = (keyword or "").strip() or None
    minimum = _decimal_yuan(min_price_yuan, "min_price_yuan")
    maximum = _decimal_yuan(max_price_yuan, "max_price_yuan")
    start = _iso_date(auction_start_from, "auction_start_from")
    end = _iso_date(auction_start_to, "auction_start_to")

    if minimum is not None and maximum is not None and int(minimum) > int(maximum):
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "min_price_yuan 不得大于 max_price_yuan",
        )
    if bool(start) != bool(end):
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "auction_start_from 与 auction_start_to 必须同时提供",
        )
    if start and end and start > end:
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "auction_start_from 不得晚于 auction_start_to",
        )

    normalized_base = base_url.rstrip("/")
    has_existing_scope = normalized_base not in {PC_HOME_URL.rstrip("/"), PC_KEYWORD_URL.rstrip("/")}
    if keyword and (minimum or maximum or start or end or has_existing_scope):
        raise AliPCBrowserError(
            "pc_keyword_filter_conflict",
            "真实 PC 页面会在关键词搜索时清空分类、地区、价格和时间筛选，不能组合查询",
        )
    if keyword:
        return f"{PC_KEYWORD_URL}?{urlencode({'_input_charset': 'utf-8', 'q': keyword, 'keywordSource': '4'})}"

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # PC 页面入口同时接受 camelCase；规范化为筛选后地址实际使用的 snake_case。
    query.pop("startPrice", None)
    query.pop("endPrice", None)
    if minimum is None:
        query.pop("start_price", None)
    else:
        query["start_price"] = minimum
    if maximum is None:
        query.pop("end_price", None)
    else:
        query["end_price"] = maximum
    if start is None:
        query.pop("auctionStartFrom", None)
        query.pop("auctionStartTo", None)
    else:
        query["auctionStartFrom"] = start
        query["auctionStartTo"] = end or ""
    query.setdefault("auction_source", "0")
    query.setdefault("st_param", "-1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _price_yuan_from_text(text: str) -> float | None:
    match = re.search(
        r"(?:当前价|变卖价|起拍价|开拍价)\s*[¥￥]?\s*([0-9][0-9,.]*)\s*(万|亿)?",
        text,
    )
    if not match:
        return None
    value = Decimal(match.group(1).replace(",", ""))
    if match.group(2) == "万":
        value *= 10_000
    elif match.group(2) == "亿":
        value *= 100_000_000
    return float(value)


def parse_pc_item_records(records: list[dict[str, Any]], limit: int = 20) -> list[dict]:
    """把页面端抽取的卡片记录规范化，按 itemId 去重."""
    items: list[dict] = []
    seen: set[str] = set()
    for record in records:
        href = str(record.get("href") or "")
        id_match = re.search(r"(?:sf_item/|[?&](?:id|itemId)=)(\d{8,})", href)
        if not id_match:
            id_match = re.search(r"(\d{10,})", href)
        if not id_match or id_match.group(1) in seen:
            continue
        text = str(record.get("text") or "").strip()
        if not re.search(r"当前价|变卖价|起拍价|开拍价|评估价|开始时间", text):
            continue
        item_id = id_match.group(1)
        seen.add(item_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        raw_title = str(record.get("title") or "").strip()
        title_lines = [line.strip() for line in raw_title.splitlines() if line.strip()]
        title = next(
            (
                line for line in title_lines + lines
                if not re.search(
                    r"[¥￥]|当前价|变卖价|起拍价|开拍价|评估价|开始时间|结束时间|次围观|人报名",
                    line,
                )
            ),
            "",
        )
        items.append({
            "itemId": item_id,
            "title": title,
            "currentPriceYuan": _price_yuan_from_text(text),
            "url": href,
            "image": record.get("image"),
            "rawText": text,
        })
        if len(items) >= limit:
            break
    return items


def evaluate_pc_live_acceptance(
    result: dict,
    *,
    max_price_yuan: int,
    auction_start_from: str,
    auction_start_to: str,
) -> dict:
    """对一次真实 PC 查询做不含 Cookie/原始页面内容的结构化验收."""
    failures: list[str] = []
    if result.get("error"):
        failures.append(f"query_error:{result['error']}")
    if result.get("state") in {"login_required", "action_required", "stopped"}:
        failures.append(f"session_state:{result['state']}")
    if result.get("source") != "ali_pc_browser":
        failures.append("unexpected_source")
    if result.get("authenticated_session") is not True:
        failures.append("authenticated_session_not_confirmed")

    items = result.get("items") if isinstance(result.get("items"), list) else []
    if not items:
        failures.append("no_items")

    missing_price_ids: list[str] = []
    over_limit_ids: list[str] = []
    prices: list[float] = []
    for item in items:
        item_id = str(item.get("itemId") or "unknown")
        price = item.get("currentPriceYuan")
        if not isinstance(price, (int, float)):
            missing_price_ids.append(item_id)
            continue
        numeric_price = float(price)
        prices.append(numeric_price)
        if numeric_price > max_price_yuan:
            over_limit_ids.append(item_id)
    if missing_price_ids:
        failures.append("items_missing_current_price")
    if over_limit_ids:
        failures.append("items_over_max_price")

    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    result_url = str(result.get("url") or diagnostics.get("url") or "")
    query = dict(parse_qsl(urlsplit(result_url).query, keep_blank_values=True))
    expected_query = {
        "end_price": str(max_price_yuan),
        "auctionStartFrom": auction_start_from,
        "auctionStartTo": auction_start_to,
    }
    mismatched_query = {
        key: {"expected": value, "actual": query.get(key)}
        for key, value in expected_query.items()
        if query.get(key) != value
    }
    if mismatched_query:
        failures.append("verified_query_params_missing")

    samples = [
        {
            "itemId": item.get("itemId"),
            "title": item.get("title"),
            "currentPriceYuan": item.get("currentPriceYuan"),
            "url": item.get("url"),
        }
        for item in items[:3]
    ]
    return {
        "accepted": not failures,
        "failures": failures,
        "evidence": {
            "result_count": len(items),
            "reported_count": result.get("count"),
            "minimum_price_yuan": min(prices) if prices else None,
            "maximum_price_yuan": max(prices) if prices else None,
            "max_price_contract_yuan": max_price_yuan,
            "missing_price_item_ids": missing_price_ids,
            "over_limit_item_ids": over_limit_ids,
            "query_params": {key: query.get(key) for key in expected_query},
            "query_param_mismatches": mismatched_query,
            "query_url": result_url,
            "query_diagnostics": diagnostics,
            "samples": samples,
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        },
    }


def evaluate_pc_matrix_scenario(
    name: str,
    result: dict,
    expected_filters: dict[str, str],
) -> dict:
    """验收单个 PC 能力矩阵场景，不携带原始卡片正文或浏览器凭据."""
    failures: list[str] = []
    if result.get("error"):
        failures.append(f"query_error:{result['error']}")
    if result.get("state") in {"login_required", "action_required", "stopped"}:
        failures.append(f"session_state:{result['state']}")
    if result.get("source") != "ali_pc_browser":
        failures.append("unexpected_source")
    if result.get("authenticated_session") is not True:
        failures.append("authenticated_session_not_confirmed")
    items = result.get("items") if isinstance(result.get("items"), list) else []
    if not items:
        failures.append("no_items")

    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    diagnostics = {
        **diagnostics,
        "url": _redact_url(diagnostics.get("url")),
    }
    applied_entries = result.get("appliedFilters")
    if not isinstance(applied_entries, list):
        applied_entries = diagnostics.get("appliedFilters", [])
    if not isinstance(applied_entries, list):
        applied_entries = []
    applied = {
        str(entry.get("dimension")): str(entry.get("label"))
        for entry in applied_entries
        if isinstance(entry, dict) and entry.get("dimension") and entry.get("label") is not None
    }
    mismatches = {
        dimension: {"expected": label, "actual": applied.get(dimension)}
        for dimension, label in expected_filters.items()
        if applied.get(dimension) != label
    }
    if mismatches:
        failures.append("applied_filter_mismatch")

    return {
        "name": name,
        "accepted": not failures,
        "failures": failures,
        "evidence": {
            "count": len(items),
            "reported_count": result.get("count"),
            "error": result.get("error"),
            "error_message": result.get("message"),
            "diagnostics": diagnostics,
            "url": _redact_url(result.get("url")) or diagnostics.get("url"),
            "expected_filters": expected_filters,
            "applied_filters": applied,
            "filter_mismatches": mismatches,
            "samples": [
                {
                    "itemId": item.get("itemId"),
                    "title": item.get("title"),
                    "currentPriceYuan": item.get("currentPriceYuan"),
                    "url": item.get("url"),
                }
                for item in items[:2]
            ],
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        },
    }


class AliPCBrowserClient:
    """基于非持久化 Playwright context 的登录态 PC 查询客户端."""

    def __init__(self, *, headless: bool = False, timeout_ms: int = 30_000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._lock = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def _locked(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self) -> dict:
        lock = await self._locked()
        async with lock:
            if self._page and not self._page.is_closed():
                return await self._status_unlocked()
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return {
                    "error": "pc_browser_dependency_missing",
                    "message": "缺少 playwright；请先安装 requirements.txt",
                }
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    channel="chrome",
                    headless=self.headless,
                )
                # 非持久化 context：不指定 user_data_dir，不导出 storage_state。
                self._context = await self._browser.new_context(locale="zh-CN")
                self._page = await self._context.new_page()
                self._page.set_default_timeout(self.timeout_ms)
                await self._page.goto(PC_HOME_URL, wait_until="domcontentloaded")
                return await self._status_unlocked()
            except Exception as exc:
                await self._close_unlocked()
                return {
                    "error": "pc_browser_start_failed",
                    "message": str(exc),
                }

    async def status(self) -> dict:
        lock = await self._locked()
        async with lock:
            return await self._status_unlocked()

    async def _status_unlocked(self) -> dict:
        if not self._page or self._page.is_closed():
            return {"state": "stopped", "cookie_policy": "browser_memory_only"}
        url = self._page.url
        title = await self._page.title()
        body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
        action = self._action_required(url, title, body)
        if action:
            return action
        login_text = ""
        login_node = self._page.locator("#J_SiteNavLogin")
        if await login_node.count():
            login_text = (await login_node.first.inner_text()).strip()
        authenticated = bool(login_text and "请登录" not in login_text)
        return {
            "state": "ready" if authenticated else "login_required",
            "authenticated": authenticated,
            "url": url,
            "title": title,
            "message": (
                "浏览器会话已登录，可调用 ali_pc_search_judicial 或 ali_pc_get_item_detail"
                if authenticated
                else "请在已打开的 Chrome 窗口手动登录，完成后调用 ali_pc_browser_status"
            ),
            "cookie_policy": "browser_memory_only",
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        }

    @staticmethod
    def _action_required(url: str, title: str, body: str) -> dict | None:
        combined = f"{title}\n{body}"
        if any(marker in url for marker in _PUNISH_MARKERS):
            return {
                "state": "action_required",
                "reason": "risk_control",
                "message": "页面进入 Ali 风控页，请用户在浏览器中手动处理；适配器不会绕过验证",
                "url": _redact_url(url),
            }
        if any(marker in combined for marker in _VERIFICATION_MARKERS):
            return {
                "state": "action_required",
                "reason": "verification",
                "message": "页面要求人工验证，请用户手动完成；适配器不会操作验证码或滑块",
                "url": _redact_url(url),
            }
        if "login.taobao.com" in url:
            return {
                "state": "action_required",
                "reason": "login",
                "message": "请用户在浏览器中手动登录，适配器不会读取或填写凭据",
                "url": _redact_url(url),
            }
        return None

    async def _read_select_entries(self) -> list[dict]:
        assert self._page is not None
        return await self._page.eval_on_selector_all(
            "select",
            """selects => selects.map((select, index) => ({
                select,
                index,
                root: select.closest('li.block') || select.parentElement
            })).map(({select, index, root}) => {
                const nativeOptions = Array.from(select.options)
                    .map(o => (o.textContent || '').trim()).filter(Boolean);
                const content = root ? root.querySelector('.bf-select-content') : null;
                const dropdown = root ? root.querySelector('.bf-select-dropdown') : null;
                const customOptions = dropdown
                    ? Array.from(dropdown.querySelectorAll('*'))
                        .filter(el => {
                            const text = (el.textContent || '').trim();
                            if (!text || text.length > 40) return false;
                            return !Array.from(el.children).some(
                                child => (child.textContent || '').trim() === text
                            );
                        })
                        .map(el => (el.textContent || '').trim())
                    : [];
                return {
                    index,
                    id: select.id || null,
                    name: select.name || null,
                    className: select.className || null,
                    selected: select.selectedOptions.length
                        ? (select.selectedOptions[0].textContent || '').trim()
                        : null,
                    customSelected: content ? (content.textContent || '').trim() : null,
                    nativeOptions,
                    customOptions: Array.from(new Set(customOptions)),
                    options: Array.from(new Set(nativeOptions.concat(customOptions)))
                };
            })""",
        )

    async def _filter_options_snapshot_unlocked(self) -> dict:
        assert self._page is not None
        links = await self._page.eval_on_selector_all(
            "a[href]",
            r"""anchors => anchors.map(a => {
                const text = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ');
                const rect = a.getBoundingClientRect();
                if (!text || text.length > 40) return null;
                return {
                    text,
                    href: a.href || '',
                    visible: rect.width > 0 && rect.height > 0
                };
            }).filter(Boolean)""",
        )
        link_options: list[dict] = []
        seen_links: set[tuple[str, str]] = set()
        for entry in links:
            text = str(entry.get("text") or "").strip()
            href = str(entry.get("href") or "")
            parts = urlsplit(href)
            key = (text, href)
            if (
                text
                and parts.scheme == "https"
                and parts.hostname == _PC_ALLOWED_HOST
                and key not in seen_links
            ):
                seen_links.add(key)
                link_options.append({
                    "text": text,
                    "href": href,
                    "visible": bool(entry.get("visible")),
                })

        selects = await self._read_select_entries()
        dimensions: dict[str, dict] = {}
        for entry in selects:
            options = [str(option).strip() for option in entry.get("options", []) if str(option).strip()]
            dimension = _select_dimension(entry)
            if dimension and dimension not in dimensions:
                dimensions[dimension] = {
                    "index": entry.get("index"),
                    "id": entry.get("id"),
                    "selected": entry.get("customSelected") or entry.get("selected"),
                    "nativeOptions": entry.get("nativeOptions", []),
                    "customOptions": entry.get("customOptions", []),
                    "options": options,
                }

        controls = await self._page.eval_on_selector_all(
            "input",
            """inputs => inputs.map((input, index) => ({
                index,
                type: input.type || 'text',
                name: input.name || null,
                placeholder: input.placeholder || null,
                valuePresent: Boolean(input.value)
            }))""",
        )
        return {
            "state": "ready",
            "url": self._page.url,
            "linkOptionCount": len(link_options),
            "linkOptions": link_options,
            "selectDimensions": dimensions,
            "unclassifiedSelects": [
                entry for entry in selects
                if entry.get("index") not in {
                    dimension.get("index") for dimension in dimensions.values()
                }
            ],
            "inputControls": controls,
            "cookie_policy": "browser_memory_only",
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        }

    async def get_filter_options(
        self,
        *,
        category: str | None = None,
        province: str | None = None,
        city: str | None = None,
    ) -> dict:
        """从当前真实 PC DOM 动态读取链接、下拉框和输入控件能力."""
        lock = await self._locked()
        async with lock:
            if not self._page or self._page.is_closed():
                return {
                    "error": "pc_browser_not_started",
                    "message": "请先调用 ali_pc_browser_start，并在打开的 Chrome 窗口完成登录",
                }
            session = await self._status_unlocked()
            if session.get("state") != "ready":
                return session
            if city and not province:
                return AliPCBrowserError(
                    "pc_filter_validation_failed",
                    "读取城市下级能力时必须同时提供 province",
                ).as_dict()
            try:
                await self._page.goto(PC_HOME_URL, wait_until="domcontentloaded")
                trace: list[dict] = []
                for label, dimension in (
                    (category, "category"),
                    (province, "province"),
                    (city, "city"),
                ):
                    if label:
                        trace.append(await self._navigate_exact_link(label, dimension))
                title = await self._page.title()
                body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                action = self._action_required(self._page.url, title, body)
                if action:
                    return action
                snapshot = await self._filter_options_snapshot_unlocked()
                snapshot["scope"] = {"category": category, "province": province, "city": city}
                snapshot["appliedFilters"] = trace
                return snapshot
            except AliPCBrowserError as exc:
                return exc.as_dict()
            except Exception as exc:
                return {
                    "error": "pc_filter_options_failed",
                    "message": str(exc),
                    "diagnostics": {"url": self._page.url if self._page else None},
                }

    async def _navigate_exact_link(self, label: str, dimension: str) -> dict:
        assert self._page is not None
        hrefs = await self._page.eval_on_selector_all(
            "a[href]",
            """(anchors, wanted) => anchors
                .filter(a => (a.innerText || a.textContent || '').trim() === wanted)
                .map(a => a.href)
                .filter(Boolean)""",
            label,
        )
        unique = [
            href for href in dict.fromkeys(hrefs)
            if urlsplit(href).scheme == "https" and urlsplit(href).hostname == _PC_ALLOWED_HOST
        ]
        if len(unique) != 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法在当前 PC 页面唯一解析{dimension}中文名: {label}",
                {
                    "dimension": dimension,
                    "name": label,
                    "match_count": len(unique),
                    "url": self._page.url,
                },
            )
        await self._page.goto(urljoin(self._page.url, unique[0]), wait_until="domcontentloaded")
        return {"dimension": dimension, "label": label, "url": self._page.url}

    async def _select_option_exact(self, label: str, dimension: str) -> dict:
        assert self._page is not None
        selects = await self._read_select_entries()
        matches = [
            entry for entry in selects
            if label in (entry.get("nativeOptions") or entry.get("options", []))
        ]
        if len(matches) != 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法唯一解析{dimension}选项: {label}",
                {"dimension": dimension, "name": label, "match_count": len(matches)},
            )
        locator = self._page.locator("select").nth(matches[0]["index"])
        await locator.select_option(label=label)
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            # 部分旧页面 change 事件只做同步跳转；后续状态检查负责判定结果。
            pass
        deadline = asyncio.get_running_loop().time() + min(5, self.timeout_ms / 1000)
        while True:
            current = await self._read_select_entries()
            selected_matches = [entry for entry in current if entry.get("selected") == label]
            if len(selected_matches) == 1:
                return {"dimension": dimension, "label": label, "url": self._page.url}
            remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
            if remaining_ms <= 0:
                raise AliPCBrowserError(
                    "pc_filter_application_failed",
                    f"页面未确认已应用{dimension}选项: {label}",
                    {"dimension": dimension, "name": label, "url": self._page.url},
                )
            await self._page.wait_for_timeout(min(250, remaining_ms))

    async def _select_custom_option_exact(
        self,
        entry: dict,
        label: str,
        dimension: str,
    ) -> dict:
        """操作真实 bf-select 组件并验证可见回显，底层隐藏 select 仅作定位."""
        assert self._page is not None
        select_id = str(entry.get("id") or "")
        if not select_id:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"{dimension}自定义控件缺少稳定 select id",
                {"dimension": dimension, "name": label},
            )
        trigger = await self._page.evaluate(
            """selectId => {
                const select = document.getElementById(selectId);
                const root = select && (select.closest('li.block') || select.parentElement);
                const content = root && root.querySelector('.bf-select-content');
                if (!content) return {ok: false};
                content.click();
                return {ok: true, selected: (content.textContent || '').trim()};
            }""",
            select_id,
        )
        if not trigger.get("ok"):
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法展开{dimension}自定义控件: {label}",
                {"dimension": dimension, "name": label, "select_id": select_id},
            )
        await self._page.wait_for_timeout(200)
        click_evaluation_interrupted = False
        try:
            clicked = await self._page.evaluate(
                """({selectId, wanted}) => {
                    const select = document.getElementById(selectId);
                    const root = select && (select.closest('li.block') || select.parentElement);
                    const dropdown = root && root.querySelector('.bf-select-dropdown');
                    if (!dropdown) return {matchCount: 0};
                    const visible = el => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const matches = Array.from(dropdown.querySelectorAll('*')).filter(el => {
                        if ((el.textContent || '').trim() !== wanted || !visible(el)) return false;
                        return !Array.from(el.children).some(
                            child => (child.textContent || '').trim() === wanted && visible(child)
                        );
                    });
                    if (matches.length === 1) matches[0].click();
                    return {matchCount: matches.length};
                }""",
                {"selectId": select_id, "wanted": label},
            )
        except Exception:
            # 旧页面点击选项会同步导航，Playwright 的 evaluate 上下文随页面销毁。
            # 后续必须通过新页面可见回显再次验证，不能仅凭此异常认定成功。
            clicked = None
            click_evaluation_interrupted = True
        if clicked is not None and clicked.get("matchCount") != 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法唯一点击{dimension}自定义选项: {label}",
                {
                    "dimension": dimension,
                    "name": label,
                    "select_id": select_id,
                    "match_count": clicked.get("matchCount"),
                },
            )
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            pass
        deadline = asyncio.get_running_loop().time() + min(5, self.timeout_ms / 1000)
        while True:
            current = await self._read_select_entries()
            selected = next((item for item in current if item.get("id") == select_id), None)
            if selected and (
                selected.get("customSelected") == label or selected.get("selected") == label
            ):
                return {
                    "dimension": dimension,
                    "label": label,
                    "url": self._page.url,
                    "controlType": "bf-select",
                    "selectId": select_id,
                    "clickEvaluationInterrupted": click_evaluation_interrupted,
                }
            remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
            if remaining_ms <= 0:
                raise AliPCBrowserError(
                    "pc_filter_application_failed",
                    f"页面未确认已应用{dimension}自定义选项: {label}",
                    {"dimension": dimension, "name": label, "select_id": select_id},
                )
            await self._page.wait_for_timeout(min(250, remaining_ms))

    async def _apply_choice_exact(self, label: str, dimension: str) -> dict:
        """bf-select/原生 select 优先；否则只接受唯一的同站精确链接."""
        selects = await self._read_select_entries()
        custom_matches = [
            entry for entry in selects
            if _select_dimension(entry) == dimension
            and label in entry.get("customOptions", [])
        ]
        if len(custom_matches) == 1:
            return await self._select_custom_option_exact(custom_matches[0], label, dimension)
        if len(custom_matches) > 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法唯一解析{dimension}自定义选项: {label}",
                {"dimension": dimension, "name": label, "match_count": len(custom_matches)},
            )
        native_matches = [
            entry for entry in selects
            if label in (entry.get("nativeOptions") or entry.get("options", []))
        ]
        if len(native_matches) == 1:
            trace = await self._select_option_exact(label, dimension)
            trace["controlType"] = "select"
            return trace
        if len(native_matches) > 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法唯一解析{dimension}原生下拉选项: {label}",
                {"dimension": dimension, "name": label, "match_count": len(native_matches)},
            )
        trace = await self._navigate_exact_link(label, dimension)
        trace["controlType"] = "link"
        return trace

    async def _pagination_snapshot_unlocked(self) -> dict[str, Any]:
        assert self._page is not None
        return await self._page.evaluate(_PC_PAGER_SNAPSHOT_SCRIPT)

    async def _wait_for_page_items_change(
        self,
        previous_ids: list[str],
    ) -> tuple[list[dict], dict]:
        assert self._page is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_ms / 1000
        last_items: list[dict] = []
        diagnostics: dict[str, Any] = {}
        while True:
            last_items, diagnostics = await self._wait_for_items(100)
            current_ids = [
                str(item.get("itemId"))
                for item in last_items
                if item.get("itemId") is not None
            ]
            if current_ids and current_ids != previous_ids:
                return last_items, diagnostics
            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                return last_items, diagnostics
            await self._page.wait_for_timeout(min(500, remaining_ms))

    async def _navigate_to_page(self, target_page: int) -> dict[str, Any]:
        assert self._page is not None
        if target_page == 1:
            return {"page": 1, "pageTurns": 0, "paginationReceipts": []}

        previous_items, previous_diagnostics = await self._wait_for_items(100)
        previous_ids = [
            str(item.get("itemId"))
            for item in previous_items
            if item.get("itemId") is not None
        ]
        if not previous_ids:
            raise AliPCBrowserError(
                "pc_pagination_application_failed",
                "无法在翻页前确认当前页标的集合",
                {"page": 1, "url": _redact_url(self._page.url), **previous_diagnostics},
            )

        receipts: list[dict[str, Any]] = []
        for expected_page in range(2, target_page + 1):
            snapshot = await self._pagination_snapshot_unlocked()
            current_page = _pagination_current_page(snapshot)
            if current_page != expected_page - 1:
                raise AliPCBrowserError(
                    "pc_pagination_resolution_failed",
                    "当前页码指示与预期不一致，拒绝继续翻页",
                    {
                        "expected_current_page": expected_page - 1,
                        "actual_current_page": current_page,
                        "url": _redact_url(self._page.url),
                    },
                )
            candidates = _pagination_next_candidates(snapshot, expected_page)
            if len(candidates) != 1:
                raise AliPCBrowserError(
                    "pc_pagination_resolution_failed",
                    "无法唯一解析指向预期页码的下一页控件",
                    {
                        "expected_page": expected_page,
                        "match_count": len(candidates),
                        "url": _redact_url(self._page.url),
                    },
                )

            candidate = candidates[0]
            await self._page.locator(_PC_CLICKABLE_SELECTOR).nth(candidate["index"]).click()
            try:
                await self._page.wait_for_load_state(
                    "domcontentloaded", timeout=self.timeout_ms
                )
            except Exception:
                pass
            await self._page.wait_for_timeout(1500)

            title = await self._page.title()
            body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
            action = self._action_required(self._page.url, title, body)
            if action:
                return {"action_required": action}

            current_items, parse_diagnostics = await self._wait_for_page_items_change(
                previous_ids
            )
            current_ids = [
                str(item.get("itemId"))
                for item in current_items
                if item.get("itemId") is not None
            ]
            current_snapshot = await self._pagination_snapshot_unlocked()
            confirmed_page = _pagination_current_page(current_snapshot)
            if confirmed_page != expected_page or not current_ids or current_ids == previous_ids:
                raise AliPCBrowserError(
                    "pc_pagination_application_failed",
                    "页面未同时确认页码递增和标的集合变化",
                    {
                        "expected_page": expected_page,
                        "actual_page": confirmed_page,
                        "previous_count": len(previous_ids),
                        "current_count": len(current_ids),
                        "page_sets_differ": current_ids != previous_ids,
                        "url": _redact_url(self._page.url),
                        **parse_diagnostics,
                    },
                )

            overlap = sorted(set(previous_ids) & set(current_ids))
            receipts.append({
                "fromPage": expected_page - 1,
                "toPage": expected_page,
                "url": _redact_url(self._page.url),
                "previousCount": len(previous_ids),
                "currentCount": len(current_ids),
                "overlapCount": len(overlap),
                "newItemCount": len(set(current_ids) - set(previous_ids)),
                "control": {
                    "tag": candidate.get("tag"),
                    "className": candidate.get("className"),
                    "href": _redact_url(candidate.get("href")),
                },
            })
            previous_ids = current_ids

        return {
            "page": target_page,
            "pageTurns": target_page - 1,
            "paginationReceipts": receipts,
        }

    async def _wait_for_items(self, limit: int) -> tuple[list[dict], dict]:
        """轮询等待旧 PC 页面异步渲染拍品卡片，避免 DOMContentLoaded 过早解析."""
        assert self._page is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_ms / 1000
        attempts = 0
        last_candidate_count = 0
        while True:
            attempts += 1
            records = await self._page.eval_on_selector_all(
                "a[href]",
                r"""anchors => anchors.map(a => {
                    const href = a.href || '';
                    if (!/\d{8,}/.test(href) || !/(?:sf_item\/|item\.htm)/.test(href)) return null;
                    let root = a;
                    for (let i = 0; i < 8 && root && root.parentElement; i++) {
                        const parentText = (root.parentElement.innerText || '').trim();
                        if (/当前价|变卖价|起拍价|开拍价|评估价|开始时间/.test(parentText)
                            && parentText.length < 2500) {
                            root = root.parentElement;
                            break;
                        }
                        root = root.parentElement;
                    }
                    const image = root ? root.querySelector('img') : null;
                    return {
                        href,
                        title: (a.getAttribute('title') || a.innerText || '').trim(),
                        text: root ? (root.innerText || '').trim() : '',
                        image: image ? (image.currentSrc || image.src || image.getAttribute('data-ks-lazyload')) : null
                    };
                }).filter(Boolean)""",
            )
            last_candidate_count = len(records)
            items = parse_pc_item_records(records, limit=limit)
            if items:
                return items, {
                    "poll_attempts": attempts,
                    "candidate_record_count": last_candidate_count,
                }
            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                return [], {
                    "poll_attempts": attempts,
                    "candidate_record_count": last_candidate_count,
                }
            await self._page.wait_for_timeout(min(500, remaining_ms))

    async def _wait_for_detail_snapshot(
        self,
        item_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """等待详情正文与附件异步容器完成，拒绝把占位文本当作详情."""
        assert self._page is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_ms / 1000
        attempts = 0
        last_snapshot: dict[str, Any] = {}
        while True:
            attempts += 1
            last_snapshot = await self._page.evaluate(_PC_DETAIL_SNAPSHOT_SCRIPT)
            title = await self._page.title()
            action = self._action_required(
                self._page.url,
                title,
                str(last_snapshot.get("bodyText") or ""),
            )
            if action:
                return {"action_required": action}, {"poll_attempts": attempts}

            label_blocks = last_snapshot.get("labelBlocks") or {}
            price_ready = any(
                label_blocks.get(label)
                for label in ("当前价", "变卖价", "起拍价", "保证金", "评估价")
            )
            detail_ready = (
                bool(last_snapshot.get("detailPresent"))
                and bool(str(last_snapshot.get("detailText") or "").strip())
                and not last_snapshot.get("detailLoading")
            )
            attachment_ready = (
                bool(last_snapshot.get("attachmentPresent"))
                and not last_snapshot.get("attachmentLoading")
            )
            loading_markers_present = any(
                marker in (
                    f"{last_snapshot.get('detailText') or ''} "
                    f"{last_snapshot.get('attachmentText') or ''}"
                )
                for marker in _DETAIL_LOADING_MARKERS
            )
            detail_ready = detail_ready and not loading_markers_present
            attachment_ready = attachment_ready and not loading_markers_present
            url_ready = _pc_detail_url_matches(self._page.url, item_id)
            if price_ready and detail_ready and attachment_ready and url_ready:
                return last_snapshot, {
                    "poll_attempts": attempts,
                    "price_ready": True,
                    "detail_ready": True,
                    "attachment_ready": True,
                    "url_ready": True,
                }

            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                return last_snapshot, {
                    "poll_attempts": attempts,
                    "price_ready": price_ready,
                    "detail_ready": detail_ready,
                    "attachment_ready": attachment_ready,
                    "url_ready": url_ready,
                    "detail_present": bool(last_snapshot.get("detailPresent")),
                    "attachment_present": bool(last_snapshot.get("attachmentPresent")),
                    "detail_loading": bool(last_snapshot.get("detailLoading")),
                    "attachment_loading": bool(last_snapshot.get("attachmentLoading")),
                }
            await self._page.wait_for_timeout(min(500, remaining_ms))

    async def get_item_detail(self, item_id: Any) -> dict[str, Any]:
        """读取一个受限阿里 PC 详情页，不读取或持久化 Cookie."""
        lock = await self._locked()
        async with lock:
            if not self._page or self._page.is_closed():
                return {
                    "error": "pc_browser_not_started",
                    "message": "请先调用 ali_pc_browser_start，并在打开的 Chrome 窗口完成登录",
                }
            session = await self._status_unlocked()
            if session.get("state") != "ready":
                return session
            try:
                normalized_id = _validate_pc_item_id(item_id)
                target_url = build_pc_detail_url(normalized_id)
                await self._page.goto(target_url, wait_until="domcontentloaded")
                title = await self._page.title()
                body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                action = self._action_required(self._page.url, title, body)
                if action:
                    return action
                if not _pc_detail_url_matches(self._page.url, normalized_id):
                    raise AliPCBrowserError(
                        "pc_detail_target_mismatch",
                        "详情页最终 URL 与请求 item_id 不一致",
                        {
                            "item_id": normalized_id,
                            "url": _redact_url(self._page.url),
                        },
                    )

                snapshot, diagnostics = await self._wait_for_detail_snapshot(normalized_id)
                if snapshot.get("action_required"):
                    return snapshot["action_required"]
                if not all(
                    diagnostics.get(key)
                    for key in (
                        "price_ready",
                        "detail_ready",
                        "attachment_ready",
                        "url_ready",
                    )
                ):
                    raise AliPCBrowserError(
                        "pc_detail_content_not_ready",
                        "详情正文或附件仍处于异步加载状态，未返回不完整详情",
                        {
                            "item_id": normalized_id,
                            "url": _redact_url(self._page.url),
                            **diagnostics,
                        },
                    )

                detail = _normalize_pc_detail_snapshot(
                    snapshot,
                    item_id=normalized_id,
                    url=self._page.url,
                    page_title=title,
                )
                return {
                    **detail,
                    "diagnostics": diagnostics,
                    "authenticated_session": True,
                    "cookie_policy": "browser_memory_only",
                    "cookie_exported": False,
                    "cookie_persisted_by_adapter": False,
                }
            except AliPCBrowserError as exc:
                return exc.as_dict()
            except Exception as exc:
                return {
                    "error": "pc_detail_query_failed",
                    "message": "阿里 PC 详情读取失败",
                    "diagnostics": {
                        "item_id": str(item_id),
                        "url": _redact_url(self._page.url if self._page else None),
                        "exception_type": type(exc).__name__,
                    },
                }

    async def search(
        self,
        *,
        keyword: str | None = None,
        category: str | None = None,
        province: str | None = None,
        city: str | None = None,
        district: str | None = None,
        asset_type: str | None = None,
        sort: str | None = None,
        status: str | None = None,
        stage: str | None = None,
        min_price_yuan: int | float | str | None = None,
        max_price_yuan: int | float | str | None = None,
        auction_start_from: str | None = None,
        auction_start_to: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict:
        lock = await self._locked()
        async with lock:
            if not self._page or self._page.is_closed():
                return {
                    "error": "pc_browser_not_started",
                    "message": "请先调用 ali_pc_browser_start，并在打开的 Chrome 窗口完成登录",
                }
            session = await self._status_unlocked()
            if session.get("state") != "ready":
                return session
            if not 1 <= limit <= 100:
                return AliPCBrowserError(
                    "pc_filter_validation_failed", "limit 必须在 1 到 100 之间"
                ).as_dict()
            try:
                target_page = _validate_pc_page(page)
            except AliPCBrowserError as exc:
                return exc.as_dict()

            scoped = any((category, province, city, district, asset_type, sort, status, stage))
            try:
                # 真实页面已验证关键词会清空其他筛选，组合时必须拒绝。
                if keyword and (scoped or min_price_yuan is not None or max_price_yuan is not None
                                or auction_start_from or auction_start_to):
                    raise AliPCBrowserError(
                        "pc_keyword_filter_conflict",
                        "关键词搜索会清空其他 PC 筛选，当前不支持组合",
                    )

                applied_filters: list[dict] = []
                await self._page.goto(PC_HOME_URL, wait_until="domcontentloaded")
                if keyword:
                    target = build_pc_search_url(keyword=keyword)
                    await self._page.goto(target, wait_until="domcontentloaded")
                    applied_filters.append({
                        "dimension": "keyword",
                        "label": keyword,
                        "url": self._page.url,
                    })
                else:
                    for label, dimension in (
                        (category, "category"),
                        (province, "province"),
                        (city, "city"),
                        (district, "district"),
                        (asset_type, "asset_type"),
                    ):
                        if label:
                            applied_filters.append(
                                await self._navigate_exact_link(label, dimension)
                            )
                    for label, dimension in (
                        (sort, "sort"),
                        (status, "status"),
                        (stage, "stage"),
                    ):
                        if label:
                            applied_filters.append(
                                await self._apply_choice_exact(label, dimension)
                            )
                    target = build_pc_search_url(
                        self._page.url,
                        min_price_yuan=min_price_yuan,
                        max_price_yuan=max_price_yuan,
                        auction_start_from=auction_start_from,
                        auction_start_to=auction_start_to,
                    )
                    if target != self._page.url:
                        await self._page.goto(target, wait_until="domcontentloaded")
                    for dimension, label in (
                        ("min_price_yuan", min_price_yuan),
                        ("max_price_yuan", max_price_yuan),
                        ("auction_start_from", auction_start_from),
                        ("auction_start_to", auction_start_to),
                    ):
                        if label is not None:
                            applied_filters.append({
                                "dimension": dimension,
                                "label": str(label),
                                "url": self._page.url,
                            })

                title = await self._page.title()
                body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                action = self._action_required(self._page.url, title, body)
                if action:
                    return action
                pagination = await self._navigate_to_page(target_page)
                if pagination.get("action_required"):
                    return pagination["action_required"]
                items, parse_diagnostics = await self._wait_for_items(limit)
                if not items:
                    final_body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                    return {
                        "error": "pc_result_parse_failed",
                        "message": "PC 页面已加载，但未能识别拍品卡片；未返回未经验证的空结果",
                        "diagnostics": {
                            "url": self._page.url,
                            "title": title,
                            "body_length": len(final_body),
                            "body_has_price_markers": bool(re.search(
                                r"当前价|变卖价|起拍价|开拍价|评估价|开始时间",
                                final_body,
                            )),
                            "appliedFilters": applied_filters,
                            **parse_diagnostics,
                        },
                    }
                return {
                    "source": "ali_pc_browser",
                    "count": len(items),
                    "items": items,
                    "url": self._page.url,
                    "appliedFilters": applied_filters,
                    **pagination,
                    "authenticated_session": True,
                    "cookie_policy": "browser_memory_only",
                    "cookie_exported": False,
                    "cookie_persisted_by_adapter": False,
                }
            except AliPCBrowserError as exc:
                return exc.as_dict()
            except Exception as exc:
                return {
                    "error": "pc_browser_search_failed",
                    "message": str(exc),
                    "diagnostics": {
                        "url": self._page.url if self._page else None,
                        "exception_type": type(exc).__name__,
                        "appliedFilters": locals().get("applied_filters", []),
                    },
                }

    async def close(self) -> dict:
        lock = await self._locked()
        async with lock:
            await self._close_unlocked()
            return {
                "state": "stopped",
                "cookie_policy": "browser_memory_only",
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }

    async def _close_unlocked(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
