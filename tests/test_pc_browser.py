"""Ali PC 浏览器适配器离线契约测试."""
from __future__ import annotations

import asyncio
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

SYNTHETIC_TEST_PHONE = "138" + "0000" + "0000"

from ali_pc_browser_client import (
    _normalize_pc_detail_snapshot,
    _pagination_current_page,
    _pagination_next_candidates,
    _pc_detail_url_matches,
    _redact_url,
    _validate_pc_item_id,
    _validate_pc_page,
    AliPCBrowserClient,
    AliPCBrowserError,
    build_pc_detail_url,
    build_pc_search_url,
    evaluate_pc_live_acceptance,
    evaluate_pc_matrix_scenario,
    parse_pc_item_records,
)


def _ready_detail_snapshot() -> dict:
    return {
        "title": "江门市蓬江区泰和广场11号之三402室",
        "bodyText": (
            "一拍 即将开始 2026-08-06 10:00开拍 0人报名 "
            "27人设置提醒 940次围观 江门市蓬江区人民法院 "
            f"联系方式：陈生 手机：{SYNTHETIC_TEST_PHONE}"
        ),
        "labelBlocks": {
            "当前价": None,
            "变卖价": None,
            "起拍价": "起拍价：¥330,176元",
            "评估价": "评估价：¥471,680元",
            "保证金": "保证金：¥33,018元",
            "加价幅度": "加价幅度：¥3,000元",
            "延时周期": "延时周期：5分钟",
            "竞价周期": "竞价周期：1天",
            "联系方式": f"联系方式：陈生 手机：{SYNTHETIC_TEST_PHONE}",
            "标的公告": "标的公告：查看公告",
        },
        "detailPresent": True,
        "detailText": (
            "标的物介绍 相关附件下载 标的物位置 "
            "广东省 江门市 蓬江区泰和广场11号之三402室。"
        ),
        "detailLoading": False,
        "attachmentPresent": True,
        "attachmentText": "相关附件下载：竞买公告",
        "attachmentLoading": False,
        "attachments": [{
            "text": "竞买公告",
            "url": "https://sf-item.taobao.com/notice.pdf?x5secdata=secret",
        }],
        "announcementUrl": "https://sf-item.taobao.com/notice.htm?x5secdata=secret",
        "images": [
            "https://img.alicdn.com/a.jpg?x5secdata=secret",
            "https://img.alicdn.com/a.jpg?x5secdata=secret",
        ],
    }


@pytest.mark.parametrize("value", [True, "abc", "1234567", "1" * 21, "106/../../"])
def test_pc_detail_item_id_rejects_invalid_values(value):
    with pytest.raises(AliPCBrowserError) as exc_info:
        _validate_pc_item_id(value)

    assert exc_info.value.code == "pc_detail_validation_failed"


def test_pc_detail_url_is_fixed_to_verified_host_and_item_path():
    item_id = "1062507630078"
    assert _validate_pc_item_id(item_id) == item_id
    assert build_pc_detail_url(item_id) == (
        "https://sf-item.taobao.com/sf_item/1062507630078.htm"
    )
    assert _pc_detail_url_matches(
        f"https://sf-item.taobao.com/sf_item/{item_id}.htm?track_id=public",
        item_id,
    )
    for url in (
        f"http://sf-item.taobao.com/sf_item/{item_id}.htm",
        f"https://example.com/sf_item/{item_id}.htm",
        f"https://sf-item.taobao.com:443/sf_item/{item_id}.htm",
        f"https://sf-item.taobao.com:invalid/sf_item/{item_id}.htm",
        "https://sf-item.taobao.com/sf_item/1062507630079.htm",
    ):
        assert not _pc_detail_url_matches(url, item_id)


