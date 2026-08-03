"""Offline regression and fail-closed contracts for the Beike provider."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from beike_browser_client import (
    BeikeBrowserClient,
    BeikeBrowserError,
    _action_required,
    _city_host,
    _safe_ke_url,
    _validated_cdp_url,
    _validated_limit,
    _validated_xiaoqu_id,
    listing_statistics,
    normalize_suggestions,
    parse_detail_snapshot,
    parse_primary_listings,
)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = json.loads(
    (ROOT / "tests" / "fixtures" / "beike_provider_snapshots.json").read_text(
        encoding="utf-8"
    )
)
ORIGINAL_TWELVE_CONTRACT_SHA256 = (
    "f17be9a4a17044b23dbab5c24cfc325de14cb58f012d114712c2a7fc148d003f"
)
ORIGINAL_TOOL_NAMES = {
    "ali_get_filter_options",
    "ali_get_supported_areas",
    "ali_pc_browser_close",
    "ali_pc_browser_start",
    "ali_pc_browser_status",
    "ali_pc_get_filter_options",
    "ali_pc_get_item_detail",
    "ali_pc_search_judicial",
    "ali_search_judicial",
    "jd_get_supported_areas",
    "jd_search_judicial",
    "search_judicial",
}


@pytest.mark.parametrize("name", ["jianghai_garden", "aoyuan_waitan"])
def test_historical_real_listing_snapshots_keep_count_and_median(name):
    sample = SNAPSHOTS[name]
    listings = [
        {"unit_price_yuan": value} for value in sample["unit_prices_yuan"]
    ]
    statistics = listing_statistics(listings)
    assert len(listings) == sample["listing_count"]
    assert statistics["median_unit_price_yuan"] == sample["median_unit_price_yuan"]
    assert statistics["minimum_unit_price_yuan"] == min(sample["unit_prices_yuan"])
    assert statistics["maximum_unit_price_yuan"] == max(sample["unit_prices_yuan"])


def test_historical_wufu_no_match_never_substitutes_another_community():
    sample = SNAPSHOTS["chengnan_wufu"]
    assert normalize_suggestions([], "jiangmen.ke.com") == []
    assert sample == {
        "keyword": "城南五福村",
        "candidate_count": 0,
        "replacement_allowed": False,
        "expected_status": "NO_MATCH",
    }


def test_autocomplete_extracts_only_community_candidates_and_exact_ids():
    candidates = normalize_suggestions(
        [
            {
                "href": "/xiaoqu/c8895132280985217/?sug=1",
                "kind": "小区",
                "name": "江海花园",
                "region": "(江海花園) 河南 江海区",
            },
            {
                "href": "/ershoufang/rs江海花园/",
                "kind": "搜索词",
                "name": "江海花园",
                "region": "江门",
            },
        ],
        "jiangmen.ke.com",
    )
    assert candidates == [
        {
            "xiaoqu_id": "8895132280985217",
            "name": "江海花园",
            "region": "(江海花園) 河南 江海区",
            "source_url": "https://jiangmen.ke.com/xiaoqu/c8895132280985217/?sug=1",
        }
    ]


def test_primary_list_excludes_viewdata_ad_and_rejects_partial_real_card():
    raw = [
        {
            "class_name": "list_goodhouse_daoliu VIEWDATA",
            "title": "猜你喜欢",
        },
        {
            "class_name": "clear",
            "title": "户型方正",
            "href": "https://jiangmen.ke.com/ershoufang/105120921147.html",
            "house_info": "高楼层 (共12层) 5室2厅 | 157平米 | 南",
            "total_price": "100\n万",
            "unit_price": "6,370元/平",
        },
    ]
    listings, excluded = parse_primary_listings(raw, "jiangmen.ke.com")
    assert excluded == 1
    assert listings == [
        {
            "title": "户型方正",
            "area_sqm": 157.0,
            "layout": "5室2厅",
            "house_info": "高楼层 (共12层) 5室2厅 | 157平米 | 南",
            "total_price_wan": 100.0,
            "unit_price_yuan": 6370,
            "source_url": "https://jiangmen.ke.com/ershoufang/105120921147.html",
        }
    ]

    with pytest.raises(BeikeBrowserError) as caught:
        parse_primary_listings([{"class_name": "clear", "title": "缺字段"}], "jiangmen.ke.com")
    assert caught.value.code == "BEIKE_DOM_CONTRACT_DRIFT"


def test_detail_snapshot_requires_real_name_price_and_two_part_fields():
    detail = parse_detail_snapshot(
        {
            "name": "江海花园",
            "address": "(江海区) 麻园路151号",
            "unit_price": "6251",
            "items": ["建筑类型\n塔楼/板楼", "房屋总数\n734户"],
        },
        "8895132280985217",
        "https://jiangmen.ke.com/xiaoqu/8895132280985217/",
    )
    assert detail["listing_average_unit_price_yuan"] == 6251
    assert detail["facts"]["房屋总数"] == "734户"

    with pytest.raises(BeikeBrowserError) as caught:
        parse_detail_snapshot(
            {"name": "江海花园", "unit_price": "6251", "items": ["损坏字段"]},
            "8895132280985217",
            "https://jiangmen.ke.com/xiaoqu/8895132280985217/",
        )
    assert caught.value.code == "BEIKE_DOM_CONTRACT_DRIFT"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: _city_host("广州市"), "BEIKE_UNSUPPORTED_CITY"),
        (lambda: _validated_xiaoqu_id("../../1"), "BEIKE_INVALID_XIAOQU_ID"),
        (lambda: _validated_limit(31), "BEIKE_DOM_CONTRACT_DRIFT"),
        (lambda: _validated_cdp_url("http://192.0.2.1:9222"), "BEIKE_BROWSER_DISCONNECTED"),
    ],
)
def test_inputs_and_cdp_configuration_fail_closed(call, code):
    with pytest.raises(BeikeBrowserError) as caught:
        call()
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("url", "title", "code"),
    [
        ("https://hip.ke.com/", "CAPTCHA", "BEIKE_CAPTCHA_REQUIRED"),
        ("https://passport.ke.com/login", "登录贝壳", "BEIKE_LOGIN_REQUIRED"),
    ],
)
def test_captcha_and_login_are_structured_stop_conditions(url, title, code):
    with pytest.raises(BeikeBrowserError) as caught:
        _action_required(url, title, "jiangmen.ke.com")
    assert caught.value.as_result()["error"]["code"] == code


def test_non_ke_redirect_is_rejected_without_exposing_full_url():
    with pytest.raises(BeikeBrowserError) as caught:
        _safe_ke_url("https://example.com/path?secret=value", "jiangmen.ke.com")
    result = caught.value.as_result()
    assert result["error"]["code"] == "BEIKE_DOM_CONTRACT_DRIFT"
    assert "secret" not in json.dumps(result)


def test_navigation_timeout_has_public_error_code():
    class Page:
        async def goto(self, *args, **kwargs):
            raise PlaywrightTimeoutError("bounded timeout")

    client = BeikeBrowserClient(timeout_ms=1)
    with pytest.raises(BeikeBrowserError) as caught:
        asyncio.run(
            client._navigate(
                Page(),
                "https://jiangmen.ke.com/xiaoqu/",
                "jiangmen.ke.com",
            )
        )
    assert caught.value.code == "BEIKE_NAVIGATION_TIMEOUT"


def test_browser_disconnect_is_returned_as_structured_status(monkeypatch):
    client = BeikeBrowserClient()

    async def disconnected():
        raise BeikeBrowserError("BEIKE_BROWSER_DISCONNECTED", "offline")

    monkeypatch.setattr(client, "_connect_unlocked", disconnected)
    result = asyncio.run(client.status())
    assert result["connected"] is False
    assert result["error"]["code"] == "BEIKE_BROWSER_DISCONNECTED"


def test_playwright_timeout_is_returned_as_structured_status(monkeypatch):
    client = BeikeBrowserClient()

    async def timed_out(host):
        raise PlaywrightTimeoutError("bounded timeout")

    monkeypatch.setattr(client, "_page_unlocked", timed_out)
    result = asyncio.run(client.status())
    assert result["connected"] is False
    assert result["error"]["code"] == "BEIKE_NAVIGATION_TIMEOUT"


def test_provider_source_never_calls_browser_or_credential_storage_apis():
    source = (ROOT / "beike_browser_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {
            "launch",
            "launch_persistent_context",
            "new_context",
            "new_page",
            "cookies",
            "storage_state",
            "close",
        }
    )


def test_original_twelve_tool_schemas_are_byte_semantically_unchanged():
    contract = json.loads(
        (ROOT / "auction_mcp_assets" / "mcp_contract.json").read_text(encoding="utf-8")
    )["tools"]
    original = {name: contract[name] for name in ORIGINAL_TOOL_NAMES}
    payload = json.dumps(
        original, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == ORIGINAL_TWELVE_CONTRACT_SHA256
    assert set(contract) == ORIGINAL_TOOL_NAMES | {
        "beike_browser_status",
        "beike_search_xiaoqu",
        "beike_get_xiaoqu_market",
    }
