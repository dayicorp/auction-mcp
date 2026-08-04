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
        "analyze_auction_asset",
        "ali_get_filter_options",
        "ali_get_supported_areas",
        "ali_pc_browser_close",
        "ali_pc_browser_start",
        "ali_pc_browser_status",
        "ali_pc_get_filter_options",
        "ali_pc_get_item_detail",
        "ali_pc_search_judicial",
        "ali_search_judicial",
        "beike_browser_status",
        "beike_get_xiaoqu_market",
        "beike_search_xiaoqu",
        "jd_get_supported_areas",
        "jd_search_judicial",
        "search_judicial",
    }


def test_release_gate_starts_real_stdio_server_and_lists_exact_tools():
    assert (
        release_gate.verify_mcp_stdio_startup()
        == release_gate.EXPECTED_MCP_TOOLS
    )


def test_release_gate_stdio_startup_timeout_fails_closed(monkeypatch):
    async def never_ready():
        import asyncio

        await asyncio.sleep(1)
        return release_gate.EXPECTED_MCP_TOOLS

    monkeypatch.setattr(release_gate, "_stdio_registered_tool_names", never_ready)

    with pytest.raises(release_gate.VerificationError, match="timed out"):
        release_gate.verify_mcp_stdio_startup(timeout_seconds=0.01)


def test_release_gate_stdio_tool_drift_fails_closed(monkeypatch):
    async def incomplete_manifest():
        return {"search_judicial"}

    monkeypatch.setattr(
        release_gate, "_stdio_registered_tool_names", incomplete_manifest
    )

    with pytest.raises(
        release_gate.VerificationError, match="stdio tool registry mismatch"
    ):
        release_gate.verify_mcp_stdio_startup()


def test_release_gate_dependency_contract_matches_repository():
    assert release_gate._normalized_requirements() == {
        "mcp>=1.0,<2",
        "httpx>=0.27",
        "playwright>=1.50,<2",
        "jsonschema>=4.20,<5",
        "pytest>=8.0",
        "coverage>=7.10,<8",
    }
    assert release_gate._normalized_build_requirements() == {
        "build==1.5.0",
        "setuptools==83.0.0",
        "wheel==0.47.0",
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
        ".coverage",
        "coverage.json",
        "htmlcov/index.html",
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


def test_release_gate_enforces_deterministic_evidence_replay():
    result = release_gate.verify_deterministic_evidence(timeout_seconds=30.0)
    assert result == {
        "bundle_sha256": release_gate.EXPECTED_EVIDENCE_BUNDLE_SHA256,
        "report_sha256": release_gate.EXPECTED_EVIDENCE_REPORT_SHA256,
        "cross_process_runs": 2,
        "json_schema_validated": True,
        "network_calls": 0,
        "tamper_cases_rejected": 1,
    }


def test_release_and_cleanroom_gates_emit_machine_readable_diagnostics():
    release_source = (ROOT / "scripts" / "verify_release.py").read_text(
        encoding="utf-8"
    )
    cleanroom_source = (ROOT / "scripts" / "verify_cleanroom.py").read_text(
        encoding="utf-8"
    )
    assert "RELEASE_DIAGNOSTIC=" in release_source
    assert 'diagnostic["stage"]' in release_source
    assert "CLEANROOM_DIAGNOSTIC=" in cleanroom_source
    assert 'diagnostic["temporary_environment_removed"] = True' in cleanroom_source


def test_release_gate_rejects_mutable_action_references():
    workflow = """
steps:
  - uses: actions/checkout@v7
  - uses: owner/local-action@0123456789abcdef0123456789abcdef01234567
  - uses: ./local-action
  - uses: docker://python:3.14
"""
    assert release_gate.unpinned_action_uses(workflow) == ["actions/checkout@v7"]


def test_repository_actions_are_pinned_to_immutable_commits():
    assert release_gate.verify_github_actions_pinned(
        [PurePosixPath(".github/workflows/ci.yml")]
    ) == 1


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
    assert (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        in workflow
    )
    assert (
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
        in workflow
    )
    assert "persist-credentials: false" in workflow
    assert "PIP_NO_INPUT: \"1\"" in workflow
    assert "actions/checkout@v7" not in workflow
    assert "actions/setup-python@v7" not in workflow
    assert "python scripts/verify_cleanroom.py" in workflow
    assert "python scripts/verify_release.py" not in workflow
    assert "python scripts/runtime_chaos.py --mode all" in workflow
    assert "python scripts/coverage_gate.py" in workflow
    assert "python scripts/mutation_gate.py" in workflow
    assert "Runtime stress / ${{ matrix.os }}" in workflow
    assert "Mutation and coverage / Ubuntu / Python 3.12" in workflow
    assert "timeout-minutes: 45" in workflow
    assert "timeout-minutes: 35" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "python -m pip install --no-input -r requirements.txt" in workflow
    assert "--run-live" not in workflow
    assert "secrets." not in workflow


def test_dependabot_tracks_actions_and_python_dependencies_only():
    config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert config.count("package-ecosystem:") == 2
    assert "package-ecosystem: github-actions" in config
    assert "package-ecosystem: pip" in config
    assert config.count("interval: weekly") == 2
    assert config.count("target-branch: main") == 2
    assert config.count("open-pull-requests-limit: 5") == 2
