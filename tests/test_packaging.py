"""发布依赖契约测试（零网络）。"""

from pathlib import Path


def test_mcp_requirement_keeps_fastmcp_compatible_major():
    """server.py 仍使用 MCP 1.x 的 mcp.server.fastmcp 导入路径。"""
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()

    normalized = {line.strip().replace(" ", "") for line in requirements}
    assert "mcp>=1.0,<2" in normalized
