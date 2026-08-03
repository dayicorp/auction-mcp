"""Raw-stdio MCP chaos and real-process lifecycle stress gate.

The gate uses only newline-delimited JSON-RPC and the Python standard library;
it does not use the MCP client SDK.  Every server starts from an external cwd
under a sitecustomize network guard and is explicitly reaped.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gc
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
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
PROTOCOL_VERSION = "2025-06-18"
MAX_STDERR_BYTES = 64 * 1024
STRESS_RESPONSE_TIMEOUT_SECONDS = 30.0
CHAOS_EXIT_TIMEOUT_SECONDS = 10.0
FORBIDDEN_OUTPUT_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"_m_h5_tk"),
    re.compile(r"storage_state", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
NETWORK_GUARD_SOURCE = """\
import socket
from safety_core import is_local_socket_address

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection

def _blocked(address):
    raise RuntimeError("network disabled by runtime reliability gate")

def _guarded_connect(self, address):
    return _connect(self, address) if is_local_socket_address(address) else _blocked(address)

def _guarded_connect_ex(self, address):
    return _connect_ex(self, address) if is_local_socket_address(address) else _blocked(address)

def _guarded_create_connection(address, *args, **kwargs):
    return _create_connection(address, *args, **kwargs) if is_local_socket_address(address) else _blocked(address)

socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.create_connection = _guarded_create_connection
"""


class ReliabilityError(RuntimeError):
    """Raised when any runtime reliability contract is violated."""


class GuardEnvironment:
    """External cwd plus a process-wide external-network blocker."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="auction-mcp-runtime-"))
        self.cwd = self.root / "external-cwd"
        self.guard = self.root / "guard"
        self.cwd.mkdir()
        self.guard.mkdir()
        (self.guard / "sitecustomize.py").write_text(
            NETWORK_GUARD_SOURCE, encoding="utf-8"
        )
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = os.pathsep.join([str(self.guard), str(ROOT)])
        self.env["PYTHONNOUSERSITE"] = "1"
        self.env["PYTEST_ADDOPTS"] = ""
        self._processes: set["RawMCPProcess"] = set()
        self._lock = threading.Lock()

    def register(self, process: "RawMCPProcess") -> None:
        with self._lock:
            self._processes.add(process)

    def unregister(self, process: "RawMCPProcess") -> None:
        with self._lock:
            self._processes.discard(process)

    def verify(self) -> None:
        if self.cwd == ROOT or ROOT in self.cwd.parents:
            raise ReliabilityError("runtime cwd is not external to repository")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; socket.create_connection(('203.0.113.1',443),timeout=1)",
            ],
            cwd=self.cwd,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if (
            completed.returncode == 0
            or "network disabled by runtime reliability gate"
            not in completed.stderr
        ):
            raise ReliabilityError("runtime network guard self-test failed")

    def close(self) -> None:
        root = self.root
        with self._lock:
            active = list(self._processes)
        for process in active:
            process.abort()
        shutil.rmtree(root)
        if root.exists():
            raise ReliabilityError(f"temporary runtime root survived: {root}")

    def __enter__(self) -> "GuardEnvironment":
        self.verify()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class RawMCPProcess:
    """One raw JSON-RPC stdio server lifecycle with bounded diagnostics."""

    def __init__(self, guard: GuardEnvironment) -> None:
        self.guard = guard
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=guard.cwd,
            env=guard.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self.pid = self.process.pid
        self._stdout_queue: queue.Queue[bytes] = queue.Queue()
        self.stdout_lines: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name=f"mcp-stdout-{self.pid}", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"mcp-stderr-{self.pid}", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        guard.register(self)

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            self._stdout_queue.put(line)
            if not line:
                return
            self.stdout_lines.append(line)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            chunk = self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_chunks.append(chunk)

    def write(self, data: bytes, fragments: tuple[int, ...] | None = None) -> None:
        if self.process.stdin is None:
            raise ReliabilityError("server stdin is closed")
        try:
            if not fragments:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                return
            offset = 0
            for size in fragments:
                if offset >= len(data):
                    break
                self.process.stdin.write(data[offset : offset + size])
                self.process.stdin.flush()
                offset += size
            if offset < len(data):
                self.process.stdin.write(data[offset:])
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ReliabilityError("server closed stdin unexpectedly") from exc

    def send(self, message: dict[str, Any], *, fragmented: bool = False) -> None:
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        fragments = (1, 2, 3, 5, 8, 13, 21) if fragmented else None
        self.write(encoded, fragments)

    def read(self, timeout: float = 10.0) -> dict[str, Any]:
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("raw MCP response timed out") from exc
        if not line:
            raise ReliabilityError("MCP stdout reached EOF before response")
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReliabilityError("MCP stdout contained non-JSON pollution") from exc
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ReliabilityError("MCP stdout contained a non-JSON-RPC object")
        return message

    def close_input(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()

    def finish(self, timeout: float = 10.0) -> tuple[int, float]:
        self.close_input()
        started = time.monotonic()
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.process.kill()
            self.process.wait(timeout=5)
            raise ReliabilityError("MCP process did not exit before timeout") from exc
        finally:
            self._stdout_thread.join(timeout=2)
            self._stderr_thread.join(timeout=2)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        self.validate_output()
        self.guard.unregister(self)
        return returncode, time.monotonic() - started

    def abort(self) -> None:
        """Best-effort cleanup used only while unwinding another failure."""
        self.close_input()
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.guard.unregister(self)

    def validate_output(self) -> None:
        stderr = b"".join(self.stderr_chunks)
        if len(stderr) > MAX_STDERR_BYTES:
            raise ReliabilityError("MCP stderr exceeded 64 KiB boundary")
        decoded_stderr = stderr.decode("utf-8", errors="replace")
        if any(pattern.search(decoded_stderr) for pattern in FORBIDDEN_OUTPUT_PATTERNS):
            raise ReliabilityError("MCP stderr exposed sensitive state")
        for line in self.stdout_lines:
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReliabilityError("MCP stdout protocol pollution detected") from exc
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ReliabilityError("MCP stdout protocol pollution detected")


def initialize(
    process: RawMCPProcess,
    *,
    fragmented: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    process.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "auction-runtime-gate", "version": "1"},
            },
        },
        fragmented=fragmented,
    )
    response = process.read(timeout=timeout)
    if response.get("id") != 1 or "result" not in response:
        raise ReliabilityError("initialize response contract failed")
    process.send(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    )
    return response


