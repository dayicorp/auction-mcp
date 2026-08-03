"""Cross-platform offline release gate for auction-mcp.

The gate intentionally uses only repository files and the installed Python
environment.  It never enables ``--run-live`` and never starts a browser.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import py_compile
import re
import subprocess
import sys
import tempfile
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_REQUIREMENTS = {
    "mcp>=1.0,<2",
    "httpx>=0.27",
    "playwright>=1.50,<2",
    "pytest>=8.0",
    "coverage>=7.10,<8",
}

EXPECTED_MCP_TOOLS = {
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

MCP_STDIO_STARTUP_TIMEOUT_SECONDS = 30.0

FORBIDDEN_PATH_PARTS = {
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    ".venv",
    ".workbuddy",
    "__pycache__",
    "node_modules",
    "playwright_chromiumdev_profile",
    "venv",
}

FORBIDDEN_FILE_NAMES = {
    "cookies.json",
    "storage_state.json",
    ".coverage",
    "coverage.json",
}

FORBIDDEN_SUFFIXES = {
    ".har",
    ".key",
    ".pem",
    ".pyc",
    ".pyo",
}

SENSITIVE_PATTERNS = {
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "openai_api_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "slack_token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "cn_mobile_number": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}

ACTION_USE_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses:\s*([^#\s]+)",
    re.MULTILINE,
)
FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class VerificationError(RuntimeError):
    """Raised when a release contract is not satisfied."""


def _run(command: Sequence[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        list(command),
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise VerificationError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
        )
    return ""


def tracked_files() -> list[PurePosixPath]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise VerificationError("git ls-files failed")
    return [
        PurePosixPath(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    ]


def verify_python_compilation(paths: Iterable[PurePosixPath]) -> int:
    python_paths = sorted(path for path in paths if path.suffix == ".py")
    if not python_paths:
        raise VerificationError("no tracked Python files found")

    with tempfile.TemporaryDirectory(prefix="auction-mcp-compile-") as temp_dir:
        output_root = Path(temp_dir)
        for relative in python_paths:
            source = ROOT / Path(*relative.parts)
            target = output_root / Path(*relative.parts).with_suffix(".pyc")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                py_compile.compile(
                    str(source), cfile=str(target), doraise=True
                )
            except py_compile.PyCompileError as exc:
                raise VerificationError(
                    f"Python compilation failed: {relative}: {exc.msg}"
                ) from exc
    return len(python_paths)


def _normalized_requirements() -> set[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return {
        line.split("#", 1)[0].strip().replace(" ", "")
        for line in lines
        if line.split("#", 1)[0].strip()
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    if not match:
        raise VerificationError(f"unparseable installed version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def verify_dependency_contract() -> dict[str, str]:
    actual_requirements = _normalized_requirements()
    if actual_requirements != EXPECTED_REQUIREMENTS:
        raise VerificationError(
            "requirements contract mismatch: "
            f"expected={sorted(EXPECTED_REQUIREMENTS)!r}, "
            f"actual={sorted(actual_requirements)!r}"
        )

    versions = {
        name: importlib.metadata.version(name)
        for name in ("mcp", "httpx", "playwright", "pytest", "coverage")
    }
    if _version_tuple(versions["mcp"])[0] != 1:
        raise VerificationError(f"MCP must remain on 1.x, got {versions['mcp']}")
    if _version_tuple(versions["httpx"]) < (0, 27):
        raise VerificationError(f"httpx must be >=0.27, got {versions['httpx']}")
    if not ((1, 50) <= _version_tuple(versions["playwright"]) < (2,)):
        raise VerificationError(
            f"playwright must be >=1.50,<2, got {versions['playwright']}"
        )
    if _version_tuple(versions["pytest"]) < (8,):
        raise VerificationError(f"pytest must be >=8, got {versions['pytest']}")
    if not ((7, 10) <= _version_tuple(versions["coverage"]) < (8,)):
        raise VerificationError(
            f"coverage must be >=7.10,<8, got {versions['coverage']}"
        )
    return versions


def verify_cleanroom_isolation() -> bool:
    """Validate the clean-room marker when the outer bootstrapper is active."""
    if os.environ.get("AUCTION_MCP_CLEANROOM") != "1":
        return False
    raw_root = os.environ.get("AUCTION_MCP_CLEANROOM_ROOT")
    if not raw_root:
        raise VerificationError("clean-room root marker is missing")
    clean_root = Path(raw_root).resolve()
    interpreter_prefix = Path(sys.prefix).resolve()
    repository_venv = (ROOT / ".venv").resolve()
    if interpreter_prefix == repository_venv:
        raise VerificationError("repository .venv reuse is forbidden")
    if (
        clean_root != interpreter_prefix
        and clean_root not in interpreter_prefix.parents
    ):
        raise VerificationError("interpreter is outside the declared clean room")
    working_directory = Path.cwd().resolve()
    if working_directory == ROOT or ROOT in working_directory.parents:
        raise VerificationError("release gate must start from an external cwd")
    return True


def verify_forbidden_tracked_paths(paths: Iterable[PurePosixPath]) -> None:
    failures: list[str] = []
    for path in paths:
        lower_parts = {part.lower() for part in path.parts}
        name = path.name.lower()
        has_forbidden_prefix = any(
            part.lower().startswith("playwright_chromiumdev_profile-")
            or part.lower().startswith("_agent_round3_")
            for part in path.parts
        )
        if lower_parts & FORBIDDEN_PATH_PARTS or has_forbidden_prefix:
            failures.append(str(path))
        elif name in FORBIDDEN_FILE_NAMES:
            failures.append(str(path))
        elif path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(str(path))
        elif name == ".env" or name.startswith(".env."):
            failures.append(str(path))
    if failures:
        raise VerificationError(
            f"forbidden tracked artifacts: {sorted(failures)!r}"
        )


def scan_sensitive_text(paths: Iterable[PurePosixPath]) -> None:
    findings: list[str] = []
    for relative in paths:
        absolute = ROOT / Path(*relative.parts)
        data = absolute.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for pattern_name, pattern in SENSITIVE_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}:{pattern_name}")
    if findings:
        raise VerificationError(f"sensitive text detected: {findings!r}")


def unpinned_action_uses(text: str) -> list[str]:
    """Return external GitHub Action references that are not immutable SHAs."""
    failures: list[str] = []
    for action_ref in ACTION_USE_PATTERN.findall(text):
        if action_ref.startswith("./") or action_ref.startswith("docker://"):
            continue
        if "@" not in action_ref:
            failures.append(action_ref)
            continue
        revision = action_ref.rsplit("@", 1)[1]
        if not FULL_COMMIT_SHA_PATTERN.fullmatch(revision):
            failures.append(action_ref)
    return failures


def verify_github_actions_pinned(paths: Iterable[PurePosixPath]) -> int:
    workflows = sorted(
        path
        for path in paths
        if len(path.parts) >= 3
        and path.parts[:2] == (".github", "workflows")
        and path.suffix.lower() in {".yaml", ".yml"}
    )
    if not workflows:
        raise VerificationError("no tracked GitHub Actions workflows found")

    failures: list[str] = []
    for relative in workflows:
        text = (ROOT / Path(*relative.parts)).read_text(encoding="utf-8")
        failures.extend(
            f"{relative}:{action_ref}"
            for action_ref in unpinned_action_uses(text)
        )
    if failures:
        raise VerificationError(f"unpinned GitHub Actions: {failures!r}")
    return len(workflows)


async def _registered_tool_names() -> set[str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import server

    tools = await server.mcp.list_tools()
    return {tool.name for tool in tools}


def verify_mcp_tools() -> set[str]:
    actual = asyncio.run(_registered_tool_names())
    if actual != EXPECTED_MCP_TOOLS:
        raise VerificationError(
            "MCP tool registry mismatch: "
            f"missing={sorted(EXPECTED_MCP_TOOLS - actual)!r}, "
            f"unexpected={sorted(actual - EXPECTED_MCP_TOOLS)!r}"
        )
    return actual


async def _stdio_registered_tool_names() -> set[str]:
    """Start the real MCP entrypoint and list its tools over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=ROOT,
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
    return {tool.name for tool in tools.tools}


