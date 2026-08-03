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
    overall_timeout = 7 * (
        gate.CHAOS_RESPONSE_TIMEOUT_SECONDS + gate.CHAOS_EXIT_TIMEOUT_SECONDS
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "chaos"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=overall_timeout,
    )
    assert completed.returncode == 0, completed.stderr
    assert "RUNTIME_RELIABILITY: PASS" in completed.stdout
    assert '"lifecycle_count": 7' in completed.stdout
    assert '"network_blocked": true' in completed.stdout
    assert '"stdout_protocol_clean": true' in completed.stdout
    assert '"early_eof": "ready_then_bounded_exit"' in completed.stdout


def test_successful_lifecycle_unregisters_after_containment_check():
    with gate.GuardEnvironment() as guard:
        process = gate.RawMCPProcess(guard)
        gate.initialize(process)
        returncode, _ = process.finish()
        assert returncode == 0
        assert guard.active_process_count() == 0


@pytest.mark.parametrize(
    ("sequential", "workers", "per_worker", "rounds"),
    [(99, 8, 20, 2), (100, 7, 20, 2), (100, 8, 19, 2), (100, 8, 20, 1)],
)
def test_stress_gate_rejects_counts_below_acceptance_floor(
    sequential, workers, per_worker, rounds
):
    with pytest.raises(gate.ReliabilityError, match="acceptance floor"):
        gate.run_process_stress(
            None,
            sequential=sequential,
            workers=workers,
            per_worker=per_worker,
            rounds=rounds,
        )


def test_runtime_stress_contains_no_sleep_based_soak():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "time.sleep(" not in source
    assert "sequential < 100" in source
    assert "workers < 8" in source
    assert "per_worker < 20" in source
    assert "rounds < 2" in source
    assert "CHAOS_RESPONSE_TIMEOUT_SECONDS = 30.0" in source
    assert "CHAOS_EXIT_TIMEOUT_SECONDS = 10.0" in source
    assert "PROCESS_REAP_TIMEOUT_SECONDS = 1.0" in source
    assert "RUNTIME_STRESS_CHECKPOINT=" in source
    assert "stderr_sha256" in source
    assert gate.initialize.__kwdefaults__["timeout"] == 30.0


def test_parent_process_resource_counter_is_available():
    value = gate._resource_count()
    assert isinstance(value, int)
    assert value > 0


def test_process_containment_does_not_use_parent_pid_snapshots():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "CreateToolhelp32Snapshot" not in source
    assert "CreateJobObjectW" in source
    assert "QueryInformationJobObject" in source


def test_platform_process_containment_is_operational():
    if gate.os.name != "nt":
        # Stay inside pid_t so Linux reaches the intended ESRCH path.
        assert gate._lingering_process_groups({2_147_483_647}) == set()
        return

    job = gate._WindowsJob()
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
    )
    try:
        job.assign(process)
        assert job.active_processes() == 1
        assert job.active_process_ids() == (process.pid,)
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert job.wait_for_empty(timeout=1.0) == ()
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _fake_resource_snapshot(stage: str, count: int) -> dict:
    return {
        "active_mcp_processes": 0,
        "mcp_io_threads": 0,
        "open_windows_job_handles": 0,
        "python_thread_count": 1,
        "python_thread_names": ["MainThread"],
        "resource_count": count,
        "resource_count_samples": [count, count, count],
        "stage": stage,
    }


def test_cold_start_resources_are_attributed_before_stable_gate(monkeypatch):
    snapshots = iter(
        [
            _fake_resource_snapshot("cold", 10),
            _fake_resource_snapshot("warmed", 29),
            _fake_resource_snapshot("after_round_1", 29),
            _fake_resource_snapshot("after_round_2", 29),
        ]
    )
    monkeypatch.setattr(gate, "_settled_resource_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(
        gate, "_warm_concurrency_runtime", lambda workers: [f"worker-{i}" for i in range(workers)]
    )
    monkeypatch.setattr(
        gate,
        "_run_stress_round",
        lambda *args, **kwargs: [(index, 0.1) for index in range(260)],
    )

    result = gate.run_process_stress(
        None, sequential=100, workers=8, per_worker=20, rounds=2
    )

    assert result["warmup_resource_delta"] == 19
    assert result["round_resource_deltas"] == [0, 0]
    assert result["parent_resource_delta"] == 0
    assert result["lifecycle_count"] == 520
    assert result["resource_growth_stable"] is True


def test_post_warmup_resource_growth_fails_even_below_legacy_threshold(monkeypatch):
    snapshots = iter(
        [
            _fake_resource_snapshot("cold", 10),
            _fake_resource_snapshot("warmed", 29),
            _fake_resource_snapshot("after_round_1", 30),
            _fake_resource_snapshot("after_round_2", 31),
        ]
    )
    monkeypatch.setattr(gate, "_settled_resource_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(gate, "_warm_concurrency_runtime", lambda workers: ["worker"] * workers)
    monkeypatch.setattr(
        gate,
        "_run_stress_round",
        lambda *args, **kwargs: [(index, 0.1) for index in range(260)],
    )

    with pytest.raises(gate.ReliabilityError, match="continued growing"):
        gate.run_process_stress(
            None, sequential=100, workers=8, per_worker=20, rounds=2
        )
