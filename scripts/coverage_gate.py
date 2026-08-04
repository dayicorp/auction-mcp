"""Offline statement and branch coverage release gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "coverage_contract.json"


class CoverageGateError(RuntimeError):
    """Raised when measured coverage violates the frozen contract."""


def _run(command: Sequence[str], *, env: dict[str, str]) -> None:
    completed = subprocess.run(
        list(command),
        cwd=ROOT,
        env=env,
        check=False,
        timeout=360,
    )
    if completed.returncode != 0:
        raise CoverageGateError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
        )


def load_contract() -> dict[str, Any]:
    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageGateError(f"cannot read coverage contract: {exc}") from exc
    if contract.get("version") != 1:
        raise CoverageGateError("unsupported coverage contract version")
    return contract


def _percent(summary: dict[str, Any], kind: str) -> float:
    if kind == "statement":
        total = int(summary.get("num_statements", 0))
        covered = int(summary.get("covered_lines", 0))
    elif kind == "branch":
        total = int(summary.get("num_branches", 0))
        covered = int(summary.get("covered_branches", 0))
    else:
        raise CoverageGateError(f"unsupported coverage kind: {kind}")
    if total <= 0:
        raise CoverageGateError(f"coverage report has no {kind} opportunities")
    return covered * 100.0 / total


def verify_report(
    report: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    totals = report.get("totals") or {}
    combined_percent = float(totals.get("percent_covered", 0.0))
    statement_percent = _percent(totals, "statement")
    branch_percent = _percent(totals, "branch")
    combined_minimum = float(contract["minimum_combined_coverage_percent"])
    statement_minimum = float(contract["minimum_statement_coverage_percent"])
    branch_minimum = float(contract["minimum_branch_coverage_percent"])
    if combined_percent + 1e-9 < combined_minimum:
        raise CoverageGateError(
            f"offline combined coverage {combined_percent:.3f}% is below {combined_minimum:.3f}%"
        )
    if statement_percent + 1e-9 < statement_minimum:
        raise CoverageGateError(
            f"offline statement coverage {statement_percent:.3f}% is below {statement_minimum:.3f}%"
        )
    if branch_percent + 1e-9 < branch_minimum:
        raise CoverageGateError(
            f"offline branch coverage {branch_percent:.3f}% is below {branch_minimum:.3f}%"
        )

    critical: dict[str, Any] = {}
    files = report.get("files") or {}
    for filename, thresholds in contract["critical_files"].items():
        file_report = files.get(filename)
        if not isinstance(file_report, dict):
            raise CoverageGateError(f"critical coverage file missing: {filename}")
        summary = file_report.get("summary") or {}
        critical_statement_percent = _percent(summary, "statement")
        critical_branch_percent = _percent(summary, "branch")
        if critical_statement_percent + 1e-9 < float(
            thresholds["minimum_statement_coverage_percent"]
        ):
            raise CoverageGateError(
                f"critical statement coverage below threshold: {filename}"
            )
        if critical_branch_percent + 1e-9 < float(
            thresholds["minimum_branch_coverage_percent"]
        ):
            raise CoverageGateError(
                f"critical branch coverage below threshold: {filename}"
            )
        if summary.get("missing_lines") or summary.get("missing_branches"):
            raise CoverageGateError(
                f"critical fail-closed paths have missing coverage: {filename}"
            )
        critical[filename] = {
            "branch_percent": round(critical_branch_percent, 3),
            "statement_percent": round(critical_statement_percent, 3),
        }

    return {
        "baseline_commit": contract["baseline"]["commit"],
        "branch_percent": round(branch_percent, 3),
        "combined_percent": round(combined_percent, 3),
        "critical": critical,
        "minimum_branch_percent": branch_minimum,
        "minimum_combined_percent": combined_minimum,
        "minimum_statement_percent": statement_minimum,
        "statement_percent": round(statement_percent, 3),
    }


def main() -> int:
    try:
        contract = load_contract()
        source = ",".join(contract["source_modules"])
        with tempfile.TemporaryDirectory(prefix="auction-mcp-coverage-") as temp:
            temp_root = Path(temp)
            coverage_file = temp_root / ".coverage"
            json_report = temp_root / "coverage.json"
            env = os.environ.copy()
            env["COVERAGE_FILE"] = str(coverage_file)
            env["PYTEST_ADDOPTS"] = ""
            _run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    f"--source={source}",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests",
                ],
                env=env,
            )
            _run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    str(json_report),
                ],
                env=env,
            )
            report = json.loads(json_report.read_text(encoding="utf-8"))
        diagnostic = verify_report(report, contract)
    except Exception as exc:
        print(
            "COVERAGE_DIAGNOSTIC="
            + json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, sort_keys=True
            ),
            file=sys.stderr,
        )
        print("COVERAGE_GATE: FAIL", file=sys.stderr)
        return 1
    print("COVERAGE_DIAGNOSTIC=" + json.dumps(diagnostic, sort_keys=True))
    print("COVERAGE_GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
