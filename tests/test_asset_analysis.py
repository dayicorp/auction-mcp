"""Offline contracts for the one-call auction asset analysis orchestrator."""
from __future__ import annotations

import asyncio

import pytest

import asset_analysis as analysis
import server


ITEM_ID = "1065987562217"
ITEM_URL = f"https://sf-item.taobao.com/sf_item/{ITEM_ID}.htm"
NOTICE_URL = (
    "https://sf.taobao.com/notice_detail/17937539.htm?item_id=" + ITEM_ID
)


def _detail(**overrides):
    value = {
        "source": "ali_pc_browser",
        "itemId": ITEM_ID,
        "url": ITEM_URL,
        "title": "二拍 江门市蓬江区东华二路2号之二1702室",
        "status": "即将开始",
        "stage": "二拍",
        "auctionStartAt": "2026-08-04 10:00",
        "startingPriceYuan": 296800,
        "appraisalPriceYuan": 529940.54,
        "depositYuan": 30000,
        "incrementYuan": 2000,
        "announcementUrl": NOTICE_URL,
        "location": "广东省 江门市 蓬江区东华二路2号之二1702室",
        "detailText": (
            "房屋用途 成套住宅 钥 匙 无 权利限制情况 被江门市蓬江区人民法院查封。"
            "建筑总面积 建筑面积106.87平方米 房产年龄 1999年建成 房屋楼层 总22层"
        ),
    }
    value.update(overrides)
    return value


def _notice(**overrides):
    value = {
        "source_url": NOTICE_URL,
        "as_is_delivery": True,
        "no_defect_warranty": True,
        "court_clear_handover": True,
        "possible_arrears_buyer_risk": True,
        "transfer_not_guaranteed": True,
        "household_registration_not_handled": True,
        "buyer_advances_seller_tax": True,
        "regret_deposit_forfeited": True,
        "occupancy_disclosed": False,
        "lease_disclosed": False,
        "viewing_deadline": "2026-07-27 16:00",
        "balance_deadline": "2026-08-12 16:00",
    }
    value.update(overrides)
    return value


def _community(**overrides):
    value = {
        "xiaoqu_id": "8895132387978913",
        "name": "益源大厦",
        "region": "江华港口 蓬江区",
        "source_url": "https://jiangmen.ke.com/xiaoqu/c8895132387978913/",
    }
    value.update(overrides)
    return value


def _market(listings=None, **overrides):
    value = {
        "status": "OK",
        "detail": {
            "xiaoqu_id": "8895132387978913",
            "name": "益源大厦",
            "address": "(蓬江区) 东华二路2号",
            "listing_average_unit_price_yuan": 4862,
            "facts": {"房屋总数": "296户", "建筑年代": "1999-2007年"},
            "source_url": "https://jiangmen.ke.com/xiaoqu/8895132387978913/",
        },
        "listings": listings
        or [
            {
                "title": "高层两房",
                "area_sqm": 109.0,
                "house_info": "高楼层 2室1厅 | 109平米 | 1999年",
                "total_price_wan": 58.0,
                "unit_price_yuan": 5322,
                "source_url": "https://jiangmen.ke.com/ershoufang/105124490657.html",
            },
            {
                "title": "疑似同套",
                "area_sqm": 106.87,
                "house_info": "高楼层 2室2厅 | 106.87平米 | 1999年",
                "total_price_wan": 55.0,
                "unit_price_yuan": 5147,
                "source_url": "https://jiangmen.ke.com/ershoufang/105121178010.html",
            },
            {
                "title": "中层三房",
                "area_sqm": 119.03,
                "house_info": "中楼层 3室1厅 | 119.03平米 | 2007年",
                "total_price_wan": 69.8,
                "unit_price_yuan": 5865,
                "source_url": "https://jiangmen.ke.com/ershoufang/105120965152.html",
            },
        ],
        "listing_source_url": (
            "https://jiangmen.ke.com/ershoufang/c8895132387978913/"
        ),
    }
    value.update(overrides)
    return value


