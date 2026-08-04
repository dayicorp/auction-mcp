"""P2.6 阿里 PC 分页协议发现：人工登录、一次翻页、人工确认关闭。"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ali_pc_browser_client import AliPCBrowserClient, _redact_url


QUERY = {
    "category": "住宅用房",
    "province": "广东",
    "city": "江门",
    "limit": 100,
}

_CLICKABLE_SELECTOR = 'a,button,[role="button"]'
_PAGER_SNAPSHOT_SCRIPT = r"""() => {
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


def _print_report(report: dict[str, Any]) -> None:
    print(
        "PC_PAGINATION_DISCOVERY="
        + json.dumps(report, ensure_ascii=False, indent=2),
        flush=True,
    )


def _safe_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (_redact_url(value) if key == "href" else value)
        for key, value in candidate.items()
        if key in {"index", "tag", "text", "aria", "title", "className", "id", "href"}
    }


def _choose_next_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
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
        if is_next and not is_previous:
            candidates.append(item)
    return candidates


def _evaluate_pages(
    page1_ids: list[str],
    page2_ids: list[str],
    *,
    page1_url: str | None,
    page2_url: str | None,
    page1_current: list[dict[str, Any]],
    page2_current: list[dict[str, Any]],
) -> dict[str, Any]:
    overlap = sorted(set(page1_ids) & set(page2_ids))
    page1_set = set(page1_ids)
    page2_set = set(page2_ids)
    page2_distinct_count = len(page2_set)
    new_item_count = len(page2_set - page1_set)
    accepted = bool(
        page1_ids
        and page2_ids
        and page1_ids != page2_ids
        and len(overlap) < page2_distinct_count
    )
    failures: list[str] = []
    if not page1_ids:
        failures.append("page1_no_items")
    if not page2_ids:
        failures.append("page2_no_items")
    if page1_ids and page2_ids and page1_ids == page2_ids:
        failures.append("page2_same_as_page1")
    if page2_ids and len(overlap) >= page2_distinct_count:
        failures.append("page2_has_no_unique_items")
    return {
        "accepted": accepted,
        "failures": failures,
        "page1": {
            "url": _redact_url(page1_url),
            "count": len(page1_ids),
            "item_ids": page1_ids,
            "current_indicators": page1_current,
        },
        "page2": {
            "url": _redact_url(page2_url),
            "url_changed": page1_url != page2_url,
            "count": len(page2_ids),
            "item_ids": page2_ids,
            "current_indicators": page2_current,
        },
        "dedup_boundary": {
            "overlap_count": len(overlap),
            "overlap_item_ids": overlap,
            "page2_distinct_count": page2_distinct_count,
            "new_item_count": new_item_count,
            "combined_unique_count": len(page1_set | page2_set),
            "page_sets_differ": page1_ids != page2_ids,
        },
        "page_turns": 1,
        "cookie_exported": False,
        "cookie_persisted_by_adapter": False,
    }


