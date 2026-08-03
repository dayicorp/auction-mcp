"""Contracts for deterministic distribution and installed-consumer gates."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_artifact.py"


def _load_artifact_gate():
    spec = importlib.util.spec_from_file_location("verify_artifact", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


artifact_gate = _load_artifact_gate()


def test_artifact_gate_source_allowlist_is_consumer_scoped():
    sources = set(artifact_gate.SOURCE_FILES)
    assert artifact_gate.RUNTIME_MODULES <= sources
    assert artifact_gate.RUNTIME_ASSETS <= sources
    assert all(not path.startswith("tests/") for path in sources)
    assert all(not path.startswith("scripts/") for path in sources)
    assert "pyproject.toml" in sources
    assert "build_backend.py" in sources
    assert "LICENSE" in sources


def test_artifact_gate_resolves_platform_specific_venv_python(tmp_path):
    assert artifact_gate.venv_python(tmp_path, "nt") == (
        tmp_path / "Scripts" / "python.exe"
    )
    assert artifact_gate.venv_python(tmp_path, "posix") == (
        tmp_path / "bin" / "python"
    )


def test_artifact_gate_requires_byte_identical_rebuilds(tmp_path):
    first_wheel = tmp_path / "first" / "auction_mcp-0.1.0-py3-none-any.whl"
    second_wheel = tmp_path / "second" / first_wheel.name
    first_sdist = tmp_path / "first" / "auction_mcp-0.1.0.tar.gz"
    second_sdist = tmp_path / "second" / first_sdist.name
    first_wheel.parent.mkdir()
    second_wheel.parent.mkdir()
    first_wheel.write_bytes(b"wheel-one")
    second_wheel.write_bytes(b"wheel-two")
    first_sdist.write_bytes(b"same-sdist")
    second_sdist.write_bytes(b"same-sdist")

    with pytest.raises(
        artifact_gate.ArtifactVerificationError, match="wheel bytes"
    ):
        artifact_gate.compare_reproducible(
            {"wheel": first_wheel, "sdist": first_sdist},
            {"wheel": second_wheel, "sdist": second_sdist},
        )


def test_artifact_gate_rejects_test_or_secret_archive_members():
    with pytest.raises(
        artifact_gate.ArtifactVerificationError, match="forbidden"
    ):
        artifact_gate._reject_forbidden_members(
            ["tests/test_live_ali.py", "secrets/private.pem"]
        )


def test_artifact_and_cleanroom_gates_emit_machine_diagnostics():
    artifact_source = SCRIPT_PATH.read_text(encoding="utf-8")
    cleanroom_source = (ROOT / "scripts" / "verify_cleanroom.py").read_text(
        encoding="utf-8"
    )
    assert "ARTIFACT_DIAGNOSTIC=" in artifact_source
    assert "ARTIFACT_VERIFICATION: PASS" in artifact_source
    assert 'diagnostic["temporary_environments_removed"] = True' in artifact_source
    assert "verify_artifact.py" in cleanroom_source