def test_pc_detail_snapshot_normalizes_verified_schema_and_redacts_risk_tokens():
    item_id = "1062507630078"
    detail = _normalize_pc_detail_snapshot(
        _ready_detail_snapshot(),
        item_id=item_id,
        url=build_pc_detail_url(item_id),
        page_title="fallback title",
    )

    assert detail["source"] == "ali_pc_browser"
    assert detail["itemId"] == item_id
    assert detail["title"] == "江门市蓬江区泰和广场11号之三402室"
    assert detail["status"] == "即将开始"
    assert detail["stage"] == "一拍"
    assert detail["auctionStartAt"] == "2026-08-06 10:00"
    assert detail["currentPriceYuan"] is None
    assert detail["startingPriceYuan"] == 330_176
    assert detail["appraisalPriceYuan"] == 471_680
    assert detail["depositYuan"] == 33_018
    assert detail["incrementYuan"] == 3_000
    assert detail["registrationCount"] == 0
    assert detail["reminderCount"] == 27
    assert detail["viewCount"] == 940
    assert detail["court"] == "江门市蓬江区人民法院"
    assert detail["contact"] == {"name": "陈生", "phone": SYNTHETIC_TEST_PHONE}
    assert detail["location"].endswith("蓬江区泰和广场11号之三402室")
    assert "secret" not in detail["announcementUrl"]
    assert "secret" not in detail["attachments"][0]["url"]
    assert detail["images"] == ["https://img.alicdn.com/a.jpg?x5secdata=REDACTED"]


@pytest.mark.parametrize("value", [0, 6, -1, True, 1.5, "2"])
def test_pc_page_validation_rejects_out_of_contract_values(value):
    with pytest.raises(AliPCBrowserError) as exc_info:
        _validate_pc_page(value)

    assert exc_info.value.code == "pc_filter_validation_failed"


def test_pc_page_validation_accepts_bounded_integer():
    assert _validate_pc_page(1) == 1
    assert _validate_pc_page(5) == 5


def test_pc_pagination_contract_requires_unique_same_site_expected_page_link():
    snapshot = {
        "current": [{"text": "1", "className": "current"}],
        "controls": [
            {
                "index": 1,
                "text": "",
                "className": "next",
                "href": "https://sf.taobao.com/list/example.htm?page=2",
                "visible": True,
                "disabled": False,
            },
            {
                "index": 2,
                "text": "下一页",
                "className": "next",
                "href": "https://example.com/list?page=2",
                "visible": True,
                "disabled": False,
            },
            {
                "index": 3,
                "text": "下一页",
                "className": "next",
                "href": "https://sf.taobao.com/list/example.htm?page=3",
                "visible": True,
                "disabled": False,
            },
        ],
    }

    assert _pagination_current_page(snapshot) == 1
    assert _pagination_next_candidates(snapshot, 2) == [snapshot["controls"][0]]


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
            "title": "江门住宅一套\n当前价 ￥11.3417万\n开始时间 08月14日\n8850次围观",
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
    assert items[0]["title"] == "江门住宅一套"
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


