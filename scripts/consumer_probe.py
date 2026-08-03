"""End-to-end offline MCP consumer probe for a clean checkout.

This script is deliberately runnable from outside the repository.  It starts
the real ``server.py`` over stdio, verifies the public schema contract, calls
only offline-safe tools, and exercises crash and timeout lifecycle handling.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_STDERR_BYTES = 64 * 1024
FORBIDDEN_STDERR_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"_m_h5_tk"),
    re.compile(r"storage_state", re.IGNORECASE),
)

NETWORK_GUARD_SOURCE = """\
import socket

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection

def _is_local(address):
    if isinstance(address, str):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    return address[0] in {"127.0.0.1", "::1", "localhost"}

def _network_disabled(address):
    raise RuntimeError("network disabled by clean-room consumer")

def _guarded_connect(self, address):
    if _is_local(address):
        return _original_connect(self, address)
    return _network_disabled(address)

def _guarded_connect_ex(self, address):
    if _is_local(address):
        return _original_connect_ex(self, address)
    return _network_disabled(address)

def _guarded_create_connection(address, *args, **kwargs):
    if _is_local(address):
        return _original_create_connection(address, *args, **kwargs)
    return _network_disabled(address)

socket.create_connection = _guarded_create_connection
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
"""


class ConsumerProbeError(RuntimeError):
    """Raised when a clean-room consumer contract is not satisfied."""


def _normalized_type(specification: dict[str, Any]) -> tuple[str, bool]:
    options = specification.get("anyOf")
    if options is None:
        concrete = specification
        nullable = specification.get("type") == "null"
    else:
        nullable = any(option.get("type") == "null" for option in options)
        concrete_options = [
            option for option in options if option.get("type") != "null"
        ]
        if len(concrete_options) != 1:
            raise ConsumerProbeError(
                f"unsupported schema union: {specification!r}"
            )
        concrete = concrete_options[0]

    parameter_type = concrete.get("type")
    if parameter_type == "array":
        item_type = (concrete.get("items") or {}).get("type")
        if not item_type:
            raise ConsumerProbeError(
                f"array schema has no item type: {specification!r}"
            )
        parameter_type = f"array[{item_type}]"
    if not parameter_type or parameter_type == "null":
        raise ConsumerProbeError(f"schema has no concrete type: {specification!r}")
    return parameter_type, nullable


def normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove generator-only titles while preserving the public contract."""
    required = set(schema.get("required") or [])
    parameters: dict[str, Any] = {}
    for name, specification in sorted((schema.get("properties") or {}).items()):
        parameter_type, nullable = _normalized_type(specification)
        normalized: dict[str, Any] = {
            "nullable": nullable,
            "required": name in required,
            "type": parameter_type,
        }
        if "default" in specification:
            normalized["default"] = specification["default"]
        parameters[name] = normalized
    return {"parameters": parameters}


def tool_contract(tools: Iterable[Any]) -> dict[str, Any]:
    return {
        tool.name: normalize_input_schema(tool.inputSchema)
        for tool in sorted(tools, key=lambda item: item.name)
    }


def load_contract(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsumerProbeError(f"cannot read MCP contract: {exc}") from exc
    if document.get("version") != 1 or not isinstance(document.get("tools"), dict):
        raise ConsumerProbeError("unsupported MCP contract document")
    return document["tools"]


def _assert_contract(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    actual_names = set(actual)
    expected_names = set(expected)
    if actual_names != expected_names:
        raise ConsumerProbeError(
            "MCP tool names drifted: "
            f"missing={sorted(expected_names - actual_names)!r}, "
            f"unexpected={sorted(actual_names - expected_names)!r}"
        )
    for name in sorted(expected):
        if actual[name] != expected[name]:
            raise ConsumerProbeError(
                f"MCP input schema drifted for {name}: "
                f"expected={expected[name]!r}, actual={actual[name]!r}"
            )


def _json_tool_result(result: Any, tool_name: str) -> dict[str, Any]:
    if result.isError:
        raise ConsumerProbeError(f"offline-safe call failed: {tool_name}")
    text_parts = [
        item.text for item in result.content if getattr(item, "type", None) == "text"
    ]
    if len(text_parts) != 1:
        raise ConsumerProbeError(f"unexpected MCP content for {tool_name}")
    try:
        value = json.loads(text_parts[0])
    except json.JSONDecodeError as exc:
        raise ConsumerProbeError(f"non-JSON MCP result for {tool_name}") from exc
    if not isinstance(value, dict):
        raise ConsumerProbeError(f"non-object MCP result for {tool_name}")
    return value


def exception_type_names(exc: BaseException) -> set[str]:
    names = {type(exc).__name__}
    for nested in getattr(exc, "exceptions", ()):  # ExceptionGroup on Python 3.11+
        names.update(exception_type_names(nested))
    return names


def verify_network_guard(env: dict[str, str], working_directory: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket; "
                "socket.create_connection(('203.0.113.1', 443), timeout=1)"
            ),
        ],
        cwd=working_directory,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    if completed.returncode == 0 or "network disabled by clean-room consumer" not in (
        completed.stderr or ""
    ):
        raise ConsumerProbeError("external network guard self-test failed")


async def _expect_initialization_failure(
    source: str,
    *,
    timeout_seconds: float,
    expected_type: str,
    working_directory: Path,
) -> float:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", source],
        cwd=working_directory,
    )
    started = time.monotonic()
    try:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(
                        session.initialize(), timeout=timeout_seconds
                    )
    except BaseException as exc:
        names = exception_type_names(exc)
        if expected_type not in names:
            raise ConsumerProbeError(
                f"expected {expected_type} lifecycle failure, got {sorted(names)!r}"
            ) from exc
    else:
        raise ConsumerProbeError(
            f"failure probe unexpectedly initialized: {expected_type}"
        )
    return time.monotonic() - started


