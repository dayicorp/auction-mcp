"""P2.7 阿里 PC page=2 正式实现 Live 验收。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ali_pc_browser_client import AliPCBrowserClient, _redact_url
from scripts.manual_live_pc_pagination import _manual_continue, _wait_for_manual_close


QUERY = {
    "category": "住宅用房",
    "province": "广东",
    "city": "江门",
    "page": 2,
    "limit": 100,
}


def evaluate_page2_acceptance(result: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if result.get("error"):
        failures.append(f"query_error:{result['error']}")
    if result.get("state") == "action_required":
        failures.append(f"action_required:{result.get('reason', 'unknown')}")
    if result.get("source") != "ali_pc_browser":
        failures.append("unexpected_source")
    if result.get("authenticated_session") is not True:
        failures.append("authenticated_session_not_confirmed")
    if result.get("page") != 2:
        failures.append("page_not_confirmed")
    if result.get("pageTurns") != 1:
        failures.append("page_turn_count_mismatch")

    query = parse_qs(urlsplit(str(result.get("url") or "")).query)
    if query.get("page") != ["2"]:
        failures.append("page_query_param_mismatch")

    receipts = result.get("paginationReceipts") or []
    if len(receipts) != 1:
        failures.append("pagination_receipt_count_mismatch")
    elif receipts[0].get("fromPage") != 1 or receipts[0].get("toPage") != 2:
        failures.append("pagination_receipt_mismatch")

    expected_filters = {"category": "住宅用房", "province": "广东", "city": "江门"}
    actual_filters = {
        str(entry.get("dimension")): str(entry.get("label"))
        for entry in result.get("appliedFilters", [])
        if entry.get("dimension") and entry.get("label") is not None
    }
    filter_mismatches = {
        dimension: {"expected": label, "actual": actual_filters.get(dimension)}
        for dimension, label in expected_filters.items()
        if actual_filters.get(dimension) != label
    }
    if filter_mismatches:
        failures.append("applied_filter_mismatch")

    items = result.get("items") or []
    item_ids = [
        str(item.get("itemId"))
        for item in items
        if item.get("itemId") is not None
    ]
    if not item_ids:
        failures.append("no_items")
    if len(item_ids) != len(set(item_ids)):
        failures.append("duplicate_items_within_page")
    if result.get("count") != len(items):
        failures.append("reported_count_mismatch")

    return {
        "accepted": not failures,
        "failures": failures,
        "evidence": {
            "count": len(items),
            "reported_count": result.get("count"),
            "page": result.get("page"),
            "page_turns": result.get("pageTurns"),
            "url": _redact_url(result.get("url")),
            "query_page": query.get("page", [None])[0],
            "pagination_receipts": receipts,
            "expected_filters": expected_filters,
            "applied_filters": actual_filters,
            "filter_mismatches": filter_mismatches,
            "sample_item_ids": item_ids[:5],
            "cookie_exported": result.get("cookie_exported", False),
            "cookie_persisted_by_adapter": result.get(
                "cookie_persisted_by_adapter", False
            ),
        },
        "cookie_exported": False,
        "cookie_persisted_by_adapter": False,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(
        "PC_PAGE2_ACCEPTANCE=" + json.dumps(report, ensure_ascii=False, indent=2),
        flush=True,
    )


async def _run() -> int:
    client = AliPCBrowserClient(headless=False)
    browser_started = False
    report: dict[str, Any] | None = None
    try:
        status = await client.start()
        browser_started = not bool(status.get("error"))
        if status.get("error"):
            report = {
                "accepted": False,
                "failures": [status["error"]],
                "evidence": status,
            }
            return 2

        for _ in range(2):
            if status.get("state") == "ready":
                break
            reason = status.get("reason") or status.get("state") or "unknown"
            print(f"浏览器等待人工操作：{reason}", flush=True)
            print(
                "请只在弹出的 Chrome 中完成登录/滑块/二维码；程序不会读取或保存 Cookie。",
                flush=True,
            )
            if not _manual_continue("完成后回到此 PowerShell，只按 Enter 继续；输入 q 终止："):
                report = {"accepted": False, "failures": ["user_cancelled"]}
                return 2
            assert client._page is not None
            await client._page.reload(wait_until="domcontentloaded")
            status = await client.status()

        if status.get("state") != "ready":
            report = {
                "accepted": False,
                "failures": [f"session_state:{status.get('state', 'unknown')}"],
                "evidence": {
                    "reason": status.get("reason"),
                    "message": status.get("message"),
                    "url": _redact_url(status.get("url")),
                },
            }
            return 2

        print("登录态已确认，执行正式 ali_pc_search_judicial(page=2) 验收……", flush=True)
        result = await client.search(**QUERY)
        report = evaluate_page2_acceptance(result)
        return 0 if report["accepted"] else 1
    except Exception as exc:
        report = {
            "accepted": False,
            "failures": ["page2_acceptance_exception"],
            "evidence": {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "url": _redact_url(client._page.url if client._page else None),
            },
        }
        return 1
    finally:
        if report is None:
            report = {"accepted": False, "failures": ["page2_acceptance_no_report"]}
        report["cookie_exported"] = False
        report["cookie_persisted_by_adapter"] = False
        _print_report(report)
        if browser_started:
            _wait_for_manual_close()
            await client.close()
            print("浏览器已按用户确认关闭。", flush=True)


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        _print_report({
            "accepted": False,
            "failures": ["user_interrupted"],
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        })
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
