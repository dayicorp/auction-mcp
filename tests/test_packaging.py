"""发布与安装元数据契约测试（零网络）。"""

import json
from pathlib import Path


def test_mcp_requirement_keeps_fastmcp_compatible_major():
    """server.py 仍使用 MCP 1.x 的 mcp.server.fastmcp 导入路径。"""
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()

    normalized = {line.strip().replace(" ", "") for line in requirements}
    assert "mcp>=1.0,<2" in normalized


def test_pyproject_exposes_supported_console_entrypoint_and_runtime_only_deps():
    pyproject = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert 'auction-mcp = "server:main"' in pyproject
    assert 'auction-evidence = "evidence_cli:main"' in pyproject
    for module in ("evidence_bundle", "evidence_cli", "evidence_safety"):
        assert f'"{module}"' in pyproject
    assert '"mcp>=1.0,<2"' in pyproject
    assert '"httpx>=0.27"' in pyproject
    assert '"playwright>=1.50,<2"' in pyproject
    assert '"jsonschema>=4.20,<5"' in pyproject
    assert "pytest" not in pyproject
    assert "coverage" not in pyproject


def test_packaged_contract_and_region_assets_are_valid_json():
    assets = Path(__file__).resolve().parents[1] / "auction_mcp_assets"
    expected = {
        "gb2260.json",
        "gb2260_200712.json",
        "evidence_bundle_schema.json",
        "jd_areas.json",
        "mcp_contract.json",
    }
    assert {path.name for path in assets.glob("*.json")} == expected
    for name in expected:
        assert json.loads((assets / name).read_text(encoding="utf-8"))