def _build(**overrides):
    inputs = {
        "item_id": ITEM_ID,
        "detail": _detail(),
        "notice": _notice(),
        "community": _community(),
        "market": _market(),
        "expected_address": "东华二路2号之二1702室",
        "screenshot_title": "江门市蓬江区东华二路2号之二1702室",
        "screenshot_area_sqm": 106.87,
        "screenshot_starting_price_yuan": 296800,
        "retrieved_at": "2026-08-03T13:00:00+00:00",
    }
    inputs.update(overrides)
    return analysis.build_asset_analysis(**inputs)


def test_item_reference_accepts_only_id_or_canonical_url():
    assert analysis.normalize_item_reference(ITEM_ID) == ITEM_ID
    assert analysis.normalize_item_reference(ITEM_URL) == ITEM_ID
    assert analysis.normalize_item_reference(None) is None
    for invalid in (
        "123",
        f"http://sf-item.taobao.com/sf_item/{ITEM_ID}.htm",
        f"https://example.com/sf_item/{ITEM_ID}.htm",
        f"https://sf-item.taobao.com/sf_item/{ITEM_ID}.htm?cookie=x",
    ):
        with pytest.raises(analysis.AssetAnalysisError) as caught:
            analysis.normalize_item_reference(invalid)
        assert caught.value.code == "ASSET_INVALID_ITEM_REFERENCE"


def test_notice_url_is_fixed_host_path_and_matching_item_id():
    assert analysis.validate_notice_url(NOTICE_URL, ITEM_ID) == NOTICE_URL
    for invalid in (
        f"https://example.com/notice_detail/17937539.htm?item_id={ITEM_ID}",
        "https://sf.taobao.com/other/17937539.htm?item_id=" + ITEM_ID,
        "https://sf.taobao.com/notice_detail/17937539.htm?item_id=99999999",
        "https://user@sf.taobao.com/notice_detail/17937539.htm?item_id=" + ITEM_ID,
    ):
        with pytest.raises(analysis.AssetAnalysisError) as caught:
            analysis.validate_notice_url(invalid, ITEM_ID)
        assert caught.value.code == "ASSET_NOTICE_URL_REJECTED"


def test_gbk_notice_html_extracts_risk_terms_and_deadlines():
    body = """
    <html><body><h1>第二次拍卖公告</h1>
    <p>本院将依法进行司法拍卖，标的以实物现状为准，房屋拍卖成交后以现状交付。</p>
    <p>法院不承担拍卖标的瑕疵保证。可能存在物业管理费、水、电、气等欠费。</p>
    <p>被执行人税费由买受人先行垫付。本院对标的物的过户不作承诺。</p>
    <p>本院不负责户口的迁入、迁出。成交后依法对涉案标的进行清场移交。</p>
    <p>预约登记截至 202 6 年 7 月 27 日 16时。</p>
    <p>拍 卖 余 款 请 在 202 6 年 8 月 12 日 16时前缴纳。</p>
    <p>悔拍的保证金不予以退还。</p>
    <p>竞买人须自行了解全部事实，本公告用于司法拍卖。</p>
    </body></html>
    """ * 3
    text = analysis._html_to_text(body.encode("gb18030"), "text/html;charset=GBK")
    facts = analysis.parse_notice_text(text, NOTICE_URL)
    assert facts["as_is_delivery"] is True
    assert facts["court_clear_handover"] is True
    assert facts["possible_arrears_buyer_risk"] is True
    assert facts["buyer_advances_seller_tax"] is True
    assert facts["occupancy_disclosed"] is False
    assert facts["lease_disclosed"] is False
    assert facts["viewing_deadline"] == "2026-07-27 16:00"
    assert facts["balance_deadline"] == "2026-08-12 16:00"


def test_notice_parser_rejects_empty_or_non_auction_content():
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        analysis.parse_notice_text("ordinary page" * 40, NOTICE_URL)
    assert caught.value.code == "ASSET_NOTICE_CONTRACT_DRIFT"


