from pathlib import Path

from scripts.manual_live_pc_pagination import (
    _choose_next_candidates,
    _confirm_page_turn,
    _evaluate_pages,
    _safe_candidate,
    _wait_for_manual_close,
)
from scripts.manual_live_pc_page2 import evaluate_page2_acceptance


def test_choose_next_candidate_ignores_previous_hidden_and_disabled_controls():
    snapshot = {
        "controls": [
            {
                "index": 1,
                "text": "上一页",
                "className": "page-prev",
                "visible": True,
                "disabled": False,
            },
            {
                "index": 2,
                "text": "下一页",
                "className": "page-next",
                "visible": False,
                "disabled": False,
            },
            {
                "index": 3,
                "text": "下一页",
                "className": "page-next disabled",
                "visible": True,
                "disabled": True,
            },
            {
                "index": 4,
                "text": "下一页",
                "className": "page-next",
                "visible": True,
                "disabled": False,
            },
        ]
    }

    assert _choose_next_candidates(snapshot) == [snapshot["controls"][3]]


def test_evaluate_pages_accepts_distinct_second_page_and_reports_overlap():
    report = _evaluate_pages(
        ["1", "2", "3"],
        ["3", "4", "5"],
        page1_url="https://sf.taobao.com/list?page=1",
        page2_url="https://sf.taobao.com/list?page=2",
        page1_current=[{"text": "1"}],
        page2_current=[{"text": "2"}],
    )

    assert report["accepted"] is True
    assert report["failures"] == []
    assert report["page2"]["url_changed"] is True
    assert report["dedup_boundary"] == {
        "overlap_count": 1,
        "overlap_item_ids": ["3"],
        "page2_distinct_count": 3,
        "new_item_count": 2,
        "combined_unique_count": 5,
        "page_sets_differ": True,
    }
    assert report["page_turns"] == 1
    assert report["cookie_exported"] is False


def test_evaluate_pages_rejects_same_result_set():
    report = _evaluate_pages(
        ["1", "2"],
        ["1", "2"],
        page1_url="https://sf.taobao.com/list",
        page2_url="https://sf.taobao.com/list",
        page1_current=[],
        page2_current=[],
    )

    assert report["accepted"] is False
    assert "page2_same_as_page1" in report["failures"]
    assert "page2_has_no_unique_items" in report["failures"]


def test_safe_candidate_redacts_risk_token():
    result = _safe_candidate({
        "index": 7,
        "tag": "a",
        "text": "下一页",
        "href": "https://sf.taobao.com/punish?x5secdata=secret&page=2",
        "visible": True,
    })

    assert "secret" not in result["href"]
    assert "REDACTED" in result["href"]
    assert "visible" not in result


def test_manual_script_waits_for_user_before_close_without_auto_close_timer():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "manual_live_pc_pagination.py"
    ).read_text(encoding="utf-8")

    assert "asyncio.sleep(" not in source
    manual_wait = source.index("_wait_for_manual_close()")
    browser_close = source.index("await client.close()", manual_wait)
    assert manual_wait < browser_close


def test_page_turn_requires_explicit_turn_word(monkeypatch):
    answers = iter(["", "q", "TURN"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert _confirm_page_turn() is True


def test_manual_close_requires_explicit_close_word(monkeypatch):
    answers = iter(["", "q", "CLOSE"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    _wait_for_manual_close()


def test_page2_acceptance_requires_verified_page_receipt_and_filters():
    report = evaluate_page2_acceptance({
        "source": "ali_pc_browser",
        "authenticated_session": True,
        "count": 2,
        "items": [{"itemId": "2"}, {"itemId": "3"}],
        "url": "https://sf.taobao.com/list/example.htm?page=2",
        "page": 2,
        "pageTurns": 1,
        "paginationReceipts": [{"fromPage": 1, "toPage": 2}],
        "appliedFilters": [
            {"dimension": "category", "label": "住宅用房"},
            {"dimension": "province", "label": "广东"},
            {"dimension": "city", "label": "江门"},
        ],
        "cookie_exported": False,
        "cookie_persisted_by_adapter": False,
    })

    assert report["accepted"] is True
    assert report["failures"] == []
    assert report["evidence"]["query_page"] == "2"


def test_page2_acceptance_rejects_unverified_page_or_duplicate_items():
    report = evaluate_page2_acceptance({
        "source": "ali_pc_browser",
        "authenticated_session": True,
        "count": 2,
        "items": [{"itemId": "2"}, {"itemId": "2"}],
        "url": "https://sf.taobao.com/list/example.htm",
        "page": 1,
        "pageTurns": 0,
        "paginationReceipts": [],
        "appliedFilters": [],
    })

    assert report["accepted"] is False
    assert "page_not_confirmed" in report["failures"]
    assert "page_query_param_mismatch" in report["failures"]
    assert "duplicate_items_within_page" in report["failures"]
    assert "applied_filter_mismatch" in report["failures"]
