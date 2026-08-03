"""Contracts for the external-cwd MCP consumer probe."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "consumer_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("consumer_probe", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def test_normalize_schema_preserves_type_nullability_default_and_required():
    schema = {
        "properties": {
            "names": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "null"},
                ],
                "default": None,
            },
            "page": {"type": "integer"},
        },
        "required": ["page"],
    }
    assert probe.normalize_input_schema(schema) == {
        "parameters": {
            "names": {
                "default": None,
                "nullable": True,
                "required": False,
                "type": "array[string]",
            },
            "page": {
                "nullable": False,
                "required": True,
                "type": "integer",
            },
        }
    }


def test_checked_contract_matches_current_server_schema():
    import server

    tools = asyncio.run(server.mcp.list_tools())
    expected = json.loads(
        (ROOT / "auction_mcp_assets" / "mcp_contract.json").read_text(
            encoding="utf-8"
        )
    )["tools"]
    assert probe.tool_contract(tools) == expected


def test_real_consumer_probe_runs_from_external_cwd(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--repo-root",
            str(ROOT),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    assert "CLEANROOM_CONSUMER: PASS" in completed.stdout
    assert '"network_blocked": true' in completed.stdout
    assert '"offline_calls": 6' in completed.stdout
    assert '"tools": 15' in completed.stdout