def test_pc_navigate_to_page_confirms_indicator_and_changed_item_set():
    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "司法拍卖列表 当前价"

    class NextLocator:
        def __init__(self, page):
            self.page = page

        def nth(self, index):
            assert index == 0
            return self

        async def click(self):
            self.page.current_page = 2
            self.page.url = "https://sf.taobao.com/list/example.htm?page=2"

    class FakePage:
        url = "https://sf.taobao.com/list/example.htm"
        current_page = 1

        async def evaluate(self, script):
            return {
                "current": [{"text": str(self.current_page), "className": "current"}],
                "controls": [{
                    "index": 0,
                    "tag": "a",
                    "text": "",
                    "className": "next",
                    "href": f"https://sf.taobao.com/list/example.htm?page={self.current_page + 1}",
                    "visible": True,
                    "disabled": False,
                }],
            }

        def locator(self, selector):
            return BodyLocator() if selector == "body" else NextLocator(self)

        async def wait_for_load_state(self, state, timeout):
            return None

        async def wait_for_timeout(self, milliseconds):
            return None

        async def title(self):
            return "司法拍卖列表"

    class FakeClient(AliPCBrowserClient):
        async def _wait_for_items(self, limit):
            ids = ["1", "2"] if self._page.current_page == 1 else ["2", "3", "4"]
            return ([{"itemId": item_id} for item_id in ids], {"poll_attempts": 1})

        async def _wait_for_page_items_change(self, previous_ids):
            assert previous_ids == ["1", "2"]
            return ([{"itemId": "2"}, {"itemId": "3"}, {"itemId": "4"}], {"poll_attempts": 1})

    client = FakeClient()
    client._page = FakePage()

    receipt = asyncio.run(client._navigate_to_page(2))

    assert receipt["page"] == 2
    assert receipt["pageTurns"] == 1
    assert receipt["paginationReceipts"] == [{
        "fromPage": 1,
        "toPage": 2,
        "url": "https://sf.taobao.com/list/example.htm?page=2",
        "previousCount": 2,
        "currentCount": 3,
        "overlapCount": 1,
        "newItemCount": 2,
        "control": {
            "tag": "a",
            "className": "next",
            "href": "https://sf.taobao.com/list/example.htm?page=2",
        },
    }]


def test_pc_navigate_to_page_fails_closed_when_current_indicator_is_missing():
    class FakePage:
        url = "https://sf.taobao.com/list/example.htm"

        async def evaluate(self, script):
            return {
                "current": [],
                "controls": [{
                    "index": 0,
                    "text": "下一页",
                    "className": "next",
                    "href": "https://sf.taobao.com/list/example.htm?page=2",
                    "visible": True,
                    "disabled": False,
                }],
            }

    class FakeClient(AliPCBrowserClient):
        async def _wait_for_items(self, limit):
            return ([{"itemId": "1"}], {})

    client = FakeClient()
    client._page = FakePage()

    with pytest.raises(AliPCBrowserError) as exc_info:
        asyncio.run(client._navigate_to_page(2))

    assert exc_info.value.code == "pc_pagination_resolution_failed"


def test_pc_filter_snapshot_reads_dynamic_links_selects_and_inputs():
    class FakePage:
        url = "https://sf.taobao.com/"

        async def eval_on_selector_all(self, selector, script):
            if selector == "a[href]":
                return [
                    {"text": "住宅用房", "href": "https://sf.taobao.com/list/home.htm", "visible": True},
                    {"text": "外站", "href": "https://example.com/", "visible": True},
                ]
            if selector == "select":
                return [
                    {"index": 0, "selected": "默认排序", "options": ["默认排序", "价格从低到高"]},
                    {"index": 1, "selected": "拍卖状态", "options": ["拍卖状态", "正在进行"]},
                    {"index": 2, "selected": "拍卖阶段", "options": ["拍卖阶段", "一拍"]},
                    {"index": 3, "selected": "价格区间", "options": ["价格区间", "21万以下"]},
                ]
            if selector == "input":
                return [{"index": 0, "type": "text", "name": "auctionStart", "valuePresent": False}]
            raise AssertionError(selector)

    client = AliPCBrowserClient()
    client._page = FakePage()

    snapshot = asyncio.run(client._filter_options_snapshot_unlocked())

    assert snapshot["linkOptions"] == [
        {"text": "住宅用房", "href": "https://sf.taobao.com/list/home.htm", "visible": True}
    ]
    assert set(snapshot["selectDimensions"]) == {"sort", "status", "stage", "price_range"}
    assert snapshot["selectDimensions"]["status"]["options"] == ["拍卖状态", "正在进行"]
    assert snapshot["cookie_exported"] is False
    assert snapshot["cookie_persisted_by_adapter"] is False


def test_pc_filter_options_requires_started_browser():
    result = asyncio.run(AliPCBrowserClient().get_filter_options())
    assert result["error"] == "pc_browser_not_started"