def _manual_continue(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() != "q"
    except EOFError:
        return False


def _confirm_page_turn() -> bool:
    while True:
        try:
            answer = input(
                "输入 TURN 并按 Enter 执行唯一一次翻页；输入 STOP 取消："
            ).strip().upper()
        except EOFError:
            return False
        if answer == "TURN":
            return True
        if answer == "STOP":
            return False
        print("未执行翻页：请输入完整口令 TURN 或 STOP。", flush=True)


def _wait_for_manual_close() -> None:
    print("浏览器将保持打开，不会因超时自动关闭。", flush=True)
    while True:
        try:
            answer = input(
                "确认查看完毕后输入 CLOSE 并按 Enter 关闭；其他输入继续保持："
            ).strip().upper()
        except EOFError:
            print("当前终端不可交互；为避免自动关闭，请保持此进程运行。", flush=True)
            continue
        if answer == "CLOSE":
            return
        print("浏览器继续保持；只有完整口令 CLOSE 会关闭。", flush=True)


async def _pager_snapshot(client: AliPCBrowserClient) -> dict[str, Any]:
    assert client._page is not None
    return await client._page.evaluate(_PAGER_SNAPSHOT_SCRIPT)


async def _wait_for_distinct_page(
    client: AliPCBrowserClient,
    page1_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    last_items: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    for _ in range(20):
        last_items, diagnostics = await client._wait_for_items(100)
        ids = [
            str(item.get("itemId"))
            for item in last_items
            if item.get("itemId") is not None
        ]
        if ids and ids != page1_ids:
            return last_items, diagnostics
        assert client._page is not None
        await client._page.wait_for_timeout(500)
    return last_items, diagnostics


async def _run() -> int:
    client = AliPCBrowserClient(headless=False)
    browser_started = False
    exit_code = 1
    report: dict[str, Any] | None = None
    try:
        status = await client.start()
        browser_started = not bool(status.get("error"))
        if status.get("error"):
            report = {
                "accepted": False,
                "failures": [status["error"]],
                "evidence": status,
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
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
                report = {
                    "accepted": False,
                    "failures": ["user_cancelled"],
                    "cookie_exported": False,
                    "cookie_persisted_by_adapter": False,
                }
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
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 2

        print("登录态已确认，读取江门住宅第一页和分页控件……", flush=True)
        first = await client.search(**QUERY)
        if first.get("error") or first.get("state") == "action_required":
            report = {
                "accepted": False,
                "failures": [first.get("error") or first.get("reason") or "page1_failed"],
                "evidence": {
                    "message": first.get("message"),
                    "url": _redact_url(first.get("url")),
                },
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 1

        page1_ids = [
            str(item.get("itemId"))
            for item in first.get("items", [])
            if item.get("itemId") is not None
        ]
        page1_snapshot = await _pager_snapshot(client)
        next_candidates = _choose_next_candidates(page1_snapshot)
        if len(next_candidates) != 1:
            report = {
                "accepted": False,
                "failures": ["pagination_next_control_not_unique"],
                "evidence": {
                    "url": _redact_url(first.get("url")),
                    "page1_count": len(page1_ids),
                    "candidate_count": len(next_candidates),
                    "next_candidates": [_safe_candidate(item) for item in next_candidates],
                    "current_indicators": page1_snapshot.get("current", []),
                },
                "page_turns": 0,
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 1

        print(
            "已唯一识别下一页控件："
            + json.dumps(_safe_candidate(next_candidates[0]), ensure_ascii=False),
            flush=True,
        )
        if not _confirm_page_turn():
            report = {
                "accepted": False,
                "failures": ["user_cancelled_before_page_turn"],
                "page_turns": 0,
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 2

        refreshed_snapshot = await _pager_snapshot(client)
        refreshed_candidates = _choose_next_candidates(refreshed_snapshot)
        if len(refreshed_candidates) != 1:
            report = {
                "accepted": False,
                "failures": ["pagination_next_control_changed_before_click"],
                "page_turns": 0,
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 1

        assert client._page is not None
        page1_url = client._page.url
        await client._page.locator(_CLICKABLE_SELECTOR).nth(
            refreshed_candidates[0]["index"]
        ).click()
        try:
            await client._page.wait_for_load_state(
                "domcontentloaded", timeout=client.timeout_ms
            )
        except Exception:
            pass
        await client._page.wait_for_timeout(1500)

        title = await client._page.title()
        body = await client._page.locator("body").inner_text(timeout=client.timeout_ms)
        action = client._action_required(client._page.url, title, body)
        if action:
            report = {
                "accepted": False,
                "failures": [action.get("reason") or "action_required_after_page_turn"],
                "evidence": {
                    "message": action.get("message"),
                    "url": _redact_url(action.get("url")),
                },
                "page_turns": 1,
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
            return 1

        page2_items, parse_diagnostics = await _wait_for_distinct_page(
            client, page1_ids
        )
        page2_ids = [
            str(item.get("itemId"))
            for item in page2_items
            if item.get("itemId") is not None
        ]
        page2_snapshot = await _pager_snapshot(client)
        report = _evaluate_pages(
            page1_ids,
            page2_ids,
            page1_url=page1_url,
            page2_url=client._page.url,
            page1_current=page1_snapshot.get("current", []),
            page2_current=page2_snapshot.get("current", []),
        )
        report["query"] = {key: value for key, value in QUERY.items() if key != "limit"}
        report["parse_diagnostics"] = parse_diagnostics
        report["next_control"] = _safe_candidate(refreshed_candidates[0])
        exit_code = 0 if report["accepted"] else 1
        return exit_code
    except Exception as exc:
        report = {
            "accepted": False,
            "failures": ["pagination_discovery_exception"],
            "evidence": {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "url": _redact_url(client._page.url if client._page else None),
            },
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        }
        return 1
    finally:
        if report is None:
            report = {
                "accepted": False,
                "failures": ["pagination_discovery_no_report"],
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }
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
