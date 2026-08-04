"""Deterministic mutation gate for auction-mcp safety-critical behavior.

Each mutant changes exactly one verified source fragment in a temporary copy.
All listed mutants are mandatory; a surviving, skipped, ambiguous, or timed-out
mutant fails the gate.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    name: str
    category: str
    path: str
    original: str
    replacement: str
    tests: tuple[str, ...]


MUTANTS = (
    Mutant(
        "city_parent_condition_inversion",
        "condition_inversion",
        "safety_core.py",
        "if city is not None and province is None:",
        "if city is not None and province is not None:",
        ("tests/test_safety_core.py", "tests/test_region_boundary.py"),
    ),
    Mutant(
        "district_parent_boundary_deleted",
        "boundary_deletion",
        "safety_core.py",
        "if district is not None and city is None:",
        "if False and district is not None and city is None:",
        ("tests/test_safety_core.py", "tests/test_region_boundary.py"),
    ),
    Mutant(
        "region_error_code_replaced",
        "error_code_replacement",
        "safety_core.py",
        'return "city_requires_province"',
        'return "city_requires_parent"',
        ("tests/test_safety_core.py", "tests/test_region_boundary.py"),
    ),
    Mutant(
        "municipality_guard_bypassed",
        "guard_bypass",
        "safety_core.py",
        "return province in aliases and city in aliases",
        "return True",
        ("tests/test_safety_core.py", "tests/test_region_boundary.py"),
    ),
    Mutant(
        "external_network_guard_bypassed",
        "guard_bypass",
        "safety_core.py",
        "return address[0] in LOOPBACK_HOSTS",
        "return True",
        ("tests/test_safety_core.py", "tests/test_consumer_probe.py"),
    ),
    Mutant(
        "ali_scope_length_boundary_removed",
        "boundary_deletion",
        "ali_h5_client.py",
        "if len(s) == 6:",
        "if len(s) >= 6:",
        ("tests/test_validation.py",),
    ),
    Mutant(
        "ali_scope_ratio_default_changed",
        "default_change",
        "ali_h5_client.py",
        "min_ratio: float = 0.8",
        "min_ratio: float = 0.0",
        ("tests/test_validation.py",),
    ),
    Mutant(
        "ali_status_default_changed",
        "default_change",
        "ali_h5_client.py",
        'DEFAULT_STATUS_ORDERS = ["0", "1"]',
        'DEFAULT_STATUS_ORDERS = ["0"]',
        ("tests/test_mutation_contract.py",),
    ),
    Mutant(
        "jd_status_default_changed",
        "default_change",
        "jd_h5_client.py",
        'DEFAULT_STATUS = "101,102"',
        'DEFAULT_STATUS = "101"',
        ("tests/test_mutation_contract.py",),
    ),
    Mutant(
        "tool_filter_conflict_guard_bypassed",
        "guard_bypass",
        "server.py",
        "if fcat_v4_ids and fcat_v4_names:",
        "if False and fcat_v4_ids and fcat_v4_names:",
        ("tests/test_validation.py",),
    ),
    Mutant(
        "required_tool_parameter_schema_changed",
        "schema_drift",
        "auction_mcp_assets/mcp_contract.json",
        '"item_id": {"nullable": false, "required": true, "type": "string"}',
        '"item_id": {"nullable": false, "required": false, "type": "string"}',
        ("tests/test_consumer_probe.py", "tests/test_mutation_contract.py"),
    ),
    Mutant(
        "schema_required_flag_bypassed",
        "schema_drift",
        "scripts/consumer_probe.py",
        '"required": name in required,',
        '"required": False,',
        ("tests/test_consumer_probe.py",),
    ),
    Mutant(
        "evidence_size_limit_expanded",
        "boundary_deletion",
        "evidence_safety.py",
        "MAX_BUNDLE_BYTES = 1024 * 1024",
        "MAX_BUNDLE_BYTES = 1024 * 1024 * 1024",
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_depth_limit_expanded",
        "boundary_deletion",
        "evidence_safety.py",
        "MAX_JSON_DEPTH = 24",
        "MAX_JSON_DEPTH = 2400",
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_sensitive_key_guard_bypassed",
        "guard_bypass",
        "evidence_safety.py",
        "if _sensitive_key(key, path):",
        "if False and _sensitive_key(key, path):",
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_bank_card_guard_bypassed",
        "guard_bypass",
        "evidence_safety.py",
        "return total % 10 == 0",
        "return False",
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_https_boundary_weakened",
        "condition_inversion",
        "evidence_safety.py",
        'parsed.scheme != "https"',
        'parsed.scheme != "http"',
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_version_guard_bypassed",
        "guard_bypass",
        "evidence_safety.py",
        "if actual != contract:",
        "if False and actual != contract:",
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_segment_hash_guard_bypassed",
        "guard_bypass",
        "evidence_bundle.py",
        'if not hmac.compare_digest(str(segment["sha256"]), actual_hash):',
        'if False and not hmac.compare_digest(str(segment["sha256"]), actual_hash):',
        ("tests/test_evidence_bundle.py",),
    ),
    Mutant(
        "evidence_bid_guard_bypassed",
        "guard_bypass",
        "evidence_bundle.py",
        'if report.get("maximum_bid_yuan") is not None:',
        'if False and report.get("maximum_bid_yuan") is not None:',
        ("tests/test_evidence_bundle.py",),
    ),
)


class MutationGateError(RuntimeError):
    """Raised when a safety mutant cannot be proven killed."""


def repository_files() -> list[PurePosixPath]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise MutationGateError("git ls-files failed")
    return [
        PurePosixPath(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    ]


def copy_repository(target: Path, paths: Sequence[PurePosixPath]) -> None:
    for relative in paths:
        source = ROOT / Path(*relative.parts)
        destination = target / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def apply_mutant(root: Path, mutant: Mutant) -> None:
    path = root / mutant.path
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(mutant.original)
    if occurrences != 1:
        raise MutationGateError(
            f"mutant {mutant.name} expected one source match, found {occurrences}"
        )
    path.write_text(
        text.replace(mutant.original, mutant.replacement, 1), encoding="utf-8"
    )


def run_mutant(mutant: Mutant, paths: Sequence[PurePosixPath]) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"auction-mutant-{mutant.name}-") as temp:
        mutant_root = Path(temp) / "repo"
        mutant_root.mkdir()
        copy_repository(mutant_root, paths)
        apply_mutant(mutant_root, mutant)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(mutant_root)
        env["PYTEST_ADDOPTS"] = ""
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    *mutant.tests,
                ],
                cwd=mutant_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise MutationGateError(
                f"mutant {mutant.name} timed out instead of being killed"
            ) from exc
    if completed.returncode == 0:
        raise MutationGateError(f"surviving safety-critical mutant: {mutant.name}")
    if completed.returncode != 1:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
        raise MutationGateError(
            f"mutant {mutant.name} ended with infrastructure exit "
            f"{completed.returncode}: {' | '.join(tail)}"
        )
    return {
        "category": mutant.category,
        "duration_seconds": round(time.monotonic() - started, 3),
        "name": mutant.name,
        "status": "killed",
    }


def main() -> int:
    try:
        paths = repository_files()
        results = [run_mutant(mutant, paths) for mutant in MUTANTS]
        categories = sorted({mutant.category for mutant in MUTANTS})
        diagnostic = {
            "categories": categories,
            "killed": len(results),
            "mutants": results,
            "survived": 0,
            "total": len(MUTANTS),
        }
    except Exception as exc:
        print(
            "MUTATION_DIAGNOSTIC="
            + json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, sort_keys=True
            ),
            file=sys.stderr,
        )
        print("MUTATION_GATE: FAIL", file=sys.stderr)
        return 1
    print("MUTATION_DIAGNOSTIC=" + json.dumps(diagnostic, sort_keys=True))
    print("MUTATION_GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