def assert_ping(process: RawMCPProcess, request_id: int) -> None:
    process.send(
        {"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": {}}
    )
    response = read_for_id(process, request_id)
    if response.get("id") != request_id or response.get("result") != {}:
        raise ReliabilityError("ping recovery response contract failed")


def read_for_id(
    process: RawMCPProcess, request_id: int, *, max_messages: int = 4
) -> dict[str, Any]:
    """Read through bounded server notifications to one response id."""
    for _ in range(max_messages):
        message = process.read()
        if message.get("id") == request_id:
            return message
        if "method" not in message:
            raise ReliabilityError(
                f"unexpected JSON-RPC response while waiting for id {request_id}"
            )
    raise ReliabilityError(f"response id {request_id} exceeded notification bound")


def finish_scenario(
    process: RawMCPProcess, scenario: str, *, timeout: float = CHAOS_EXIT_TIMEOUT_SECONDS
) -> tuple[int, float]:
    try:
        return process.finish(timeout=timeout)
    except Exception as exc:
        raise ReliabilityError(f"{scenario} finish failed: {exc}") from exc


def run_protocol_chaos(guard: GuardEnvironment) -> dict[str, Any]:
    scenarios: dict[str, Any] = {}

    fragmented = RawMCPProcess(guard)
    initialize(fragmented, fragmented=True)
    assert_ping(fragmented, 2)
    returncode, _ = finish_scenario(fragmented, "fragmented_write")
    if returncode != 0:
        raise ReliabilityError("fragmented request lifecycle exited nonzero")
    scenarios["fragmented_write"] = "recovered"

    pipelined = RawMCPProcess(guard)
    initialize(pipelined)
    payload = b"".join(
        (
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            for message in (
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
            )
        )
    )
    pipelined.write(payload)
    responses = [pipelined.read(), pipelined.read()]
    if {response.get("id") for response in responses} != {2, 3}:
        raise ReliabilityError("pipelined requests lost or duplicated a response")
    returncode, _ = finish_scenario(pipelined, "pipelined_requests")
    if returncode != 0:
        raise ReliabilityError("pipelined request lifecycle exited nonzero")
    scenarios["pipelined_requests"] = 2

    invalid = RawMCPProcess(guard)
    initialize(invalid)
    invalid.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ali_pc_get_item_detail", "arguments": {}},
        }
    )
    invalid_response = invalid.read()
    result = invalid_response.get("result") or {}
    if invalid_response.get("id") != 2 or result.get("isError") is not True:
        raise ReliabilityError("invalid tool parameters did not return MCP error result")
    assert_ping(invalid, 3)
    returncode, _ = finish_scenario(invalid, "invalid_parameters")
    if returncode != 0:
        raise ReliabilityError("invalid parameter recovery exited nonzero")
    scenarios["invalid_parameters"] = "error_then_recovered"

    unknown = RawMCPProcess(guard)
    initialize(unknown)
    unknown.send(
        {"jsonrpc": "2.0", "id": 2, "method": "unknown/method", "params": {}}
    )
    unknown_response = read_for_id(unknown, 2)
    if (unknown_response.get("error") or {}).get("code") != -32602:
        raise ReliabilityError("unknown method did not return bounded JSON-RPC error")
    assert_ping(unknown, 3)
    returncode, _ = finish_scenario(unknown, "unknown_method")
    if returncode != 0:
        raise ReliabilityError("unknown method recovery exited nonzero")
    scenarios["unknown_method"] = "jsonrpc_error_then_recovered"

    malformed = RawMCPProcess(guard)
    initialize(malformed)
    malformed.write(b'{"jsonrpc":"2.0","id":1,"method":BROKEN}\n')
    assert_ping(malformed, 2)
    returncode, _ = finish_scenario(malformed, "malformed_json")
    if returncode != 0:
        raise ReliabilityError("malformed JSON recovery exited nonzero")
    scenarios["malformed_json"] = "error_notification_then_recovered"

    early_eof = RawMCPProcess(guard)
    early_eof.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"')
    returncode, eof_seconds = finish_scenario(early_eof, "early_eof")
    early_eof.validate_output()
    if eof_seconds > CHAOS_EXIT_TIMEOUT_SECONDS or returncode not in {0, 1}:
        raise ReliabilityError("early EOF cleanup contract failed")
    scenarios["early_eof"] = "bounded_exit"

    timeout = RawMCPProcess(guard)
    initialize(timeout)
    timeout.write(b'{"jsonrpc":"2.0","id":2,"method":"ping"')
    try:
        timeout.read(timeout=0.25)
    except TimeoutError:
        pass
    else:
        raise ReliabilityError("unterminated request did not trigger client timeout")
    returncode, _ = finish_scenario(timeout, "client_timeout")
    timeout.validate_output()
    if returncode not in {0, 1}:
        raise ReliabilityError("timeout cleanup contract failed")
    scenarios["client_timeout"] = "timed_out_then_reaped"

    return {
        "lifecycle_count": 7,
        "network_blocked": True,
        "scenarios": scenarios,
        "stderr_limit_bytes": MAX_STDERR_BYTES,
        "stdout_protocol_clean": True,
    }


