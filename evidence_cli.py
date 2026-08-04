"""Installed command-line interface for deterministic evidence operations."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from evidence_bundle import (
    create_evidence_bundle,
    diff_evidence_bundles,
    ensure_diff_schema_compatible,
    load_evidence_bundle,
    replay_evidence_bundle,
    verify_evidence_bundle,
    write_canonical_json,
)
from evidence_safety import (
    MAX_BUNDLE_BYTES,
    EvidenceBundleError,
    canonical_json_bytes,
    parse_json_bytes,
)


def _load_provider_input(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError(
            "EVIDENCE_INPUT_TOO_LARGE",
            "provider输入超过1 MiB安全上限",
            {"bytes": len(data), "maximum_bytes": MAX_BUNDLE_BYTES},
        )

    def reject_constant(value: str) -> None:
        raise EvidenceBundleError(
            "EVIDENCE_NONFINITE_NUMBER", "provider输入禁止NaN或Infinity", {"value": value}
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceBundleError(
                    "EVIDENCE_DUPLICATE_KEY", "provider输入包含重复键", {"key": key}
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            parse_float=Decimal,
            object_pairs_hook=unique_object,
        )
    except UnicodeDecodeError as exc:
        raise EvidenceBundleError("EVIDENCE_INVALID_UTF8", "provider输入必须是UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_JSON", "provider输入不是有效JSON", {"line": exc.lineno}
        ) from exc
    if not isinstance(value, dict):
        raise EvidenceBundleError("EVIDENCE_SCHEMA_INVALID", "provider输入必须是对象")
    expected = {
        "analysis_inputs",
        "collected_at",
        "community",
        "detail",
        "market",
        "notice",
        "provenance",
    }
    if set(value) != expected:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID",
            "provider输入字段集合不符合契约",
            {
                "missing": sorted(expected - set(value)),
                "unexpected": sorted(set(value) - expected),
            },
        )
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "live_collection",
        "mode",
        "note",
    }:
        raise EvidenceBundleError("EVIDENCE_SCHEMA_INVALID", "provenance字段不符合契约")
    return value


def _write_or_print(output: Path | None, value: Any) -> None:
    if output is None:
        sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    else:
        write_canonical_json(output, value)


def _load_canonical_diff_header(path: Path) -> dict[str, Any]:
    """Read only enough canonical JSON to classify incompatible schemas."""
    data = path.read_bytes()
    value = parse_json_bytes(data)
    if canonical_json_bytes(value) != data:
        raise EvidenceBundleError(
            "EVIDENCE_NONCANONICAL_BYTES", "证据文件字节不是规范JSON，可能被篡改"
        )
    if not isinstance(value, dict):
        raise EvidenceBundleError("EVIDENCE_SCHEMA_INVALID", "证据包必须是对象")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auction-evidence",
        description="Create, verify, replay, and diff canonical offline evidence bundles.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a canonical bundle")
    create.add_argument("--input", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="verify a canonical bundle")
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--expected-sha256", required=True)
    replay = subparsers.add_parser("replay", help="rebuild the analysis offline")
    replay.add_argument("--bundle", required=True, type=Path)
    replay.add_argument("--expected-sha256", required=True)
    replay.add_argument("--output", type=Path)
    difference = subparsers.add_parser("diff", help="compare two verified bundles")
    difference.add_argument("--left", required=True, type=Path)
    difference.add_argument("--right", required=True, type=Path)
    difference.add_argument("--left-expected-sha256", required=True)
    difference.add_argument("--right-expected-sha256", required=True)
    difference.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            provider_input = _load_provider_input(args.input)
            provenance = provider_input["provenance"]
            bundle = create_evidence_bundle(
                detail=provider_input["detail"],
                notice=provider_input["notice"],
                community=provider_input["community"],
                market=provider_input["market"],
                analysis_inputs=provider_input["analysis_inputs"],
                collected_at=provider_input["collected_at"],
                provenance_mode=provenance["mode"],
                provenance_note=provenance["note"],
                live_collection=provenance["live_collection"],
            )
            write_canonical_json(args.output, bundle)
            _write_or_print(
                None,
                {
                    "bundle_sha256": bundle["integrity"]["bundle_sha256"],
                    "maximum_bid_yuan": None,
                    "output": str(args.output.resolve()),
                    "status": "OK",
                },
            )
        elif args.command == "verify":
            bundle = load_evidence_bundle(
                args.bundle, expected_bundle_sha256=args.expected_sha256
            )
            _write_or_print(
                None,
                verify_evidence_bundle(
                    bundle, expected_bundle_sha256=args.expected_sha256
                ),
            )
        elif args.command == "replay":
            bundle = load_evidence_bundle(
                args.bundle, expected_bundle_sha256=args.expected_sha256
            )
            _write_or_print(args.output, replay_evidence_bundle(bundle))
        else:
            left_header = _load_canonical_diff_header(args.left)
            right_header = _load_canonical_diff_header(args.right)
            ensure_diff_schema_compatible(left_header, right_header)
            left = load_evidence_bundle(
                args.left, expected_bundle_sha256=args.left_expected_sha256
            )
            right = load_evidence_bundle(
                args.right, expected_bundle_sha256=args.right_expected_sha256
            )
            _write_or_print(args.output, diff_evidence_bundles(left, right))
    except EvidenceBundleError as exc:
        sys.stderr.buffer.write(canonical_json_bytes(exc.as_result()) + b"\n")
        return 2
    except OSError as exc:
        result = EvidenceBundleError(
            "EVIDENCE_IO_FAILED",
            "证据文件读写失败",
            {"exception_type": type(exc).__name__},
        ).as_result()
        sys.stderr.buffer.write(canonical_json_bytes(result) + b"\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
