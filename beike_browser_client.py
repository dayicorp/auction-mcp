"""Attach-only Beike browser provider for the Jiangmen consumer workflow.

The provider deliberately reuses one existing ``*.ke.com`` page from a local
Chrome DevTools Protocol session.  It never launches or closes a browser,
creates a page/context, or reads browser credentials and storage.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from statistics import median
import os
import re
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


CITY_HOSTS = {"江门": "jiangmen.ke.com", "江门市": "jiangmen.ke.com"}
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
XIAOQU_ID_PATTERN = re.compile(r"^[0-9]{10,20}$")
XIAOQU_SUGGESTION_PATTERN = re.compile(r"/xiaoqu/c([0-9]{10,20})/")
LISTING_URL_PATTERN = re.compile(
    r"^https://jiangmen\.ke\.com/ershoufang/[0-9]+\.html(?:[?#].*)?$"
)


@dataclass(frozen=True)
class BeikeBrowserError(RuntimeError):
    """Structured, public fail-closed provider error."""

    code: str
    message: str
    details: dict[str, Any] | None = None

    def as_result(self, **extra: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "NO_MATCH" if self.code == "BEIKE_NO_MATCH" else "ERROR",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details or {},
            },
        }
        result.update(extra)
        return result


def _validated_cdp_url(value: str | None = None) -> str:
    candidate = value or os.environ.get("AUCTION_MCP_BEIKE_CDP_URL", DEFAULT_CDP_URL)
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise BeikeBrowserError(
            "BEIKE_BROWSER_DISCONNECTED",
            "贝壳 CDP 配置必须是无凭据的本机 HTTP 地址",
        )
    return candidate.rstrip("/")


def _city_host(city: str) -> str:
    try:
        return CITY_HOSTS[city.strip()]
    except (AttributeError, KeyError):
        raise BeikeBrowserError(
            "BEIKE_UNSUPPORTED_CITY",
            "当前贝壳 Provider 仅支持江门市",
            {"supported_cities": ["江门市"]},
        ) from None


def _validated_keyword(keyword: str) -> str:
    normalized = keyword.strip() if isinstance(keyword, str) else ""
    if not normalized or len(normalized) > 64 or any(ord(ch) < 32 for ch in normalized):
        raise BeikeBrowserError(
            "BEIKE_DOM_CONTRACT_DRIFT",
            "keyword 必须是 1 至 64 个可见字符",
        )
    return normalized


def _validated_xiaoqu_id(xiaoqu_id: str) -> str:
    value = str(xiaoqu_id).strip()
    if not XIAOQU_ID_PATTERN.fullmatch(value):
        raise BeikeBrowserError(
            "BEIKE_INVALID_XIAOQU_ID",
            "xiaoqu_id 必须是 10 至 20 位纯数字",
        )
    return value


def _validated_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise BeikeBrowserError(
            "BEIKE_DOM_CONTRACT_DRIFT",
            "limit 必须是 1 至 30 的整数",
        )
    return limit


def _safe_ke_url(url: str, expected_host: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise BeikeBrowserError(
            "BEIKE_DOM_CONTRACT_DRIFT",
            "贝壳页面重定向到了未授权域名",
            {"expected_host": expected_host, "actual_host": parsed.hostname or ""},
        )


def _action_required(url: str, title: str, expected_host: str) -> None:
    parsed = urlparse(url)
    lowered = f"{url} {title}".lower()
    if parsed.hostname == "hip.ke.com" or any(
        marker in lowered for marker in ("captcha", "verifycode", "人机验证", "验证码")
    ):
        raise BeikeBrowserError(
            "BEIKE_CAPTCHA_REQUIRED",
            "贝壳页面要求人工完成验证码；Provider 已停止",
        )
    if parsed.hostname in {"passport.ke.com", "login.ke.com"} or any(
        marker in lowered for marker in ("/login", "登录贝壳", "账号登录")
    ):
        raise BeikeBrowserError(
            "BEIKE_LOGIN_REQUIRED",
            "贝壳页面要求用户手动登录；Provider 已停止",
        )
    _safe_ke_url(url, expected_host)


def normalize_suggestions(raw: list[dict[str, Any]], host: str) -> list[dict[str, str]]:
    """Validate current autocomplete DOM snapshots without inventing matches."""
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        href = str(item.get("href") or "")
        match = XIAOQU_SUGGESTION_PATTERN.search(urlparse(href).path)
        if not match or match.group(1) in seen:
            continue
        kind = str(item.get("kind") or "").strip()
        if kind and kind != "小区":
            continue
        absolute = href if href.startswith("https://") else f"https://{host}{href}"
        _safe_ke_url(absolute, host)
        name = str(item.get("name") or "").strip()
        region = str(item.get("region") or "").strip()
        if not name or not region:
            raise BeikeBrowserError(
                "BEIKE_DOM_CONTRACT_DRIFT",
                "贝壳搜索候选缺少名称或区域字段",
            )
        seen.add(match.group(1))
        candidates.append(
            {
                "xiaoqu_id": match.group(1),
                "name": name,
                "region": region,
                "source_url": absolute,
            }
        )
    return candidates


def parse_detail_snapshot(raw: dict[str, Any], xiaoqu_id: str, source_url: str) -> dict[str, Any]:
    name = str(raw.get("name") or "").strip()
    unit_price_text = str(raw.get("unit_price") or "")
    unit_match = re.search(r"[0-9][0-9,]*", unit_price_text)
    if not name or not unit_match:
        raise BeikeBrowserError(
            "BEIKE_DOM_CONTRACT_DRIFT",
            "贝壳小区详情缺少名称或挂牌均价",
        )
    facts: dict[str, str] = {}
    for text in raw.get("items") or []:
        parts = [part.strip() for part in str(text).splitlines() if part.strip()]
        if len(parts) != 2:
            raise BeikeBrowserError(
                "BEIKE_DOM_CONTRACT_DRIFT",
                "贝壳小区详情字段结构发生漂移",
                {"field_text": str(text)[:120]},
            )
        facts[parts[0]] = parts[1]
    if not facts:
        raise BeikeBrowserError(
            "BEIKE_DOM_CONTRACT_DRIFT",
            "贝壳小区详情字段为空",
        )
    return {
        "xiaoqu_id": xiaoqu_id,
        "name": name,
        "address": str(raw.get("address") or "").strip() or None,
        "listing_average_unit_price_yuan": int(unit_match.group().replace(",", "")),
        "facts": facts,
        "source_url": source_url,
    }


def parse_primary_listings(raw: list[dict[str, Any]], host: str) -> tuple[list[dict[str, Any]], int]:
    """Parse only a caller-selected primary ``ul`` and reject partial cards."""
    listings: list[dict[str, Any]] = []
    excluded = 0
    for index, item in enumerate(raw):
        classes = set(str(item.get("class_name") or "").split())
        if "VIEWDATA" in classes:
            excluded += 1
            continue
        title = str(item.get("title") or "").strip()
        href = str(item.get("href") or "").strip()
        house_info = str(item.get("house_info") or "").strip()
        total_text = str(item.get("total_price") or "").strip()
        unit_text = str(item.get("unit_price") or "").strip()
        if not all((title, href, house_info, total_text, unit_text)):
            raise BeikeBrowserError(
                "BEIKE_DOM_CONTRACT_DRIFT",
                "贝壳第一主列表存在不完整挂牌卡片",
                {"card_index": index},
            )
        if href.startswith("/"):
            href = f"https://{host}{href}"
        if not LISTING_URL_PATTERN.fullmatch(href):
            raise BeikeBrowserError(
                "BEIKE_DOM_CONTRACT_DRIFT",
                "贝壳挂牌链接不符合江门二手房公开契约",
                {"card_index": index},
            )
        area_match = re.search(r"([0-9]+(?:\.[0-9]+)?)平米", house_info)
        total_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", total_text.replace(",", ""))
        unit_match = re.search(r"([0-9][0-9,]*)元/平", unit_text)
        layout_match = re.search(r"([0-9]+室[0-9]+厅)", house_info)
        if not area_match or not total_match or not unit_match:
            raise BeikeBrowserError(
                "BEIKE_DOM_CONTRACT_DRIFT",
                "贝壳挂牌卡片价格或面积格式发生漂移",
                {"card_index": index},
            )
        listings.append(
            {
                "title": title,
                "area_sqm": float(area_match.group(1)),
                "layout": layout_match.group(1) if layout_match else None,
                "house_info": house_info,
                "total_price_wan": float(total_match.group(1)),
                "unit_price_yuan": int(unit_match.group(1).replace(",", "")),
                "source_url": href,
            }
        )
    return listings, excluded


def listing_statistics(listings: list[dict[str, Any]]) -> dict[str, Any]:
    if not listings:
        raise BeikeBrowserError(
            "BEIKE_PRIMARY_LIST_MISSING",
            "贝壳第一主列表没有有效挂牌",
        )
    prices = [int(item["unit_price_yuan"]) for item in listings]
    return {
        "valid_count": len(listings),
        "minimum_unit_price_yuan": min(prices),
        "maximum_unit_price_yuan": max(prices),
        "median_unit_price_yuan": float(median(prices)),
        "scope": "returned_listings",
    }


class BeikeBrowserClient:
    """Jiangmen Beike provider that attaches to an existing local page."""

    def __init__(self, cdp_url: str | None = None, timeout_ms: int = 20_000) -> None:
        self._cdp_url = _validated_cdp_url(cdp_url)
        self._timeout_ms = timeout_ms
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _connect_unlocked(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._cdp_url, timeout=self._timeout_ms
            )
            return self._browser
        except (PlaywrightError, OSError) as exc:
            self._browser = None
            raise BeikeBrowserError(
                "BEIKE_BROWSER_DISCONNECTED",
                "无法连接受控本机贝壳 Chrome CDP 会话",
                {"exception_type": type(exc).__name__},
            ) from exc

    async def _page_unlocked(self, host: str) -> Any:
        browser = await self._connect_unlocked()
        pages = [page for context in browser.contexts for page in context.pages]
        for page in pages:
            if urlparse(page.url).hostname == host:
                return page
        raise BeikeBrowserError(
            "BEIKE_BROWSER_DISCONNECTED",
            "CDP 会话中没有现有江门贝壳页面",
            {"required_host": host, "page_count": len(pages)},
        )

    async def _checked_page_state(self, page: Any, host: str) -> tuple[str, str]:
        title = await page.title()
        _action_required(page.url, title, host)
        return page.url, title

    async def _navigate(self, page: Any, url: str, host: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            await self._checked_page_state(page, host)
        except PlaywrightTimeoutError as exc:
            raise BeikeBrowserError(
                "BEIKE_NAVIGATION_TIMEOUT",
                "贝壳页面在限定时间内未完成导航",
                {"path": urlparse(url).path},
            ) from exc
        except PlaywrightError as exc:
            raise BeikeBrowserError(
                "BEIKE_BROWSER_DISCONNECTED",
                "贝壳页面导航期间 CDP 连接中断",
                {"exception_type": type(exc).__name__},
            ) from exc

    async def status(self) -> dict[str, Any]:
        try:
            async with self._lock:
                host = CITY_HOSTS["江门市"]
                page = await self._page_unlocked(host)
                url, title = await self._checked_page_state(page, host)
                return {
                    "status": "OK",
                    "connected": True,
                    "city": "江门市",
                    "page_host": urlparse(url).hostname,
                    "page_path": urlparse(url).path,
                    "page_title": title,
                    "browser_mode": "attach_only_existing_page",
                    "credential_storage_accessed": False,
                }
        except BeikeBrowserError as exc:
            return exc.as_result(connected=False)
        except PlaywrightTimeoutError as exc:
            return BeikeBrowserError(
                "BEIKE_NAVIGATION_TIMEOUT",
                "贝壳页面状态检查超时",
                {"exception_type": type(exc).__name__},
            ).as_result(connected=False)
        except PlaywrightError as exc:
            return BeikeBrowserError(
                "BEIKE_BROWSER_DISCONNECTED",
                "贝壳页面状态检查期间 CDP 连接中断",
                {"exception_type": type(exc).__name__},
            ).as_result(connected=False)

    async def search_xiaoqu(self, city: str, keyword: str) -> dict[str, Any]:
        try:
            host = _city_host(city)
            query = _validated_keyword(keyword)
            async with self._lock:
                page = await self._page_unlocked(host)
                await self._navigate(page, f"https://{host}/xiaoqu/", host)
                search = page.locator("#searchInput")
                if await search.count() != 1:
                    raise BeikeBrowserError(
                        "BEIKE_DOM_CONTRACT_DRIFT",
                        "贝壳小区搜索控件 #searchInput 不再唯一",
                    )
                await search.click()
                await search.press("Control+A")
                await search.press("Backspace")
                await search.press_sequentially(query, delay=60)
                suggestions = page.locator("a[href*='?sug=']")
                for _ in range(12):
                    await self._checked_page_state(page, host)
                    if await suggestions.count():
                        break
                    await page.wait_for_timeout(250)
                raw = await suggestions.evaluate_all(
                    """els => els.map(el => ({
                        href: el.getAttribute('href') || '',
                        kind: (el.querySelector('.sug_region')?.innerText || '').trim(),
                        name: (el.querySelector('.historyKey')?.innerText || '').trim(),
                        region: (el.querySelector('.sub-text')?.innerText || '').trim()
                    }))"""
                )
                candidates = normalize_suggestions(raw, host)
                if not candidates:
                    raise BeikeBrowserError(
                        "BEIKE_NO_MATCH",
                        "贝壳自动补全没有返回标准小区候选",
                        {"city": "江门市", "keyword": query},
                    )
                return {
                    "status": "OK",
                    "city": "江门市",
                    "keyword": query,
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                    "match_decision": "CALLER_REQUIRED",
                }
        except BeikeBrowserError as exc:
            return exc.as_result(candidates=[], candidate_count=0)
        except PlaywrightTimeoutError as exc:
            return BeikeBrowserError(
                "BEIKE_NAVIGATION_TIMEOUT",
                "贝壳小区搜索交互超时",
                {"exception_type": type(exc).__name__},
            ).as_result(candidates=[], candidate_count=0)
        except PlaywrightError as exc:
            return BeikeBrowserError(
                "BEIKE_BROWSER_DISCONNECTED",
                "贝壳小区搜索期间 CDP 连接中断",
                {"exception_type": type(exc).__name__},
            ).as_result(candidates=[], candidate_count=0)

    async def get_xiaoqu_market(
        self, city: str, xiaoqu_id: str, limit: int = 30
    ) -> dict[str, Any]:
        try:
            host = _city_host(city)
            community_id = _validated_xiaoqu_id(xiaoqu_id)
            bounded_limit = _validated_limit(limit)
            detail_url = f"https://{host}/xiaoqu/{community_id}/"
            listing_url = f"https://{host}/ershoufang/c{community_id}/"
            async with self._lock:
                page = await self._page_unlocked(host)
                await self._navigate(page, detail_url, host)
                detail_raw = await page.evaluate(
                    """() => ({
                        name: (document.querySelector('.detailHeader h1.main')?.innerText || '').trim(),
                        address: (document.querySelector('.detailHeader .sub')?.innerText || '').trim(),
                        unit_price: (document.querySelector('.xiaoquUnitPrice')?.innerText || '').trim(),
                        items: Array.from(document.querySelectorAll('.xiaoquInfoItem'))
                            .map(el => (el.innerText || '').trim())
                    })"""
                )
                detail = parse_detail_snapshot(detail_raw, community_id, detail_url)

                # The historical /xiaoqu/{id}/esf/ route now redirects to detail;
                # the canonical same-source community inventory route is current.
                await self._navigate(page, listing_url, host)
                if urlparse(page.url).path.rstrip("/") != urlparse(listing_url).path.rstrip("/"):
                    raise BeikeBrowserError(
                        "BEIKE_DOM_CONTRACT_DRIFT",
                        "贝壳小区挂牌页发生非预期站内重定向",
                        {"actual_path": urlparse(page.url).path},
                    )
                primary_lists = page.locator("ul.sellListContent:not(.VIEWDATA)")
                if await primary_lists.count() != 1:
                    raise BeikeBrowserError(
                        "BEIKE_PRIMARY_LIST_MISSING",
                        "贝壳第一非 VIEWDATA 主挂牌列表不存在或不唯一",
                        {"primary_list_count": await primary_lists.count()},
                    )
                primary = primary_lists.first
                raw = await primary.locator("li").evaluate_all(
                    """els => els.map(el => ({
                        class_name: el.className || '',
                        title: (el.querySelector('.title a')?.innerText || '').trim(),
                        href: el.querySelector('.title a')?.href || '',
                        house_info: (el.querySelector('.houseInfo')?.innerText || '').trim(),
                        total_price: (el.querySelector('.totalPrice')?.innerText || '').trim(),
                        unit_price: (el.querySelector('.unitPrice')?.innerText || '').trim()
                    }))"""
                )
                all_listings, excluded = parse_primary_listings(raw, host)
                if not all_listings:
                    raise BeikeBrowserError(
                        "BEIKE_PRIMARY_LIST_MISSING",
                        "贝壳第一主列表没有有效挂牌",
                    )
                listings = all_listings[:bounded_limit]
                return {
                    "status": "OK",
                    "source": "beike_browser",
                    "city": "江门市",
                    "detail": detail,
                    "listings": listings,
                    "statistics": listing_statistics(listings),
                    "inventory": {
                        "primary_list_valid_count": len(all_listings),
                        "returned_count": len(listings),
                        "excluded_viewdata_or_ad_count": excluded,
                        "primary_list_selector": "ul.sellListContent:not(.VIEWDATA)",
                        "recommendation_lists_used": False,
                    },
                    "listing_source_url": listing_url,
                    "credential_storage_accessed": False,
                }
        except BeikeBrowserError as exc:
            return exc.as_result(listings=[])
        except PlaywrightTimeoutError as exc:
            return BeikeBrowserError(
                "BEIKE_NAVIGATION_TIMEOUT",
                "贝壳小区市场读取超时",
                {"exception_type": type(exc).__name__},
            ).as_result(listings=[])
        except PlaywrightError as exc:
            return BeikeBrowserError(
                "BEIKE_BROWSER_DISCONNECTED",
                "贝壳小区市场读取期间 CDP 连接中断",
                {"exception_type": type(exc).__name__},
            ).as_result(listings=[])