def verify_mcp_stdio_startup(
    timeout_seconds: float = MCP_STDIO_STARTUP_TIMEOUT_SECONDS,
) -> set[str]:
    """Verify process startup, MCP initialization, and the public tool manifest."""
    try:
        actual = asyncio.run(
            asyncio.wait_for(
                _stdio_registered_tool_names(), timeout=timeout_seconds
            )
        )
    except asyncio.TimeoutError as exc:
        raise VerificationError(
            f"MCP stdio startup timed out after {timeout_seconds:g}s"
        ) from exc
    except Exception as exc:
        raise VerificationError(
            f"MCP stdio startup failed: {type(exc).__name__}: {exc}"
        ) from exc

    if actual != EXPECTED_MCP_TOOLS:
        raise VerificationError(
            "MCP stdio tool registry mismatch: "
            f"missing={sorted(EXPECTED_MCP_TOOLS - actual)!r}, "
            f"unexpected={sorted(actual - EXPECTED_MCP_TOOLS)!r}"
        )
    return actual


def verify_consumer_probe(timeout_seconds: float = 45.0) -> None:
    """Run the independent protocol consumer from outside the repository."""
    with tempfile.TemporaryDirectory(prefix="auction-mcp-consumer-cwd-") as temp:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTEST_ADDOPTS"] = ""
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "consumer_probe.py"),
                    "--repo-root",
                    str(ROOT),
                    "--contract",
                    str(ROOT / "mcp_contract.json"),
                ],
                cwd=temp,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError(
                f"clean-room consumer timed out after {timeout_seconds:g}s"
            ) from exc
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.returncode != 0:
        detail_lines = (completed.stderr or completed.stdout).strip().splitlines()
        detail = detail_lines[-1] if detail_lines else "no diagnostic output"
        raise VerificationError(
            f"clean-room consumer failed ({completed.returncode}): {detail}"
        )


