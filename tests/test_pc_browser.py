"""Ali PC 浏览器适配器离线契约测试."""
from __future__ import annotations

import asyncio
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from ali_pc_browser_client import (
    AliPCBrowserClient,
    AliPCBrowserError,
    build_pc_search_url,
    evaluate_pc_live_acceptance,
    parse_pc_item_records,
)


def test_pc_keyword_url_uses_verified_utf8_protocol():
    url = build_pc_search_url(keyword="商业用房")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert parsed.path == "/item_list.htm"
    assert query == {
        "_input_charset": ["utf-8"],
        "q": ["商业用房"],
        "keywordSource": ["4"],
    }


def test_pc_keyword_fails_closed_when_combined_with_filters():
    with pytest.raises(AliPCBrowserError) as exc_info:
        build_pc_search_url(keyword="住宅", max_price_yuan=210000)
    assert exc_info.value.code == "pc_keyword_filter_conflict"

    with pytest.raises(AliPCBrowserError) as scoped_exc:
        build_pc_search_url(
            "https://sf.taobao.com/list/50025969_____city.htm",
            keyword="住宅",
        )
    assert scoped_exc.value.code == "pc_keyword_filter_conflict"


def test_pc_url_builder_blocks_external_navigation():
    with pytest.raises(AliPCBrowserError) as exc_info:
        build_pc_search_url("https://example.com/list.htm", max_price_yuan=210000)
    assert exc_info.value.code == "pc_navigation_blocked"


def test_pc_price_and_date_url_matches_verified_contract():
    base = (
        "https://sf.taobao.com/list/50025969_____city.htm"
        "?auction_source=0&startPrice=&endPrice=210000&st_param=-1"
    )
    url = build_pc_search_url(
        base,
        min_price_yuan=100000,
        max_price_yuan=210000,
        auction_start_from="2026-08-01",
        auction_start_to="2026-09-01",
    )
    query = parse_qs(urlsplit(url).query)

    assert "startPrice" not in query
    assert "endPrice" not in query
    assert query["start_price"] == ["100000"]
    assert query["end_price"] == ["210000"]
    assert query["auctionStartFrom"] == ["2026-08-01"]
    assert query["auctionStartTo"] == ["2026-09-01"]
    assert query["auction_source"] == ["0"]
    assert query["st_param"] == ["-1"]


def test_pc_live_acceptance_passes_only_verified_query_and_prices():
    result = {
        "source": "ali_pc_browser",
        "count": 2,
        "authenticated_session": True,
        "url": (
            "https://sf.taobao.com/list/example.htm?end_price=210000"
            "&auctionStartFrom=2026-08-01&auctionStartTo=2026-09-01"
        ),
        "items": [
            {
                "itemId": "12345678901",
                "title": "住宅一",
                "currentPriceYuan": 100_360,
                "url": "https://sf.taobao.com/item.htm?id=12345678901",
                "rawText": "不应进入验收报告",
            },
            {
                "itemId": "12345678902",
                "title": "住宅二",
                "currentPriceYuan": 179_030,
                "url": "https://sf.taobao.com/item.htm?id=12345678902",
            },
        ],
    }

    report = evaluate_pc_live_acceptance(
        result,
        max_price_yuan=210_000,
        auction_start_from="2026-08-01",
        auction_start_to="2026-09-01",
    )

    assert report["accepted"] is True
    assert report["failures"] == []
    assert report["evidence"]["maximum_price_yuan"] == 179_030
    assert "rawText" not in report["evidence"]["samples"][0]


def test_pc_live_acceptance_rejects_over_limit_or_missing_price():
    result = {
        "source": "ali_pc_browser",
        "count": 2,
        "authenticated_session": True,
        "url": (
            "https://sf.taobao.com/list/example.htm?end_price=210000"
            "&auctionStartFrom=2026-08-01&auctionStartTo=2026-09-01"
        ),
        "items": [
            {"itemId": "12345678901", "currentPriceYuan": 210_001},
            {"itemId": "12345678902", "currentPriceYuan": None},
        ],
    }

    report = evaluate_pc_live_acceptance(
        result,
        max_price_yuan=210_000,
        auction_start_from="2026-08-01",
        auction_start_to="2026-09-01",
    )

    assert report["accepted"] is False
    assert "items_over_max_price" in report["failures"]
    assert "items_missing_current_price" in report["failures"]


def test_pc_live_acceptance_uses_error_diagnostic_url_without_false_param_failure():
    report = evaluate_pc_live_acceptance(
        {
            "error": "pc_result_parse_failed",
            "items": [],
            "diagnostics": {
                "url": (
                    "https://sf.taobao.com/list/example.htm?end_price=210000"
                    "&auctionStartFrom=2026-08-01&auctionStartTo=2026-09-01"
                ),
                "poll_attempts": 60,
            },
        },
        max_price_yuan=210_000,
        auction_start_from="2026-08-01",
        auction_start_to="2026-09-01",
    )

    assert report["accepted"] is False
    assert "query_error:pc_result_parse_failed" in report["failures"]
    assert "verified_query_params_missing" not in report["failures"]
    assert report["evidence"]["query_diagnostics"]["poll_attempts"] == 60


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_price_yuan": -1}, "非负金额"),
        ({"min_price_yuan": 10.5}, "整数金额"),
        ({"min_price_yuan": 300, "max_price_yuan": 200}, "不得大于"),
        ({"auction_start_from": "2026/08/01", "auction_start_to": "2026-09-01"}, "YYYY-MM-DD"),
        ({"auction_start_from": "2026-08-01"}, "必须同时提供"),
        ({"auction_start_from": "2026-09-01", "auction_start_to": "2026-08-01"}, "不得晚于"),
    ],
)
def test_pc_filter_validation(kwargs, message):
    with pytest.raises(AliPCBrowserError) as exc_info:
        build_pc_search_url(**kwargs)
    assert exc_info.value.code == "pc_filter_validation_failed"
    assert message in exc_info.value.message


