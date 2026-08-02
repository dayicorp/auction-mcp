"""Offline contracts for the release gate and GitHub Actions workflow."""
from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_release.py"


def _load_release_gate():
    spec = importlib.util.spec_from_file_location("verify_release", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_gate = _load_release_gate()


def test_release_gate_has_exact_public_mcp_manifest():
    assert release_gate.EXPECTED_MCP_TOOLS == {
        "ali_get_filter_options",
        "ali_get_supported_areas",
        "ali_pc_browser_close",
        "ali_pc_browser_start",
        "ali_pc_browser_status",
        "ali_pc_get_filter_options",
        "ali_pc_get_item_detail",
        "ali_pc_search_judicial",
        "ali_search_judicial",
        "jd_get_supported_areas",
        "jd_search_judicial",
        "search_judicial",
    }


def test_release_gate_dependency_contract_matches_repository():
    assert release_gate._normalized_requirements() == {
        "mcp>=1.0,<2",
        "httpx>=0.27",
        "playwright>=1.50,<2",
        "pytest>=8.0",
    }


@pytest.mark.parametrize(
    "path",
    [
        "cookies.json",
        ".env",
        "secrets/private.pem",
        "tests/__pycache__/test_example.pyc",
        "playwright_chromiumdev_profile-X/data.txt",
        "_agent_round3_copy/server.py",
    ],
)
def test_release_gate_rejects_forbidden_tracked_artifacts(path):
    with pytest.raises(release_gate.VerificationError):
        release_gate.verify_forbidden_tracked_paths([PurePosixPath(path)])


def test_release_gate_sensitive_scan_reports_kind_without_secret(tmp_path, monkeypatch):
    synthetic = tmp_path / "credential.txt"
    synthetic.write_text("github_pat_" + "A" * 24, encoding="utf-8")
    monkeypatch.setattr(release_gate, "ROOT", tmp_path)

    with pytest.raises(release_gate.VerificationError) as exc_info:
        release_gate.scan_sensitive_text([PurePosixPath("credential.txt")])

    message = str(exc_info.value)
    assert "github_token" in message
    assert "github_pat_" not in message


def test_release_gate_pytest_command_is_offline_only():
    command = release_gate.offline_pytest_command()
    assert command[-1] == "tests"
    assert "--run-live" not in command
    assert command[1:4] == ["-m", "pytest", "-q"]


def test_ci_workflow_covers_full_cross_platform_matrix_and_gate():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "fail-fast: false" in workflow
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    for version in ('"3.10"', '"3.12"', '"3.14"'):
        assert version in workflow
    assert "actions/checkout@v7" in workflow
    assert "actions/setup-python@v7" in workflow
    assert "python scripts/verify_release.py" in workflow
    assert "--run-live" not in workflow
    assert "secrets." not in workflow