def test_pc_select_option_confirms_dynamic_page_state():
    class FakeLocator:
        def __init__(self, page):
            self.page = page

        def nth(self, index):
            assert index == 0
            return self

        async def select_option(self, label):
            self.page.selected = label

    class FakePage:
        url = "https://sf.taobao.com/list/example.htm"
        selected = "默认排序"

        async def eval_on_selector_all(self, selector, script):
            assert selector == "select"
            return [{
                "index": 0,
                "selected": self.selected,
                "options": ["默认排序", "价格从低到高"],
            }]

        def locator(self, selector):
            assert selector == "select"
            return FakeLocator(self)

        async def wait_for_load_state(self, state, timeout):
            return None

    client = AliPCBrowserClient()
    client._page = FakePage()

    trace = asyncio.run(client._select_option_exact("价格从低到高", "sort"))

    assert trace == {
        "dimension": "sort",
        "label": "价格从低到高",
        "url": "https://sf.taobao.com/list/example.htm",
    }


def test_pc_choice_falls_back_to_unique_exact_same_site_link():
    class FakePage:
        url = "https://sf.taobao.com/item_list.htm"

        async def eval_on_selector_all(self, selector, script, *args):
            if selector == "select":
                return []
            if selector == "a[href]":
                assert args == ("正在进行",)
                return ["https://sf.taobao.com/list/status.htm?status=doing"]
            raise AssertionError(selector)

        async def goto(self, url, wait_until):
            self.url = url

    client = AliPCBrowserClient()
    client._page = FakePage()

    trace = asyncio.run(client._apply_choice_exact("正在进行", "status"))

    assert trace == {
        "dimension": "status",
        "label": "正在进行",
        "url": "https://sf.taobao.com/list/status.htm?status=doing",
        "controlType": "link",
    }


def test_pc_choice_operates_bf_select_and_verifies_visible_content():
    class FakePage:
        url = "https://sf.taobao.com/item_list.htm"
        selected = "拍卖状态"
        evaluate_calls = 0

        async def eval_on_selector_all(self, selector, script):
            assert selector == "select"
            return [{
                "index": 1,
                "id": "J_AuctionStatusSort",
                "name": None,
                "selected": None,
                "customSelected": self.selected,
                "nativeOptions": [],
                "customOptions": ["拍卖状态", "正在进行", "已结束"],
                "options": ["拍卖状态", "正在进行", "已结束"],
            }]

        async def evaluate(self, script, argument):
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                assert argument == "J_AuctionStatusSort"
                return {"ok": True, "selected": self.selected}
            assert argument == {"selectId": "J_AuctionStatusSort", "wanted": "正在进行"}
            self.selected = "正在进行"
            self.url = "https://sf.taobao.com/list/status-doing.htm"
            return {"matchCount": 1}

        async def wait_for_timeout(self, milliseconds):
            return None

        async def wait_for_load_state(self, state, timeout):
            return None

    client = AliPCBrowserClient()
    client._page = FakePage()

    trace = asyncio.run(client._apply_choice_exact("正在进行", "status"))

    assert trace == {
        "dimension": "status",
        "label": "正在进行",
        "url": "https://sf.taobao.com/list/status-doing.htm",
        "controlType": "bf-select",
        "selectId": "J_AuctionStatusSort",
        "clickEvaluationInterrupted": False,
    }


