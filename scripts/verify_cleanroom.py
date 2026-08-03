"""Create an isolated environment and run the complete release gate.

The bootstrapper uses only the Python standard library.  It creates a fresh
temporary venv outside the repository, installs ``requirements.txt``, runs
``pip check``, and launches the release gate from an unrelated working folder.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Sequence
import venv


ROOT = Path(__file__).resolve().parents[1]


class CleanroomError(RuntimeError):
    """Raised when clean-room preparation or verification fails."""


def venv_python(venv_root: Path, platform_name: str | None = None) -> Path:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    label: str,
) -> None:
    print(f"[RUN] {label}", flush=True)
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanroomError(
            f"{label} timed out after {timeout_seconds:g}s"
        ) from exc
    if completed.returncode != 0:
        raise CleanroomError(f"{label} failed with exit {completed.returncode}")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="auction-mcp-cleanroom-") as temp:
            clean_root = Path(temp).resolve()
            environment_root = clean_root / "venv"
            consumer_cwd = clean_root / "consumer-cwd"
            consumer_cwd.mkdir()

            print("[RUN] create isolated venv", flush=True)
            venv.EnvBuilder(with_pip=True, clear=True).create(environment_root)
            python = venv_python(environment_root)
            if not python.is_file():
                raise CleanroomError("clean-room Python was not created")
            repository_venv = (ROOT / ".venv").resolve()
            if python.resolve() == venv_python(repository_venv).resolve():
                raise CleanroomError("repository .venv reuse is forbidden")

            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.update(
                {
                    "AUCTION_MCP_CLEANROOM": "1",
                    "AUCTION_MCP_CLEANROOM_ROOT": str(clean_root),
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_NO_INPUT": "1",
                    "PIP_REQUIRE_VIRTUALENV": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTEST_ADDOPTS": "",
                    "VIRTUAL_ENV": str(environment_root),
                }
            )

            run_checked(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "-r",
                    str(ROOT / "requirements.txt"),
                ],
                cwd=consumer_cwd,
                env=env,
                timeout_seconds=300,
                label="install requirements into clean-room venv",
            )
            run_checked(
                [str(python), "-m", "pip", "check"],
                cwd=consumer_cwd,
                env=env,
                timeout_seconds=60,
                label="pip check",
            )
            run_checked(
                [str(python), str(ROOT / "scripts" / "verify_release.py")],
                cwd=consumer_cwd,
                env=env,
                timeout_seconds=300,
                label="complete release gate from external cwd",
            )
    except (OSError, subprocess.SubprocessError, CleanroomError) as exc:
        print(
            f"CLEANROOM_VERIFICATION: FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("CLEANROOM_VERIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
