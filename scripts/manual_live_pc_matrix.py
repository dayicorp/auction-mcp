"""P2.5 一次登录多场景 PC Live 能力矩阵验收."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ali_pc_browser_client import AliPCBrowserClient, evaluate_pc_matrix_scenario


LINK_SCENARIOS = [
    ("keyword", {"keyword": "商业用房"}, {"keyword": "商业用房"}),
    (
        "district",
        {"category": "住宅用房", "province": "广东", "city": "江门", "district": "蓬江"},
        {"category": "住宅用房", "province": "广东", "city": "江门", "district": "蓬江"},
    ),
    (
        "asset_type",
        {"category": "住宅用房", "asset_type": "涉刑资产"},
        {"category": "住宅用房", "asset_type": "涉刑资产"},
    ),
]

PREFERRED_SELECT_OPTIONS = {
    "sort": ["价格从低到高", "价格由低到高", "当前价格由低到高", "价格从高到低"],
    "status": ["正在进行", "即将开始"],
    "stage": ["一拍", "二拍", "变卖"],
}
SCENARIO_ORDER = ("keyword", "district", "asset_type", "sort", "status", "stage")


def _print_report(report: dict) -> None:
    print("PC_FILTER_MATRIX_ACCEPTANCE=" + json.dumps(report, ensure_ascii=False, indent=2))


def _pause_before_failure_close() -> None:
    try:
        input("矩阵验收未通过，浏览器暂时保留供检查；查看完成后按 Enter 关闭：")
    except EOFError:
        pass


def _choose_dynamic_select(snapshot: dict, dimension: str) -> str | None:
    entry = (snapshot.get("selectDimensions") or {}).get(dimension) or {}
    options = [str(option).strip() for option in entry.get("options", []) if str(option).strip()]
    link_labels = {
        str(link.get("text") or "").strip()
        for link in snapshot.get("linkOptions", [])
        if str(link.get("text") or "").strip()
    }
    for preferred in PREFERRED_SELECT_OPTIONS[dimension]:
        if preferred in options or preferred in link_labels:
            return preferred
    selected = str(entry.get("selected") or "")
    return next((option for option in options if option != selected), None)


def _capability_evidence(
    catalog: dict,
    scoped: dict | None,
    chosen: dict[str, str],
    selected_scenarios: set[str],
) -> dict:
    catalog_labels = {entry.get("text") for entry in catalog.get("linkOptions", [])}
    scoped_labels = {
        entry.get("text") for entry in (scoped or {}).get("linkOptions", [])
    }
    required_catalog = []
    if "keyword" in selected_scenarios:
        required_catalog.append("商业用房")
    if "district" in selected_scenarios:
        required_catalog.append("广东")
    if "asset_type" in selected_scenarios:
        required_catalog.append("涉刑资产")
    return {
        "catalog_url": catalog.get("url"),
        "scoped_url": (scoped or {}).get("url"),
        "catalog_link_option_count": catalog.get("linkOptionCount"),
        "scoped_link_option_count": (scoped or {}).get("linkOptionCount"),
        "required_catalog_links": {label: label in catalog_labels for label in required_catalog},
        "required_district_link": (
            {"蓬江": "蓬江" in scoped_labels}
            if "district" in selected_scenarios
            else {}
        ),
        "select_dimensions": catalog.get("selectDimensions"),
        "chosen_select_options": chosen,
        "cookie_exported": False,
        "cookie_persisted_by_adapter": False,
    }


async def _run(selected_scenarios: tuple[str, ...], delay_seconds: float) -> int:
    client = AliPCBrowserClient(headless=False)
    selected_set = set(selected_scenarios)
    try:
        status = await client.start()
        if status.get("error"):
            _print_report({"accepted": False, "failures": [status["error"]], "evidence": status})
            return 2

        for _ in range(2):
            if status.get("state") == "ready":
                break
            reason = status.get("reason") or status.get("state") or "unknown"
            print(f"浏览器等待人工操作：{reason}")
            print("请只在弹出的 Chrome 中完成登录/滑块/二维码；程序不会读取或保存 Cookie。")
            answer = input("完成后只按 Enter 继续；输入 q 终止：").strip().lower()
            if answer == "q":
                _print_report({"accepted": False, "failures": ["user_cancelled"], "evidence": {}})
                return 2
            status = await client.status()

        if status.get("state") != "ready":
            _print_report({"accepted": False, "failures": [f"session_state:{status.get('state')}"]})
            _pause_before_failure_close()
            return 2

        print("登录态已确认，读取真实页面能力地图……")
        catalog = await client.get_filter_options(category="住宅用房")
        if catalog.get("state") != "ready":
            _print_report({"accepted": False, "failures": [catalog.get("error") or catalog.get("state")], "evidence": catalog})
            _pause_before_failure_close()
            return 1
        scoped = None
        if "district" in selected_set:
            scoped = await client.get_filter_options(
                category="住宅用房",
                province="广东",
                city="江门",
            )
            if scoped.get("state") != "ready":
                _print_report({"accepted": False, "failures": [scoped.get("error") or scoped.get("state")], "evidence": scoped})
                _pause_before_failure_close()
                return 1

        chosen = {
            dimension: option
            for dimension in ("sort", "status", "stage")
            if dimension in selected_set
            and (option := _choose_dynamic_select(catalog, dimension))
        }
        capability = _capability_evidence(catalog, scoped, chosen, selected_set)
        capability_failures = [
            f"missing_link:{label}"
            for label, present in {
                **capability["required_catalog_links"],
                **capability["required_district_link"],
            }.items()
            if not present
        ]
        capability_failures.extend(
            f"missing_select_dimension:{dimension}"
            for dimension in ("sort", "status", "stage")
            if dimension in selected_set and dimension not in chosen
        )
        if capability_failures:
            _print_report({"accepted": False, "failures": capability_failures, "capabilities": capability})
            _pause_before_failure_close()
            return 1

        scenario_specs = {name: (name, kwargs, expected) for name, kwargs, expected in LINK_SCENARIOS}
        scenario_specs.update(
            {
                dimension: (
                    dimension,
                    {"category": "住宅用房", dimension: label},
                    {"category": "住宅用房", dimension: label},
                )
                for dimension, label in chosen.items()
            }
        )
        scenarios = [scenario_specs[name] for name in selected_scenarios]
        reports: list[dict] = []
        for index, (name, kwargs, expected) in enumerate(scenarios, start=1):
            print(f"执行矩阵场景 {index}/{len(scenarios)}：{name}")
            result = await client.search(limit=10, **kwargs)
            report = evaluate_pc_matrix_scenario(name, result, expected)
            reports.append(report)
            if not report["accepted"]:
                break
            if index < len(scenarios):
                await asyncio.sleep(delay_seconds)

        accepted = len(reports) == len(scenarios) and all(report["accepted"] for report in reports)
        matrix_report = {
            "accepted": accepted,
            "failures": [
                failure
                for report in reports
                for failure in report.get("failures", [])
            ],
            "scenario_count": len(reports),
            "expected_scenario_count": len(scenarios),
            "selected_scenarios": list(selected_scenarios),
            "delay_seconds": delay_seconds,
            "capabilities": capability,
            "scenarios": reports,
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        }
        _print_report(matrix_report)
        if not accepted:
            _pause_before_failure_close()
        return 0 if accepted else 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="阿里 PC 筛选能力短批次 Live 验收")
    parser.add_argument(
        "--scenarios",
        default=",".join(SCENARIO_ORDER),
        help="逗号分隔场景：keyword,district,asset_type,sort,status,stage",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=10.0,
        help="场景间隔秒数，最低 5 秒，默认 10 秒",
    )
    args = parser.parse_args()
    selected = tuple(
        name.strip() for name in args.scenarios.split(",") if name.strip()
    )
    unknown = [name for name in selected if name not in SCENARIO_ORDER]
    if not selected or unknown:
        parser.error(f"无效场景: {','.join(unknown) if unknown else '空'}")
    if len(set(selected)) != len(selected):
        parser.error("场景不得重复")
    if args.delay_seconds < 5:
        parser.error("--delay-seconds 不得小于 5")
    try:
        return asyncio.run(_run(selected, args.delay_seconds))
    except KeyboardInterrupt:
        _print_report({"accepted": False, "failures": ["user_interrupted"]})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