def offline_pytest_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests",
    ]


def run_offline_tests() -> None:
    env = os.environ.copy()
    # Do not inherit a user or CI setting that could silently append --run-live.
    env["PYTEST_ADDOPTS"] = ""
    _run(offline_pytest_command(), env=env)


def main() -> int:
    diagnostic: dict[str, object] = {"stage": "startup"}
    try:
        diagnostic["stage"] = "cleanroom_isolation"
        if verify_cleanroom_isolation():
            diagnostic["cleanroom_isolated"] = True
            print("[PASS] clean-room interpreter and external cwd isolation")

        diagnostic["stage"] = "tracked_artifacts"
        paths = tracked_files()
        verify_forbidden_tracked_paths(paths)
        diagnostic["repository_files"] = len(paths)
        print(f"[PASS] forbidden tracked artifacts ({len(paths)} files)")

        diagnostic["stage"] = "sensitive_scan"
        scan_sensitive_text(paths)
        diagnostic["sensitive_scan"] = "pass"
        print("[PASS] sensitive information scan")

        diagnostic["stage"] = "github_actions"
        workflow_count = verify_github_actions_pinned(paths)
        diagnostic["immutable_workflows"] = workflow_count
        print(f"[PASS] immutable GitHub Actions ({workflow_count} workflows)")

        diagnostic["stage"] = "dependencies"
        versions = verify_dependency_contract()
        diagnostic["dependencies"] = versions
        rendered_versions = ", ".join(
            f"{name}={version}" for name, version in sorted(versions.items())
        )
        print(f"[PASS] dependency contract ({rendered_versions})")

        diagnostic["stage"] = "python_compilation"
        python_count = verify_python_compilation(paths)
        diagnostic["python_files_compiled"] = python_count
        print(f"[PASS] Python compilation ({python_count} tracked files)")

        diagnostic["stage"] = "mcp_registry"
        tools = verify_mcp_tools()
        diagnostic["mcp_tools"] = len(tools)
        print(f"[PASS] MCP registry ({len(tools)} exact tools)")

        diagnostic["stage"] = "mcp_stdio"
        stdio_tools = verify_mcp_stdio_startup()
        diagnostic["mcp_stdio_tools"] = len(stdio_tools)
        print(
            "[PASS] MCP stdio startup "
            f"(initialize + {len(stdio_tools)} exact tools)"
        )

        diagnostic["stage"] = "consumer_probe"
        verify_consumer_probe()
        diagnostic["consumer_probe"] = "pass"
        print("[PASS] clean-room MCP consumer contract and lifecycle")

        diagnostic["stage"] = "offline_pytest"
        run_offline_tests()
        diagnostic["offline_pytest"] = "pass"
        print("[PASS] full offline pytest suite")
    except (OSError, VerificationError) as exc:
        diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        print(
            "RELEASE_DIAGNOSTIC="
            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        print(f"RELEASE_VERIFICATION: FAIL: {exc}", file=sys.stderr)
        return 1

    diagnostic["stage"] = "complete"
    print(
        "RELEASE_DIAGNOSTIC="
        + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    )
    print("RELEASE_VERIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