def test_pc_bf_select_accepts_navigation_interruption_only_after_visible_verification():
    class FakePage:
        url = "https://sf.taobao.com/item_list.htm"
        selected = "拍卖状态"
        evaluate_calls = 0

        async def eval_on_selector_all(self, selector, script):
            return [{
                "index": 1,
                "id": "J_AuctionStatusSort",
                "selected": None,
                "customSelected": self.selected,
                "nativeOptions": [],
                "customOptions": ["拍卖状态", "正在进行"],
                "options": ["拍卖状态", "正在进行"],
            }]

        async def evaluate(self, script, argument):
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {"ok": True, "selected": self.selected}
            self.selected = "正在进行"
            self.url = "https://sf.taobao.com/list/status-doing.htm"
            raise RuntimeError("Execution context was destroyed by navigation")

        async def wait_for_timeout(self, milliseconds):
            return None

        async def wait_for_load_state(self, state, timeout):
            return None

    client = AliPCBrowserClient()
    client._page = FakePage()

    trace = asyncio.run(client._apply_choice_exact("正在进行", "status"))

    assert trace["url"] == "https://sf.taobao.com/list/status-doing.htm"
    assert trace["clickEvaluationInterrupted"] is True


def test_pc_matrix_scenario_requires_matching_application_receipt():
    result = {
        "source": "ali_pc_browser",
        "count": 1,
        "authenticated_session": True,
        "url": "https://sf.taobao.com/list/example.htm",
        "appliedFilters": [{"dimension": "status", "label": "正在进行"}],
        "items": [{"itemId": "1061234567890", "title": "住宅", "currentPriceYuan": 100_000}],
    }

    accepted = evaluate_pc_matrix_scenario("status", result, {"status": "正在进行"})
    rejected = evaluate_pc_matrix_scenario("status", result, {"status": "已结束"})

    assert accepted["accepted"] is True
    assert rejected["accepted"] is False
    assert rejected["failures"] == ["applied_filter_mismatch"]


def test_pc_risk_control_url_redacts_x5secdata_in_reports():
    url = (
        "https://sf.taobao.com/_____tmd_____/punish"
        "?x5secdata=temporary-secret&x5step=1"
    )
    redacted = _redact_url(url)

    assert "temporary-secret" not in redacted
    assert "x5secdata=REDACTED" in redacted
    assert "x5step=1" in redacted


def test_pc_search_requires_started_browser():
    client = AliPCBrowserClient()
    result = asyncio.run(client.search(keyword="住宅"))
    assert result["error"] == "pc_browser_not_started"


def test_pc_detail_requires_started_browser():
    client = AliPCBrowserClient()
    result = asyncio.run(client.get_item_detail("1062507630078"))
    assert result["error"] == "pc_browser_not_started"


def test_pc_detail_waits_until_body_and_attachments_finish_loading():
    loading = _ready_detail_snapshot()
    loading.update({
        "detailText": "标的物详情加载中......",
        "detailLoading": True,
        "attachmentText": "附件加载中......",
        "attachmentLoading": True,
    })
    ready = _ready_detail_snapshot()

    class FakePage:
        url = "https://sf-item.taobao.com/sf_item/1062507630078.htm"

        def __init__(self):
            self.snapshots = [loading, ready]
            self.waits = []

        async def evaluate(self, script):
            assert "J_ItemDetailContent" in script
            return self.snapshots.pop(0)

        async def title(self):
            return "江门住宅详情"

        async def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    client = AliPCBrowserClient(timeout_ms=1_000)
    client._page = FakePage()

    snapshot, diagnostics = asyncio.run(
        client._wait_for_detail_snapshot("1062507630078")
    )

    assert snapshot["detailText"].startswith("标的物介绍")
    assert diagnostics == {
        "poll_attempts": 2,
        "price_ready": True,
        "detail_ready": True,
        "attachment_ready": True,
        "url_ready": True,
    }
    assert len(client._page.waits) == 1