def test_community_and_item_matchers_require_one_exact_match():
    assert analysis.select_exact_community([_community()], " 益源大厦 ")[
        "xiaoqu_id"
    ] == "8895132387978913"
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        analysis.select_exact_community([_community(name="益源大厦二期")], "益源大厦")
    assert caught.value.code == "ASSET_COMMUNITY_MATCH_AMBIGUOUS"

    result = {"items": [{"itemId": ITEM_ID, "title": _detail()["title"]}]}
    assert (
        analysis.select_exact_auction_item(result, "东华二路2号之二1702室")
        == ITEM_ID
    )
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        analysis.select_exact_auction_item({"items": []}, "东华二路2号")
    assert caught.value.code == "ASSET_ITEM_MATCH_AMBIGUOUS"


def test_real_asset_snapshot_is_matched_deduplicated_and_paused():
    report = _build()
    assert report["status"] == "OK"
    assert report["community_match"]["name"] == "益源大厦"
    assert report["market"]["listing_count"] == 3
    assert report["market"]["independent_listing_count"] == 2
    assert report["market"]["suspected_same_asset_count"] == 1
    assert report["market"]["independent_median_unit_price_yuan"] == 5593.5
    assert report["decision"] == "PAUSE_DUE_DILIGENCE"
    assert report["maximum_bid_yuan"] is None
    assert report["risk"]["critical_unknowns"] == [
        "current_occupancy",
        "lease_status",
        "arrears_amount",
        "interior_condition",
    ]
    assert report["security"] == {
        "login_automated": False,
        "credential_storage_accessed": False,
        "registration_or_bid_performed": False,
    }
    assert "最高出价：UNKNOWN" in report["markdown_report"]


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"expected_address": "错误地址"}, "expected_address"),
        ({"screenshot_title": "完全不同标的"}, "screenshot_title"),
        ({"screenshot_area_sqm": 99.0}, "screenshot_area_sqm"),
        ({"screenshot_starting_price_yuan": 1}, "screenshot_starting_price_yuan"),
    ],
)
def test_structured_screenshot_mismatch_stops_before_report(override, field):
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        _build(**override)
    assert caught.value.code == "ASSET_INPUT_EVIDENCE_MISMATCH"
    assert field in caught.value.diagnostics["mismatched_fields"]


def test_address_mismatch_and_missing_independent_comps_fail_closed():
    bad_market = _market()
    bad_market["detail"]["address"] = "(蓬江区) 另一条路99号"
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        _build(market=bad_market)
    assert caught.value.code == "ASSET_ADDRESS_MISMATCH"

    only_same = _market(listings=[_market()["listings"][1]])
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        _build(market=only_same)
    assert caught.value.code == "ASSET_INDEPENDENT_COMPS_MISSING"


def test_disclosed_occupancy_lease_and_known_interior_still_require_review():
    detail = _detail(detailText=_detail()["detailText"].replace("钥 匙 无", "钥匙 有"))
    notice = _notice(
        occupancy_disclosed=True,
        lease_disclosed=True,
        possible_arrears_buyer_risk=False,
        transfer_not_guaranteed=False,
    )
    report = _build(detail=detail, notice=notice)
    assert report["risk"]["critical_unknowns"] == []
    assert report["decision"] == "MANUAL_REVIEW_REQUIRED"
    assert report["maximum_bid_yuan"] is None


def test_asset_error_result_never_contains_a_bid_ceiling():
    result = analysis.AssetAnalysisError("CODE", "stop", {"count": 2}).as_result(
        stage="match"
    )
    assert result == {
        "status": "STOPPED",
        "stage": "match",
        "decision": "NEEDS_REVIEW",
        "maximum_bid_yuan": None,
        "error": {"code": "CODE", "message": "stop", "diagnostics": {"count": 2}},
    }


def test_public_notice_fetch_rejects_redirect_without_following(monkeypatch):
    class Response:
        status_code = 302
        content = b""
        headers = {"location": "https://example.com/secret"}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return Response()

    monkeypatch.setattr(analysis.httpx, "AsyncClient", Client)
    with pytest.raises(analysis.AssetAnalysisError) as caught:
        asyncio.run(analysis.fetch_public_notice(NOTICE_URL, ITEM_ID))
    assert caught.value.code == "ASSET_NOTICE_FETCH_FAILED"