async def run_probe(repo_root: Path, contract_path: Path) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo_root = repo_root.resolve()
    consumer_cwd = Path.cwd().resolve()
    if consumer_cwd == repo_root or repo_root in consumer_cwd.parents:
        raise ConsumerProbeError("consumer probe must run outside the repository")
    expected_contract = load_contract(contract_path)

    with tempfile.TemporaryDirectory(prefix="auction-mcp-consumer-stderr-") as temp:
        guard_root = Path(temp)
        stderr_path = guard_root / "server.stderr.log"
        (guard_root / "sitecustomize.py").write_text(
            NETWORK_GUARD_SOURCE, encoding="utf-8"
        )
        server_env = os.environ.copy()
        server_env["PYTHONPATH"] = str(guard_root)
        verify_network_guard(server_env, consumer_cwd)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(repo_root / "server.py")],
            cwd=consumer_cwd,
            env=server_env,
        )
        primary_error: BaseException | None = None
        with stderr_path.open("w", encoding="utf-8") as errlog:
            try:
                async with stdio_client(parameters, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tools_result = await session.list_tools()
                        actual_contract = tool_contract(tools_result.tools)
                        _assert_contract(actual_contract, expected_contract)

                        ali_areas = _json_tool_result(
                            await session.call_tool("ali_get_supported_areas", {}),
                            "ali_get_supported_areas",
                        )
                        jd_areas = _json_tool_result(
                            await session.call_tool("jd_get_supported_areas", {}),
                            "jd_get_supported_areas",
                        )
                        hierarchy_error = _json_tool_result(
                            await session.call_tool(
                                "search_judicial", {"city": "广州市"}
                            ),
                            "search_judicial",
                        )
                        browser_status = _json_tool_result(
                            await session.call_tool("ali_pc_browser_status", {}),
                            "ali_pc_browser_status",
                        )
                        missing_item_id = await session.call_tool(
                            "ali_pc_get_item_detail", {}
                        )
            except BaseException as exc:
                primary_error = exc

        stderr_data = stderr_path.read_bytes()
        if primary_error is not None:
            diagnostic = stderr_data.decode("utf-8", errors="replace").strip()
            if len(diagnostic) > 2000:
                diagnostic = diagnostic[-2000:]
            raise ConsumerProbeError(
                "MCP server failed under the network guard: "
                f"{diagnostic or sorted(exception_type_names(primary_error))!r}"
            ) from primary_error

    if ali_areas.get("total") != {
        "provinces": 31,
        "cities": 342,
        "districts": 3056,
    }:
        raise ConsumerProbeError("Ali static area contract drifted")
    if jd_areas.get("total") != {
        "provinces": 33,
        "cities": 455,
        "districts": 5344,
    }:
        raise ConsumerProbeError("JD static area contract drifted")
    if hierarchy_error.get("error") != "city_requires_province":
        raise ConsumerProbeError("invalid hierarchy did not fail closed")
    if browser_status != {
        "state": "stopped",
        "cookie_policy": "browser_memory_only",
    }:
        raise ConsumerProbeError("consumer probe unexpectedly touched browser state")
    if not missing_item_id.isError:
        raise ConsumerProbeError("missing required item_id did not fail validation")
    if len(stderr_data) > MAX_STDERR_BYTES:
        raise ConsumerProbeError("MCP server stderr exceeded safety limit")
    stderr_text = stderr_data.decode("utf-8", errors="replace")
    if any(pattern.search(stderr_text) for pattern in FORBIDDEN_STDERR_PATTERNS):
        raise ConsumerProbeError("MCP server stderr violated safety boundary")

    crash_seconds = await _expect_initialization_failure(
        "import sys; sys.exit(23)",
        timeout_seconds=5.0,
        expected_type="McpError",
        working_directory=consumer_cwd,
    )
    timeout_seconds = await _expect_initialization_failure(
        "import time; time.sleep(60)",
        timeout_seconds=0.2,
        expected_type="TimeoutError",
        working_directory=consumer_cwd,
    )
    if crash_seconds > 10 or timeout_seconds > 10:
        raise ConsumerProbeError("MCP failure cleanup exceeded lifecycle limit")

    return {
        "browser_started": False,
        "consumer_cwd_outside_repo": True,
        "crash_failure_seconds": round(crash_seconds, 3),
        "mcp_protocol": initialized.protocolVersion,
        "network_blocked": True,
        "offline_calls": 5,
        "stderr_bytes": len(stderr_data),
        "timeout_failure_seconds": round(timeout_seconds, 3),
        "tools": len(expected_contract),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--contract", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    repo_root = arguments.repo_root.resolve()
    contract = (arguments.contract or repo_root / "mcp_contract.json").resolve()
    try:
        result = asyncio.run(
            asyncio.wait_for(
                run_probe(repo_root, contract), timeout=DEFAULT_TIMEOUT_SECONDS
            )
        )
    except Exception as exc:
        print(
            f"CLEANROOM_CONSUMER: FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("CLEANROOM_CONSUMER: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