def test_pc_detail_query_returns_structured_ready_snapshot_without_cookie_state():
    item_id = "1062507630078"

    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "司法拍卖详情 即将开始"

    class FakePage:
        url = "https://sf.taobao.com/"

        def is_closed(self):
            return False

        async def goto(self, url, wait_until=None):
            assert wait_until == "domcontentloaded"
            self.url = url

        async def title(self):
            return "江门住宅详情"

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator()

    class FakeClient(AliPCBrowserClient):
        async def _status_unlocked(self):
            return {"state": "ready", "authenticated": True}

        async def _wait_for_detail_snapshot(self, requested_item_id):
            assert requested_item_id == item_id
            return _ready_detail_snapshot(), {
                "poll_attempts": 2,
                "price_ready": True,
                "detail_ready": True,
                "attachment_ready": True,
                "url_ready": True,
            }

    client = FakeClient()
    client._page = FakePage()

    result = asyncio.run(client.get_item_detail(item_id))

    assert result["itemId"] == item_id
    assert result["startingPriceYuan"] == 330_176
    assert result["authenticated_session"] is True
    assert result["cookie_exported"] is False
    assert result["cookie_persisted_by_adapter"] is False


def test_pc_detail_query_fails_closed_on_final_url_mismatch():
    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "司法拍卖详情"

    class FakePage:
        url = "https://sf.taobao.com/"

        def is_closed(self):
            return False

        async def goto(self, url, wait_until=None):
            self.url = "https://example.com/sf_item/1062507630078.htm"

        async def title(self):
            return "错误目标"

        def locator(self, selector):
            return BodyLocator()

    class FakeClient(AliPCBrowserClient):
        async def _status_unlocked(self):
            return {"state": "ready", "authenticated": True}

    client = FakeClient()
    client._page = FakePage()
    result = asyncio.run(client.get_item_detail("1062507630078"))

    assert result["error"] == "pc_detail_target_mismatch"


def test_pc_detail_query_fails_closed_while_async_content_is_incomplete():
    item_id = "1062507630078"

    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "司法拍卖详情"

    class FakePage:
        url = build_pc_detail_url(item_id)

        def is_closed(self):
            return False

        async def goto(self, url, wait_until=None):
            self.url = url

        async def title(self):
            return "江门住宅详情"

        def locator(self, selector):
            return BodyLocator()

    class FakeClient(AliPCBrowserClient):
        async def _status_unlocked(self):
            return {"state": "ready", "authenticated": True}

        async def _wait_for_detail_snapshot(self, requested_item_id):
            return _ready_detail_snapshot(), {
                "poll_attempts": 3,
                "price_ready": True,
                "detail_ready": False,
                "attachment_ready": False,
                "url_ready": True,
            }

    client = FakeClient()
    client._page = FakePage()
    result = asyncio.run(client.get_item_detail(item_id))

    assert result["error"] == "pc_detail_content_not_ready"
    assert result["diagnostics"]["detail_ready"] is False


def test_pc_search_wires_page_to_verified_pagination_navigation():
    class BodyLocator:
        async def inner_text(self, timeout=None):
            return "司法拍卖列表 当前价"

    class FakePage:
        url = "https://sf.taobao.com/"

        def is_closed(self):
            return False

        async def goto(self, url, wait_until=None):
            self.url = url

        async def title(self):
            return "司法拍卖列表"

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator()

    class FakeClient(AliPCBrowserClient):
        requested_page = None

        async def _status_unlocked(self):
            return {"state": "ready", "authenticated": True}

        async def _navigate_to_page(self, target_page):
            self.requested_page = target_page
            self._page.url = "https://sf.taobao.com/list/example.htm?page=2"
            return {
                "page": target_page,
                "pageTurns": 1,
                "paginationReceipts": [{"fromPage": 1, "toPage": 2}],
            }

        async def _wait_for_items(self, limit):
            return ([{
                "itemId": "2",
                "title": "第二页住宅",
                "currentPriceYuan": 100_000,
                "url": "https://sf-item.taobao.com/sf_item/2.htm",
            }], {"poll_attempts": 1})

    client = FakeClient()
    client._page = FakePage()

    result = asyncio.run(client.search(page=2, limit=10))

    assert client.requested_page == 2
    assert result["page"] == 2
    assert result["pageTurns"] == 1
    assert result["count"] == 1
    assert result["paginationReceipts"] == [{"fromPage": 1, "toPage": 2}]


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