def test_mcp_orchestrator_runs_providers_once_and_returns_traceable_report(monkeypatch):
    calls = []

    class Ali:
        async def get_item_detail(self, item_id):
            calls.append(("ali_detail", item_id))
            return _detail()

    class Beike:
        async def search_xiaoqu(self, city, keyword):
            calls.append(("beike_search", city, keyword))
            return {"status": "OK", "candidates": [_community()]}

        async def get_xiaoqu_market(self, city, xiaoqu_id, limit):
            calls.append(("beike_market", city, xiaoqu_id, limit))
            return _market()

    async def notice(url, item_id):
        calls.append(("notice", url, item_id))
        return _notice()

    monkeypatch.setattr(server, "ali_pc", Ali())
    monkeypatch.setattr(server, "beike", Beike())
    monkeypatch.setattr(server, "fetch_public_notice", notice)
    report = asyncio.run(
        server.analyze_auction_asset(
            "江门市",
            "益源大厦",
            item_ref=ITEM_URL,
            expected_address="东华二路2号之二1702室",
            screenshot_area_sqm=106.87,
            screenshot_starting_price_yuan=296800,
        )
    )
    assert report["status"] == "OK"
    assert [call[0] for call in calls] == [
        "ali_detail",
        "notice",
        "beike_search",
        "beike_market",
    ]


def test_mcp_orchestrator_stops_at_login_without_calling_downstream(monkeypatch):
    class Ali:
        async def get_item_detail(self, item_id):
            return {
                "state": "login_required",
                "authenticated": False,
                "cookie_exported": False,
            }

    class Beike:
        async def search_xiaoqu(self, *args):
            raise AssertionError("downstream provider must not run")

    monkeypatch.setattr(server, "ali_pc", Ali())
    monkeypatch.setattr(server, "beike", Beike())
    result = asyncio.run(
        server.analyze_auction_asset("江门市", "益源大厦", item_ref=ITEM_ID)
    )
    assert result["status"] == "STOPPED"
    assert result["stage"] == "ali_detail"
    assert result["maximum_bid_yuan"] is None


def test_mcp_orchestrator_can_resolve_unique_item_from_address(monkeypatch):
    class Ali:
        async def search(self, **kwargs):
            assert kwargs == {"keyword": "东华二路2号之二1702室", "limit": 20}
            return {"items": [{"itemId": ITEM_ID, "title": _detail()["title"]}]}

        async def get_item_detail(self, item_id):
            assert item_id == ITEM_ID
            return _detail()

    class Beike:
        async def search_xiaoqu(self, city, keyword):
            return {"status": "OK", "candidates": [_community()]}

        async def get_xiaoqu_market(self, city, xiaoqu_id, limit):
            return _market()

    async def notice(url, item_id):
        return _notice()

    monkeypatch.setattr(server, "ali_pc", Ali())
    monkeypatch.setattr(server, "beike", Beike())
    monkeypatch.setattr(server, "fetch_public_notice", notice)
    result = asyncio.run(
        server.analyze_auction_asset(
            "江门市",
            "益源大厦",
            expected_address="东华二路2号之二1702室",
        )
    )
    assert result["item"]["item_id"] == ITEM_ID


@pytest.mark.parametrize("limit", [0, 31, True])
def test_mcp_orchestrator_rejects_invalid_limit_before_provider(monkeypatch, limit):
    class Ali:
        async def get_item_detail(self, item_id):
            raise AssertionError("provider must not run")

    monkeypatch.setattr(server, "ali_pc", Ali())
    result = asyncio.run(
        server.analyze_auction_asset(
            "江门市", "益源大厦", item_ref=ITEM_ID, limit=limit
        )
    )
    assert result["error"]["code"] == "ASSET_INVALID_INPUT"
    assert result["stage"] == "input_validation"
