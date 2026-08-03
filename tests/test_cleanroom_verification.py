"""Standard-library contracts for the clean-room bootstrapper."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_cleanroom.py"


def _load_cleanroom():
    spec = importlib.util.spec_from_file_location("verify_cleanroom", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanroom = _load_cleanroom()


def test_cleanroom_resolves_platform_specific_venv_python(tmp_path):
    assert cleanroom.venv_python(tmp_path, "nt") == (
        tmp_path / "Scripts" / "python.exe"
    )
    assert cleanroom.venv_python(tmp_path, "posix") == (
        tmp_path / "bin" / "python"
    )


def test_cleanroom_checked_process_accepts_success(tmp_path):
    cleanroom.run_checked(
        [sys.executable, "-c", "print('cleanroom-child-ok')"],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        label="success probe",
    )


def test_cleanroom_checked_process_rejects_nonzero_exit(tmp_path):
    with pytest.raises(cleanroom.CleanroomError, match="exit 7"):
        cleanroom.run_checked(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5,
            label="crash probe",
        )


def test_cleanroom_checked_process_terminates_timeout(tmp_path):
    with pytest.raises(cleanroom.CleanroomError, match="timed out"):
        cleanroom.run_checked(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=0.1,
            label="timeout probe",
        )
