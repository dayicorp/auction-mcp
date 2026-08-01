"""一次性阿里 PC Live 验收：人工登录，自动查询，结构化判定，自动关闭."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ali_pc_browser_client import AliPCBrowserClient, evaluate_pc_live_acceptance


QUERY = {
    "category": "住宅用房",
    "province": "广东",
    "city": "江门",
    "max_price_yuan": 210_000,
    "auction_start_from": "2026-08-01",
    "auction_start_to": "2026-09-01",
    "limit": 20,
}


def _print_report(report: dict) -> None:
    print("PC_LIVE_ACCEPTANCE=" + json.dumps(report, ensure_ascii=False, indent=2))


async def _run() -> int:
    client = AliPCBrowserClient(headless=False)
    try:
        status = await client.start()
        if status.get("error"):
            _print_report({"accepted": False, "failures": [status["error"]], "evidence": status})
            return 2

        for attempt in range(2):
            if status.get("state") == "ready":
                break
            reason = status.get("reason") or status.get("state") or "unknown"
            print(f"浏览器等待人工操作：{reason}")
            print("请只在弹出的 Chrome 中完成登录/滑块/二维码；程序不会读取或保存 Cookie。")
            answer = input("完成后按 Enter 继续；输入 q 终止：").strip().lower()
            if answer == "q":
                _print_report({"accepted": False, "failures": ["user_cancelled"], "evidence": {}})
                return 2
            status = await client.status()

        if status.get("state") != "ready":
            _print_report({
                "accepted": False,
                "failures": [f"session_state:{status.get('state', 'unknown')}"],
                "evidence": {
                    "reason": status.get("reason"),
                    "message": status.get("message"),
                    "url": status.get("url"),
                    "cookie_exported": False,
                    "cookie_persisted_by_adapter": False,
                },
            })
            return 2

        print("登录态已确认，开始执行固定验收查询……")
        result = await client.search(**QUERY)
        report = evaluate_pc_live_acceptance(
            result,
            max_price_yuan=QUERY["max_price_yuan"],
            auction_start_from=QUERY["auction_start_from"],
            auction_start_to=QUERY["auction_start_to"],
        )
        _print_report(report)
        return 0 if report["accepted"] else 1
    finally:
        await client.close()


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        _print_report({"accepted": False, "failures": ["user_interrupted"], "evidence": {}})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
