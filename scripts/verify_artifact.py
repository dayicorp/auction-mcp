"""Build, audit, reproduce, install, and consume auction-mcp artifacts.

The verifier copies an explicit source allowlist into two independent trees,
builds wheel and sdist twice with a fixed epoch, compares byte hashes, audits
archive contents and metadata, then installs the wheel into a third fresh venv.
The installed console entrypoint is finally exercised by the offline MCP
consumer probe from a working directory outside the source checkout.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable, Sequence
import venv
import zipfile

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATE_EPOCH = "1704067200"
BUILD_TOOL_VERSIONS = {
    "build": "1.5.0",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
}
SOURCE_FILES = (
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "ali_h5_client.py",
    "ali_pc_browser_client.py",
    "beike_browser_client.py",
    "auction_mcp_assets/__init__.py",
    "auction_mcp_assets/gb2260.json",
    "auction_mcp_assets/gb2260_200712.json",
    "auction_mcp_assets/jd_areas.json",
    "auction_mcp_assets/mcp_contract.json",
    "build_backend.py",
    "jd_h5_client.py",
    "pyproject.toml",
    "requirements-build.txt",
    "requirements.txt",
    "safety_core.py",
    "server.py",
)
RUNTIME_MODULES = {
    "ali_h5_client.py",
    "ali_pc_browser_client.py",
    "beike_browser_client.py",
    "jd_h5_client.py",
    "safety_core.py",
    "server.py",
}
RUNTIME_ASSETS = {
    "auction_mcp_assets/__init__.py",
    "auction_mcp_assets/gb2260.json",
    "auction_mcp_assets/gb2260_200712.json",
    "auction_mcp_assets/jd_areas.json",
    "auction_mcp_assets/mcp_contract.json",
}
EXPECTED_RUNTIME_REQUIREMENTS = {
    ("httpx", ">=0.27"),
    ("mcp", "<2,>=1.0"),
    ("playwright", "<2,>=1.50"),
}
FORBIDDEN_ARCHIVE_PARTS = {
    ".coverage",
    ".env",
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "cookies.json",
    "htmlcov",
    "scripts",
    "storage_state.json",
    "tests",
}


class ArtifactVerificationError(RuntimeError):
    """Raised when a distribution or installed consumer violates its contract."""


def venv_python(venv_root: Path, platform_name: str | None = None) -> Path:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    label: str,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"[RUN] {label}", flush=True)
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=False,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactVerificationError(
            f"{label} timed out after {timeout_seconds:g}s"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ArtifactVerificationError(
            f"{label} failed with exit {completed.returncode}{suffix}"
        )
    return completed


def verify_build_tool_versions() -> None:
    from importlib.metadata import version

    actual = {name: version(name) for name in BUILD_TOOL_VERSIONS}
    if actual != BUILD_TOOL_VERSIONS:
        raise ArtifactVerificationError(
            f"build tool contract mismatch: expected={BUILD_TOOL_VERSIONS!r}, "
            f"actual={actual!r}"
        )


def copy_build_source(destination: Path) -> None:
    for relative_text in SOURCE_FILES:
        relative = PurePosixPath(relative_text)
        source = ROOT / Path(*relative.parts)
        target = destination / Path(*relative.parts)
        if not source.is_file():
            raise ArtifactVerificationError(f"required build source missing: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def build_distributions(source: Path, output: Path, env: dict[str, str]) -> dict[str, Path]:
    run_checked(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(output),
            str(source),
        ],
        cwd=source,
        env=env,
        timeout_seconds=180,
        label=f"build wheel and sdist in {source.name}",
    )
    artifacts = sorted(path for path in output.iterdir() if path.is_file())
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        raise ArtifactVerificationError(
            f"expected one wheel and one sdist, got {[path.name for path in artifacts]!r}"
        )
    return {"wheel": wheels[0], "sdist": sdists[0]}


def _reject_forbidden_members(members: Iterable[str]) -> None:
    failures: list[str] = []
    for member in members:
        path = PurePosixPath(member)
        lower_parts = {part.lower() for part in path.parts}
        if lower_parts & FORBIDDEN_ARCHIVE_PARTS:
            failures.append(member)
        if path.suffix.lower() in {".har", ".key", ".pem", ".pyc", ".pyo"}:
            failures.append(member)
    if failures:
        raise ArtifactVerificationError(
            f"forbidden distribution members: {sorted(set(failures))!r}"
        )


def inspect_wheel(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        _reject_forbidden_members(names)
        missing_runtime = (RUNTIME_MODULES | RUNTIME_ASSETS) - names
        if missing_runtime:
            raise ArtifactVerificationError(
                f"wheel runtime files missing: {sorted(missing_runtime)!r}"
            )
        dist_info_roots = {
            name.split("/", 1)[0]
            for name in names
            if ".dist-info/" in name
        }
        if len(dist_info_roots) != 1:
            raise ArtifactVerificationError("wheel must have exactly one dist-info root")
        dist_info = next(iter(dist_info_roots))
        required_metadata = {
            f"{dist_info}/METADATA",
            f"{dist_info}/RECORD",
            f"{dist_info}/WHEEL",
            f"{dist_info}/entry_points.txt",
            f"{dist_info}/licenses/LICENSE",
            f"{dist_info}/top_level.txt",
        }
        missing_metadata = required_metadata - names
        if missing_metadata:
            raise ArtifactVerificationError(
                f"wheel metadata missing: {sorted(missing_metadata)!r}"
            )
        unexpected = names - (RUNTIME_MODULES | RUNTIME_ASSETS | required_metadata)
        if unexpected:
            raise ArtifactVerificationError(
                f"unexpected wheel members: {sorted(unexpected)!r}"
            )
        metadata_text = archive.read(f"{dist_info}/METADATA").decode("utf-8")
        entrypoints = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")

    metadata = Parser().parsestr(metadata_text)
    if metadata["Name"] != "auction-mcp" or metadata["Version"] != "0.1.0":
        raise ArtifactVerificationError("wheel name/version metadata drifted")
    if metadata["Requires-Python"] != ">=3.10":
        raise ArtifactVerificationError("wheel Python compatibility metadata drifted")
    if metadata["License-Expression"] != "MIT":
        raise ArtifactVerificationError("wheel license metadata drifted")
    requirements = {
        (Requirement(value).name.lower(), str(Requirement(value).specifier))
        for value in metadata.get_all("Requires-Dist", [])
    }
    if requirements != EXPECTED_RUNTIME_REQUIREMENTS:
        raise ArtifactVerificationError(
            f"wheel runtime requirements drifted: {sorted(requirements)!r}"
        )
    if entrypoints.strip() != "[console_scripts]\nauction-mcp = server:main":
        raise ArtifactVerificationError("wheel console entrypoint drifted")
    return {
        "members": len(names),
        "runtime_files": len(RUNTIME_MODULES | RUNTIME_ASSETS),
    }


def inspect_sdist(path: Path) -> dict[str, object]:
    with tarfile.open(path, "r:gz") as archive:
        raw_names = [member.name for member in archive.getmembers() if member.isfile()]
    if not raw_names:
        raise ArtifactVerificationError("sdist is empty")
    stripped = {
        "/".join(PurePosixPath(name).parts[1:])
        for name in raw_names
        if len(PurePosixPath(name).parts) > 1
    }
    _reject_forbidden_members(stripped)
    generated = {
        "PKG-INFO",
        "setup.cfg",
        "auction_mcp.egg-info/PKG-INFO",
        "auction_mcp.egg-info/SOURCES.txt",
        "auction_mcp.egg-info/dependency_links.txt",
        "auction_mcp.egg-info/entry_points.txt",
        "auction_mcp.egg-info/requires.txt",
        "auction_mcp.egg-info/top_level.txt",
    }
    required = set(SOURCE_FILES) | generated
    missing = required - stripped
    if missing:
        raise ArtifactVerificationError(
            f"sdist source files missing: {sorted(missing)!r}"
        )
    unexpected = stripped - required
    if unexpected:
        raise ArtifactVerificationError(
            f"unexpected sdist members: {sorted(unexpected)!r}"
        )
    return {"members": len(stripped), "source_files": len(SOURCE_FILES)}


def compare_reproducible(
    first: dict[str, Path], second: dict[str, Path]
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for kind in ("wheel", "sdist"):
        if first[kind].name != second[kind].name:
            raise ArtifactVerificationError(f"{kind} filename is not reproducible")
        first_hash = sha256_file(first[kind])
        second_hash = sha256_file(second[kind])
        if first_hash != second_hash:
            raise ArtifactVerificationError(
                f"{kind} bytes are not reproducible: {first_hash} != {second_hash}"
            )
        hashes[kind] = first_hash
    return hashes


def verify_installed_consumer(
    wheel: Path,
    consumer_root: Path,
    consumer_cwd: Path,
    base_env: dict[str, str],
) -> None:
    print("[RUN] create independent artifact-consumer venv", flush=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(consumer_root)
    python = venv_python(consumer_root)
    if not python.is_file():
        raise ArtifactVerificationError("artifact-consumer Python was not created")
    env = base_env.copy()
    env.pop("PYTHONPATH", None)
    env["VIRTUAL_ENV"] = str(consumer_root)
    env["PYTHONNOUSERSITE"] = "1"
    bin_dir = python.parent
    env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    run_checked(
        [str(python), "-m", "pip", "install", "--no-input", str(wheel)],
        cwd=consumer_cwd,
        env=env,
        timeout_seconds=300,
        label="install wheel and declared runtime dependencies",
    )
    run_checked(
        [str(python), "-m", "pip", "check"],
        cwd=consumer_cwd,
        env=env,
        timeout_seconds=60,
        label="artifact-consumer pip check",
    )
    origin_check = (
        "from pathlib import Path; import server, auction_mcp_assets; "
        f"root=Path({str(ROOT)!r}).resolve(); "
        "paths=[Path(server.__file__).resolve(), "
        "Path(auction_mcp_assets.__file__).resolve()]; "
        "assert all(p != root and root not in p.parents for p in paths), paths"
    )
    run_checked(
        [str(python), "-c", origin_check],
        cwd=consumer_cwd,
        env=env,
        timeout_seconds=30,
        label="reject source-checkout import leakage",
    )
    result = run_checked(
        [
            str(python),
            str(ROOT / "scripts" / "consumer_probe.py"),
            "--repo-root",
            str(ROOT),
            "--installed",
        ],
        cwd=consumer_cwd,
        env=env,
        timeout_seconds=60,
        label="installed wheel MCP consumer probe",
        capture=True,
    )
    if "CLEANROOM_CONSUMER: PASS" not in result.stdout:
        raise ArtifactVerificationError("installed consumer did not emit PASS marker")
    if '"distribution": "installed-wheel"' not in result.stdout:
        raise ArtifactVerificationError("installed consumer provenance is missing")
    print(result.stdout.rstrip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    diagnostic: dict[str, object] = {"stage": "startup"}
    try:
        diagnostic["stage"] = "build_tool_contract"
        verify_build_tool_versions()
        diagnostic["build_tools"] = BUILD_TOOL_VERSIONS

        with tempfile.TemporaryDirectory(prefix="auction-mcp-artifact-") as temp:
            temp_root = Path(temp).resolve()
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONHASHSEED": "0",
                    "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
                }
            )
            builds: list[dict[str, Path]] = []
            for number in (1, 2):
                source = temp_root / f"source-{number}"
                output = temp_root / f"dist-{number}"
                source.mkdir()
                output.mkdir()
                copy_build_source(source)
                diagnostic["stage"] = f"build_{number}"
                builds.append(build_distributions(source, output, env))

            diagnostic["stage"] = "artifact_reproducibility"
            hashes = compare_reproducible(builds[0], builds[1])
            diagnostic["sha256"] = hashes

            diagnostic["stage"] = "artifact_content_audit"
            diagnostic["wheel"] = inspect_wheel(builds[0]["wheel"])
            diagnostic["sdist"] = inspect_sdist(builds[0]["sdist"])

            diagnostic["stage"] = "installed_consumer"
            consumer_cwd = temp_root / "consumer-cwd"
            consumer_cwd.mkdir()
            verify_installed_consumer(
                builds[0]["wheel"],
                temp_root / "consumer-venv",
                consumer_cwd,
                env,
            )
            diagnostic["installed_consumer"] = "pass"
    except (OSError, subprocess.SubprocessError, ArtifactVerificationError) as exc:
        diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        print(
            "ARTIFACT_DIAGNOSTIC="
            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        print(
            f"ARTIFACT_VERIFICATION: FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    diagnostic["stage"] = "complete"
    diagnostic["temporary_environments_removed"] = True
    print(
        "ARTIFACT_DIAGNOSTIC="
        + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    )
    print("ARTIFACT_VERIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
