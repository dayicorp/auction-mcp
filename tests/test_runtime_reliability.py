"""Regression tests for the raw MCP runtime reliability gate."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runtime_chaos.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("runtime_chaos", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def test_raw_protocol_chaos_runs_in_real_external_processes():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "chaos"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    assert "RUNTIME_RELIABILITY: PASS" in completed.stdout
    assert '"lifecycle_count": 7' in completed.stdout
    assert '"network_blocked": true' in completed.stdout
    assert '"stdout_protocol_clean": true' in completed.stdout


@pytest.mark.parametrize(
    ("sequential", "workers", "per_worker"),
    [(99, 8, 20), (100, 7, 20), (100, 8, 19)],
)
def test_stress_gate_rejects_counts_below_acceptance_floor(
    sequential, workers, per_worker
):
    with pytest.raises(gate.ReliabilityError, match="acceptance floor"):
        gate.run_process_stress(
            None,
            sequential=sequential,
            workers=workers,
            per_worker=per_worker,
        )


def test_runtime_stress_contains_no_sleep_based_soak():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "time.sleep(" not in source
    assert "sequential < 100" in source
    assert "workers < 8" in source
    assert "per_worker < 20" in source
    assert "CHAOS_EXIT_TIMEOUT_SECONDS = 10.0" in source


def test_parent_process_resource_counter_is_available():
    value = gate._resource_count()
    assert isinstance(value, int)
    assert value > 0


def test_process_tree_check_has_no_false_positive_for_impossible_parent():
    assert gate._lingering_child_processes({sys.maxsize}) == set()