def _resource_count() -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        count = wintypes.DWORD()
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(), ctypes.byref(count)
        ):
            return None
        return int(count.value)
    fd_root = Path("/proc/self/fd")
    if fd_root.is_dir():
        return len(list(fd_root.iterdir()))
    return None


def _lingering_child_processes(parent_pids: set[int]) -> set[int]:
    """Return live descendants still attached to completed server parents."""
    if not parent_pids:
        return set()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ReliabilityError("cannot snapshot Windows process tree")
        children: set[int] = set()
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            available = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while available:
                if int(entry.th32ParentProcessID) in parent_pids:
                    children.add(int(entry.th32ProcessID))
                available = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return children

    lingering: set[int] = set()
    for process_group in parent_pids:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise ReliabilityError(
                f"cannot verify process group {process_group} cleanup"
            ) from exc
        else:
            lingering.add(process_group)
    return lingering


def _stress_lifecycle(guard: GuardEnvironment) -> tuple[int, float]:
    started = time.monotonic()
    process = RawMCPProcess(guard)
    try:
        try:
            initialize(process, timeout=STRESS_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ReliabilityError("initialize stage timed out") from exc
        process.send(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        try:
            tools_response = process.read(timeout=STRESS_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ReliabilityError("tools/list stage timed out") from exc
        tools = {
            item.get("name")
            for item in ((tools_response.get("result") or {}).get("tools") or [])
        }
        if tools != EXPECTED_TOOLS:
            raise ReliabilityError("stress lifecycle observed MCP schema/name drift")
        process.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ali_pc_browser_status", "arguments": {}},
            }
        )
        try:
            status_response = process.read(timeout=STRESS_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ReliabilityError("tools/call stage timed out") from exc
        result = status_response.get("result") or {}
        if result.get("isError") is True:
            raise ReliabilityError("offline status call failed during stress")
        content = result.get("content") or []
        if len(content) != 1:
            raise ReliabilityError("offline status call returned unexpected content")
        status = json.loads(content[0].get("text", "null"))
        if status != {
            "state": "stopped",
            "cookie_policy": "browser_memory_only",
        }:
            raise ReliabilityError("stress lifecycle touched browser state")
        returncode, _ = process.finish()
        if returncode != 0:
            raise ReliabilityError("stress lifecycle exited nonzero")
        process.validate_output()
        return process.pid, time.monotonic() - started
    except Exception:
        process.abort()
        raise


def run_process_stress(
    guard: GuardEnvironment,
    *,
    sequential: int,
    workers: int,
    per_worker: int,
) -> dict[str, Any]:
    if sequential < 100 or workers < 8 or per_worker < 20:
        raise ReliabilityError("stress counts are below the P3.4 acceptance floor")
    before_resources = _resource_count()
    started = time.monotonic()
    records: list[tuple[int, float]] = []

    for index in range(sequential):
        try:
            records.append(_stress_lifecycle(guard))
        except Exception as exc:
            raise ReliabilityError(
                f"sequential lifecycle {index + 1}/{sequential} failed: {exc}"
            ) from exc

    def worker(worker_index: int) -> list[tuple[int, float]]:
        worker_records: list[tuple[int, float]] = []
        for iteration in range(per_worker):
            try:
                worker_records.append(_stress_lifecycle(guard))
            except Exception as exc:
                raise ReliabilityError(
                    f"worker {worker_index} lifecycle {iteration + 1}/{per_worker} failed: {exc}"
                ) from exc
        return worker_records

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, index + 1) for index in range(workers)]
        for future in concurrent.futures.as_completed(futures):
            records.extend(future.result())

    gc.collect()
    after_resources = _resource_count()
    resource_delta = (
        None
        if before_resources is None or after_resources is None
        else after_resources - before_resources
    )
    if resource_delta is not None and resource_delta > 8:
        raise ReliabilityError(
            f"parent process resource count leaked by {resource_delta}"
        )
    expected = sequential + workers * per_worker
    if len(records) != expected:
        raise ReliabilityError("stress lifecycle accounting mismatch")
    parent_pids = {pid for pid, _ in records}
    lingering_children = _lingering_child_processes(parent_pids)
    if lingering_children:
        raise ReliabilityError(
            f"lingering child processes detected: {sorted(lingering_children)!r}"
        )
    lifecycle_seconds = sorted(duration for _, duration in records)
    p95_index = max(0, int(len(lifecycle_seconds) * 0.95) - 1)

    return {
        "concurrent_lifecycles": workers * per_worker,
        "browser_started": False,
        "duration_seconds": round(time.monotonic() - started, 3),
        "external_cwd": True,
        "lifecycle_count": expected,
        "lingering_child_processes": 0,
        "network_blocked": True,
        "max_lifecycle_seconds": round(max(lifecycle_seconds), 3),
        "parent_resource_delta": resource_delta,
        "p95_lifecycle_seconds": round(lifecycle_seconds[p95_index], 3),
        "processes_reaped": expected,
        "response_timeout_seconds": STRESS_RESPONSE_TIMEOUT_SECONDS,
        "sequential_lifecycles": sequential,
        "workers": workers,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("chaos", "stress", "all"), default="all")
    parser.add_argument("--sequential", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-worker", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    diagnostic: dict[str, Any] = {
        "mode": arguments.mode,
        "platform": sys.platform,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
    try:
        with GuardEnvironment() as guard:
            if arguments.mode in {"chaos", "all"}:
                diagnostic["protocol_chaos"] = run_protocol_chaos(guard)
            if arguments.mode in {"stress", "all"}:
                diagnostic["process_stress"] = run_process_stress(
                    guard,
                    sequential=arguments.sequential,
                    workers=arguments.workers,
                    per_worker=arguments.per_worker,
                )
    except Exception as exc:
        diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        print(
            "RUNTIME_RELIABILITY_DIAGNOSTIC="
            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        print("RUNTIME_RELIABILITY: FAIL", file=sys.stderr)
        return 1
    print(
        "RUNTIME_RELIABILITY_DIAGNOSTIC="
        + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    )
    print("RUNTIME_RELIABILITY: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