def test_pc_item_parser_deduplicates_and_normalizes_price_units():
    records = [
        {
            "href": "https://sf-item.taobao.com/sf_item/1061234567890.htm",
            "title": "江门住宅一套",
            "text": "江门住宅一套\n当前价 ￥11.3417万\n开始时间 08月14日",
            "image": "https://img.example/one.jpg",
        },
        {
            "href": "https://sf-item.taobao.com/sf_item/1061234567890.htm",
            "title": "重复链接",
            "text": "当前价 ￥11.3417万",
        },
        {
            "href": "https://sf-item.taobao.com/sf_item/1069876543210.htm",
            "title": "大型资产",
            "text": "大型资产\n变卖价 1.25亿\n评估价 2亿",
        },
        {
            "href": "https://sf.taobao.com/item_list.htm?category=50025969",
            "title": "不是拍品",
            "text": "住宅用房筛选",
        },
    ]

    items = parse_pc_item_records(records)

    assert [item["itemId"] for item in items] == ["1061234567890", "1069876543210"]
    assert items[0]["currentPriceYuan"] == pytest.approx(113417)
    assert items[1]["currentPriceYuan"] == pytest.approx(125_000_000)


def test_pc_wait_for_items_polls_until_dynamic_cards_render():
    class FakePage:
        def __init__(self):
            self.calls = 0
            self.waits: list[int] = []

        async def eval_on_selector_all(self, selector, script):
            self.calls += 1
            if self.calls == 1:
                return []
            return [{
                "href": "https://sf-item.taobao.com/sf_item/1061234567890.htm",
                "title": "动态住宅",
                "text": "动态住宅\n开拍价 ￥10.036万\n开始时间 08月16日",
            }]

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    client = AliPCBrowserClient(timeout_ms=1_000)
    client._page = FakePage()

    items, diagnostics = asyncio.run(client._wait_for_items(limit=20))

    assert items[0]["currentPriceYuan"] == pytest.approx(100_360)
    assert diagnostics == {"poll_attempts": 2, "candidate_record_count": 1}
    assert client._page.waits == [500]


def test_pc_search_requires_started_browser():
    client = AliPCBrowserClient()
    result = asyncio.run(client.search(keyword="住宅"))
    assert result["error"] == "pc_browser_not_started"


def test_pc_search_requires_authenticated_session():
    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector
            self.first = self

        async def inner_text(self, timeout=None):
            return "亲，请登录" if self.selector == "#J_SiteNavLogin" else "司法拍卖首页"

        async def count(self):
            return 1

    class FakePage:
        url = "https://sf.taobao.com/"

        def is_closed(self):
            return False

        async def title(self):
            return "司法拍卖"

        def locator(self, selector):
            return FakeLocator(selector)

    client = AliPCBrowserClient()
    client._page = FakePage()

    result = asyncio.run(client.search(keyword="住宅"))

    assert result["state"] == "login_required"
    assert result["authenticated"] is False


def test_pc_dynamic_link_resolution_fails_closed_when_ambiguous():
    class FakePage:
        url = "https://sf.taobao.com/"

        async def eval_on_selector_all(self, selector, script, label):
            return [
                "https://sf.taobao.com/list/one.htm",
                "https://sf.taobao.com/list/two.htm",
            ]

        async def goto(self, url, wait_until=None):
            raise AssertionError("歧义链接不得导航")

    client = AliPCBrowserClient()
    client._page = FakePage()

    with pytest.raises(AliPCBrowserError) as exc_info:
        asyncio.run(client._navigate_exact_link("住宅用房", "category"))

    assert exc_info.value.code == "pc_filter_resolution_failed"
    assert exc_info.value.diagnostics["match_count"] == 2


def test_pc_browser_start_uses_non_persistent_context(monkeypatch):
    captured: dict = {}

    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector
            self.first = self

        async def inner_text(self, timeout=None):
            return "已登录用户" if self.selector == "#J_SiteNavLogin" else "司法拍卖首页"

        async def count(self):
            return 1

    class FakePage:
        url = "https://sf.taobao.com/"

        def is_closed(self):
            return False

        def set_default_timeout(self, value):
            captured["timeout"] = value

        async def goto(self, url, wait_until=None):
            captured["goto"] = (url, wait_until)

        async def title(self):
            return "司法拍卖"

        def locator(self, selector):
            return FakeLocator(selector)

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def close(self):
            captured["context_closed"] = True

    class FakeBrowser:
        async def new_context(self, **kwargs):
            captured["context_kwargs"] = kwargs
            return FakeContext()

        async def close(self):
            captured["browser_closed"] = True

    class FakeChromium:
        async def launch(self, **kwargs):
            captured["launch_kwargs"] = kwargs
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            captured["playwright_stopped"] = True

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = lambda: FakeStarter()
    fake_package = types.ModuleType("playwright")
    fake_package.async_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

    client = AliPCBrowserClient()
    result = asyncio.run(client.start())

    assert result["state"] == "ready"
    assert result["authenticated"] is True
    assert result["cookie_exported"] is False
    assert result["cookie_persisted_by_adapter"] is False
    assert captured["launch_kwargs"] == {"channel": "chrome", "headless": False}
    assert captured["context_kwargs"] == {"locale": "zh-CN"}
    assert "user_data_dir" not in captured["context_kwargs"]
    assert "storage_state" not in captured["context_kwargs"]

    close_result = asyncio.run(client.close())
    assert close_result["state"] == "stopped"
    assert captured["context_closed"] is True
    assert captured["browser_closed"] is True
    assert captured["playwright_stopped"] is True
