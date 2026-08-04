"""Offline forensic bundle, replay, tamper, and deterministic CLI contracts."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import evidence_bundle as bundles
import evidence_safety as safety
from evidence_cli import _load_provider_input


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "yiyuan_evidence_provider_results.json"
EXPECTED_BUNDLE_SHA256 = "728294aaf4a86a62a81d7f64712bcb0a9a4dbeba80c87608b0b6db3cc622a169"
EXPECTED_MANIFEST_SHA256 = "c55a9628c5a28010c4cdf583ee7f4c44ca7d648c8d3cdb4db4e91580c1085631"
EXPECTED_REPORT_SHA256 = "f762580b5266a8bca969f4dccac1a803a0f08bb4ae8e25b2087ae2d5819489b3"


def _provider_input():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _bundle(provider_input=None):
    value = provider_input or _provider_input()
    provenance = value["provenance"]
    return bundles.create_evidence_bundle(
        detail=value["detail"],
        notice=value["notice"],
        community=value["community"],
        market=value["market"],
        analysis_inputs=value["analysis_inputs"],
        collected_at=value["collected_at"],
        provenance_mode=provenance["mode"],
        provenance_note=provenance["note"],
        live_collection=provenance["live_collection"],
    )


def _resign(bundle):
    for segment in bundle["segments"]:
        base = {key: value for key, value in segment.items() if key != "sha256"}
        segment["sha256"] = safety.sha256_hex(safety.canonical_json_bytes(base))
    manifest_segments = [
        {
            key: segment[key]
            for key in (
                "collected_at",
                "name",
                "provider",
                "provider_version",
                "schema_version",
                "sha256",
                "source_url",
            )
        }
        for segment in bundle["segments"]
    ]
    bundle["manifest"]["segments"] = manifest_segments
    manifest_base = {"segments": manifest_segments}
    bundle["manifest"]["manifest_sha256"] = safety.sha256_hex(
        safety.canonical_json_bytes(manifest_base)
    )
    base = {key: value for key, value in bundle.items() if key != "integrity"}
    bundle["integrity"]["bundle_sha256"] = safety.sha256_hex(
        safety.canonical_json_bytes(base)
    )
    return bundle


def _error_code(function):
    with pytest.raises(safety.EvidenceBundleError) as caught:
        function()
    assert caught.value.as_result()["maximum_bid_yuan"] is None
    return caught.value.code


def test_schema_and_fixture_are_versioned_machine_readable_and_not_live():
    assert safety.MAX_BUNDLE_BYTES == 1_048_576
    assert safety.MAX_JSON_DEPTH == 24
    assert safety.SEGMENT_ORDER == (
        "ali_item_detail",
        "court_notice_facts",
        "beike_community",
        "beike_market",
    )
    schema = json.loads(
        (ROOT / "auction_mcp_assets" / "evidence_bundle_schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == bundles.SCHEMA_ID
    assert schema["properties"]["bundle_schema_version"]["const"] == "1.0.0"
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(_bundle())
    fixture = _provider_input()
    assert fixture["provenance"] == {
        "mode": "ANONYMIZED_HISTORICAL_FIXTURE",
        "live_collection": False,
        "note": "P3.8匿名历史测试快照，仅用于确定性离线回放；不是P3.9本轮Live采集。",
    }


def test_canonical_bundle_and_offline_replay_have_frozen_hashes_and_no_bid():
    bundle = _bundle()
    verification = bundles.verify_evidence_bundle(
        bundle, expected_bundle_sha256=EXPECTED_BUNDLE_SHA256
    )
    assert verification == {
        "status": "OK",
        "bundle_schema_version": "1.0.0",
        "bundle_sha256": EXPECTED_BUNDLE_SHA256,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "segments": 4,
        "maximum_bid_yuan": None,
    }
    assert [item["name"] for item in bundle["segments"]] == list(
        safety.SEGMENT_ORDER
    )
    assert all(len(item["sha256"]) == 64 for item in bundle["segments"])
    report = bundles.replay_evidence_bundle(bundle)
    assert safety.sha256_hex(safety.canonical_json_bytes(report)) == EXPECTED_REPORT_SHA256
    assert report["status"] == "OK"
    assert report["maximum_bid_yuan"] is None
    assert report["item"]["area_sqm"] == "106.87"
    assert report["item"]["starting_price_yuan"] == "296800"
    assert report["market"]["independent_median_unit_price_yuan"] == "5593.5"
    assert "最高出价：UNKNOWN" in report["markdown_report"]


def test_replay_uses_zero_network_browser_or_provider_calls(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network must remain unused")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    report = bundles.replay_evidence_bundle(_bundle())
    assert report["status"] == "OK"
    assert calls == []
    assert "server" not in bundles.__dict__


def test_replay_rejects_any_future_analysis_that_emits_a_bid(monkeypatch):
    monkeypatch.setattr(
        bundles,
        "build_asset_analysis",
        lambda **kwargs: {"status": "OK", "maximum_bid_yuan": "1.00"},
    )
    assert _error_code(lambda: bundles.replay_evidence_bundle(_bundle())) == (
        "EVIDENCE_UNSAFE_ANALYSIS"
    )


def test_canonical_file_rejects_whitespace_and_custody_detects_full_rehash(tmp_path):
    bundle = _bundle()
    path = tmp_path / "bundle.json"
    bundles.write_canonical_json(path, bundle)
    assert bundles.load_evidence_bundle(
        path, expected_bundle_sha256=EXPECTED_BUNDLE_SHA256
    ) == bundle
    path.write_bytes(path.read_bytes() + b"\n")
    assert _error_code(lambda: bundles.load_evidence_bundle(path)) == (
        "EVIDENCE_NONCANONICAL_BYTES"
    )

    changed = deepcopy(bundle)
    changed["segments"][0]["payload"]["startingPriceYuan"] = "296801.00"
    _resign(changed)
    bundles.verify_evidence_bundle(changed)
    assert _error_code(
        lambda: bundles.verify_evidence_bundle(
            changed, expected_bundle_sha256=EXPECTED_BUNDLE_SHA256
        )
    ) == "EVIDENCE_CUSTODY_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda value: value["segments"][0]["payload"].__setitem__(
                "startingPriceYuan", "1.00"
            ),
            "EVIDENCE_SEGMENT_HASH_MISMATCH",
        ),
        (
            lambda value: value["manifest"].__setitem__("manifest_sha256", "0" * 64),
            "EVIDENCE_MANIFEST_HASH_MISMATCH",
        ),
        (
            lambda value: value["integrity"].__setitem__("bundle_sha256", "0" * 64),
            "EVIDENCE_BUNDLE_HASH_MISMATCH",
        ),
    ],
)
def test_tamper_matrix_rejects_each_integrity_layer(mutate, code):
    bundle = _bundle()
    mutate(bundle)
    assert _error_code(lambda: bundles.verify_evidence_bundle(bundle)) == code


def test_manifest_semantic_tamper_is_detected_before_bundle_hash():
    bundle = _bundle()
    bundle["manifest"]["segments"][0]["source_url"] = (
        "https://sf-item.taobao.com/sf_item/99999999.htm"
    )
    assert _error_code(lambda: bundles.verify_evidence_bundle(bundle)) == (
        "EVIDENCE_MANIFEST_MISMATCH"
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value["segments"].pop(), "EVIDENCE_REQUIRED_SEGMENT_MISSING"),
        (
            lambda value: value["segments"].__setitem__(1, deepcopy(value["segments"][0])),
            "EVIDENCE_DUPLICATE_SEGMENT",
        ),
        (
            lambda value: value.__setitem__("bundle_schema_version", "2.0.0"),
            "EVIDENCE_UNKNOWN_VERSION",
        ),
        (
            lambda value: value["segments"][0].__setitem__("provider_version", "2.0.0"),
            "EVIDENCE_UNKNOWN_VERSION",
        ),
        (
            lambda value: value["segments"][1].__setitem__(
                "collected_at", "2026-08-03T12:59:59Z"
            ),
            "EVIDENCE_TIME_ROLLBACK",
        ),
        (
            lambda value: value["segments"][0].__setitem__(
                "source_url", "https://example.com/sf_item/1065987562217.htm"
            ),
            "EVIDENCE_URL_OUT_OF_SCOPE",
        ),
    ],
)
def test_missing_duplicate_version_time_and_url_fail_closed(mutate, code):
    bundle = _bundle()
    mutate(bundle)
    assert _error_code(lambda: bundles.verify_evidence_bundle(bundle)) == code


def test_conflicting_segments_and_duplicate_samples_fail_closed_after_rehash():
    announcement = _bundle()
    announcement["segments"][0]["payload"]["announcementUrl"] = (
        "https://sf.taobao.com/notice_detail/17937540.htm?item_id=1065987562217"
    )
    _resign(announcement)
    assert _error_code(lambda: bundles.verify_evidence_bundle(announcement)) == (
        "EVIDENCE_CONFLICT"
    )

    community = _bundle()
    community["segments"][2]["payload"]["xiaoqu_id"] = "8895132387978914"
    _resign(community)
    assert _error_code(lambda: bundles.verify_evidence_bundle(community)) == (
        "EVIDENCE_CONFLICT"
    )

    duplicate = _bundle()
    duplicate["segments"][3]["payload"]["listings"].append(
        deepcopy(duplicate["segments"][3]["payload"]["listings"][0])
    )
    _resign(duplicate)
    assert _error_code(lambda: bundles.verify_evidence_bundle(duplicate)) == (
        "EVIDENCE_DUPLICATE_SAMPLE"
    )


@pytest.mark.parametrize(
    "sensitive_mutation",
    [
        lambda value: value["detail"].__setitem__("cookie", "x"),
        lambda value: value["detail"].__setitem__(
            "detailText", "Authorization" + ": Bearer hidden"
        ),
        lambda value: value["detail"].__setitem__(
            "detailText", "access " + "token" + " must not enter evidence"
        ),
        lambda value: value["detail"].__setitem__(
            "detailText", "联系电话" + "138" + "0013" + "8000"
        ),
        lambda value: value["detail"].__setitem__(
            "detailText", "身份证" + "110105" + "19491231" + "002X"
        ),
        lambda value: value["detail"].__setitem__(
            "detailText", "银行卡" + "45320151" + "12830366"
        ),
        lambda value: value["notice"].__setitem__("raw_" + "notice", "正文"),
        lambda value: value["notice"].__setitem__("body", "正文"),
        lambda value: value["market"].__setitem__("browser_" + "storage", {}),
        lambda value: value["market"].__setitem__("storage" + "State", {}),
    ],
)
def test_sensitive_redlines_reject_before_normalization(sensitive_mutation):
    provider = _provider_input()
    sensitive_mutation(provider)
    assert _error_code(lambda: _bundle(provider)) == "EVIDENCE_SENSITIVE_DATA"


def test_nonfinite_duplicate_json_depth_size_and_type_boundaries():
    for raw in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}'):
        assert _error_code(lambda raw=raw: safety.parse_json_bytes(raw)) == (
            "EVIDENCE_NONFINITE_NUMBER"
        )
    assert _error_code(lambda: safety.parse_json_bytes(b'{"x":1,"x":2}')) == (
        "EVIDENCE_DUPLICATE_KEY"
    )
    assert _error_code(lambda: safety.parse_json_bytes(b"\xff")) == (
        "EVIDENCE_INVALID_UTF8"
    )
    assert _error_code(lambda: safety.parse_json_bytes(b"{")) == (
        "EVIDENCE_INVALID_JSON"
    )
    assert _error_code(
        lambda: safety.parse_json_bytes(b" " * 1_048_577)
    ) == "EVIDENCE_INPUT_TOO_LARGE"
    deep = None
    for _ in range(26):
        deep = [deep]
    assert _error_code(lambda: safety.canonical_json_bytes(deep)) == (
        "EVIDENCE_INPUT_TOO_DEEP"
    )
    assert _error_code(lambda: safety.canonical_json_bytes(1.0)) == (
        "EVIDENCE_FLOAT_FORBIDDEN"
    )
    assert _error_code(lambda: safety.canonical_json_bytes({1: "bad"})) == (
        "EVIDENCE_INVALID_JSON_TYPE"
    )
    assert _error_code(lambda: safety.canonical_json_bytes(object())) == (
        "EVIDENCE_INVALID_JSON_TYPE"
    )
    assert _error_code(lambda: safety.canonical_json_bytes(10**25)) == (
        "EVIDENCE_INTEGER_OUT_OF_RANGE"
    )
    assert safety.canonical_json_bytes(1) == b"1"
    assert _error_code(
        lambda: safety.canonical_json_bytes([None] * (safety.MAX_CONTAINER_ITEMS + 1))
    ) == "EVIDENCE_INPUT_TOO_LARGE"
    too_many_keys = {
        f"k{index}": None for index in range(safety.MAX_CONTAINER_ITEMS + 1)
    }
    assert _error_code(lambda: safety.canonical_json_bytes(too_many_keys)) == (
        "EVIDENCE_INPUT_TOO_LARGE"
    )
    assert _error_code(
        lambda: safety.canonical_json_bytes("x" * 1_048_576)
    ) == "EVIDENCE_INPUT_TOO_LARGE"


def test_provider_contract_provenance_decimal_and_collection_time_boundaries():
    unexpected = _provider_input()
    unexpected["detail"]["unexpected"] = "drift"
    assert _error_code(lambda: _bundle(unexpected)) == (
        "EVIDENCE_PROVIDER_CONTRACT_DRIFT"
    )
    bad_decimal = _provider_input()
    bad_decimal["market"]["listings"][0]["area_sqm"] = "NaN"
    assert _error_code(lambda: _bundle(bad_decimal)) == "EVIDENCE_INVALID_DECIMAL"
    bad_mode = _provider_input()
    bad_mode["provenance"]["mode"] = "UNKNOWN"
    assert _error_code(lambda: _bundle(bad_mode)) == "EVIDENCE_PROVENANCE_INVALID"
    live_fixture = _provider_input()
    live_fixture["provenance"]["live_collection"] = True
    assert _error_code(lambda: _bundle(live_fixture)) == (
        "EVIDENCE_PROVENANCE_CONFLICT"
    )
    rollback = _provider_input()
    rollback["collected_at"]["beike_community"] = "2026-08-03T12:00:00Z"
    assert _error_code(lambda: _bundle(rollback)) == "EVIDENCE_TIME_ROLLBACK"
    naive = _provider_input()
    naive["collected_at"]["ali_item_detail"] = "2026-08-03T13:00:00"
    assert _error_code(lambda: _bundle(naive)) == "EVIDENCE_INVALID_TIME"
    invalid = _provider_input()
    invalid["collected_at"]["ali_item_detail"] = "not-a-time"
    assert _error_code(lambda: _bundle(invalid)) == "EVIDENCE_INVALID_TIME"
    assert safety.normalize_utc_timestamp("2026-08-03T13:00:00.123456+00:00") == (
        "2026-08-03T13:00:00.123456Z"
    )
    assert _error_code(
        lambda: safety.parse_canonical_utc("2026-08-03T13:00:00+00:00")
    ) == "EVIDENCE_NONCANONICAL_TIME"


@pytest.mark.parametrize(
    ("name", "url", "payload"),
    [
        (
            "ali_item_detail",
            "http://sf-item.taobao.com/sf_item/1065987562217.htm",
            {"itemId": "1065987562217"},
        ),
        (
            "ali_item_detail",
            "https://user@sf-item.taobao.com/sf_item/1065987562217.htm",
            {"itemId": "1065987562217"},
        ),
        (
            "ali_item_detail",
            "https://sf-item.taobao.com:444/sf_item/1065987562217.htm",
            {"itemId": "1065987562217"},
        ),
        (
            "ali_item_detail",
            "https://sf-item.taobao.com/sf_item/1065987562217.htm?x=1",
            {"itemId": "1065987562217"},
        ),
        (
            "court_notice_facts",
            "https://sf.taobao.com/wrong/17937539.htm?item_id=1065987562217",
            {},
        ),
        (
            "beike_community",
            "https://jiangmen.ke.com/xiaoqu/not-an-id/",
            {},
        ),
        (
            "beike_market",
            "https://jiangmen.ke.com/ershoufang/not-an-id/",
            {},
        ),
        ("unknown", "https://example.com/", {}),
    ],
)
def test_each_segment_url_boundary_rejects_out_of_scope(name, url, payload):
    assert _error_code(
        lambda: safety.validate_segment_url(
            name, url, payload, item_id="1065987562217"
        )
    ) == "EVIDENCE_URL_OUT_OF_SCOPE"


def test_unknown_segment_contract_fails_closed():
    assert _error_code(
        lambda: safety.validate_segment_contract({"name": "unknown"})
    ) == "EVIDENCE_UNKNOWN_SEGMENT"


def test_diff_classifies_prices_samples_legal_facts_sources_and_fields():
    left = _bundle()
    changed = _provider_input()
    changed["detail"]["startingPriceYuan"] = "300000.00"
    changed["analysis_inputs"]["screenshot_starting_price_yuan"] = "300000.00"
    changed["notice"]["lease_disclosed"] = True
    changed["market"]["listings"].pop(0)
    changed["market"]["listings"].append(
        {
            "title": "新增匿名样本D",
            "area_sqm": "88.00",
            "house_info": "中楼层 2室1厅 | 88平米 | 2005年",
            "total_price_wan": "48.00",
            "unit_price_yuan": "5454.00",
            "source_url": "https://jiangmen.ke.com/ershoufang/105120000004.html",
        }
    )
    changed["community"]["region"] = "江华港口"
    changed["market"]["listing_source_url"] = (
        "https://jiangmen.ke.com/ershoufang/c8895132387978914/"
    )
    right = _bundle(changed)
    result = bundles.diff_evidence_bundles(left, right)
    assert result["status"] == "OK"
    assert result["maximum_bid_yuan"] is None
    assert result["summary"]["price_changes"] == 1
    assert result["summary"]["legal_fact_changes"] == 1
    assert result["summary"]["sample_additions"] == 1
    assert result["summary"]["sample_removals"] == 1
    assert result["summary"]["source_changes"] == 1
    assert result["summary"]["field_changes"] >= 1


def test_diff_classifies_schema_change_but_refuses_cross_version_interpretation():
    left = _bundle()
    right = _bundle()
    right["segments"][0]["schema_version"] = "2.0.0"
    _resign(right)
    with pytest.raises(safety.EvidenceBundleError) as caught:
        bundles.diff_evidence_bundles(left, right)
    assert caught.value.code == "EVIDENCE_SCHEMA_CHANGE"
    assert caught.value.diagnostics == {
        "category": "schema_changes",
        "changes": [
            {
                "after": "2.0.0",
                "before": "1.0.0",
                "path": "segments.ali_item_detail.schema_version",
            }
        ],
    }
    assert caught.value.as_result()["maximum_bid_yuan"] is None


def test_cli_is_byte_deterministic_across_processes_hash_seeds_and_cwds(tmp_path):
    outputs = []
    reports = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    for index, seed in enumerate(("1", "77", "999")):
        cwd = tmp_path / f"cwd-{index}"
        cwd.mkdir()
        output = tmp_path / f"bundle-{index}.json"
        process_env = env.copy()
        process_env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "evidence_cli",
                "create",
                "--input",
                str(FIXTURE),
                "--output",
                str(output),
            ],
            cwd=cwd,
            env=process_env,
            check=False,
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        assert EXPECTED_BUNDLE_SHA256.encode() in completed.stdout
        outputs.append(output.read_bytes())
        report_output = tmp_path / f"report-{index}.json"
        replay = subprocess.run(
            [
                sys.executable,
                "-m",
                "evidence_cli",
                "replay",
                "--bundle",
                str(output),
                "--expected-sha256",
                EXPECTED_BUNDLE_SHA256,
                "--output",
                str(report_output),
            ],
            cwd=cwd,
            env=process_env,
            check=False,
            capture_output=True,
            timeout=30,
        )
        assert replay.returncode == 0, replay.stderr.decode(errors="replace")
        reports.append(report_output.read_bytes())
    assert outputs[0] == outputs[1] == outputs[2]
    assert reports[0] == reports[1] == reports[2]
    assert safety.sha256_hex(reports[0]) == EXPECTED_REPORT_SHA256

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(outputs[0])
    report_path = tmp_path / "report.json"
    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "evidence_cli",
            "verify",
            "--bundle",
            str(bundle_path),
            "--expected-sha256",
            EXPECTED_BUNDLE_SHA256,
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert verify.returncode == 0
    replay = subprocess.run(
        [
            sys.executable,
            "-m",
            "evidence_cli",
            "replay",
            "--bundle",
            str(bundle_path),
            "--expected-sha256",
            EXPECTED_BUNDLE_SHA256,
            "--output",
            str(report_path),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert replay.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["maximum_bid_yuan"] is None


def test_cli_failure_is_fail_closed_and_never_echoes_sensitive_value(tmp_path):
    provider = _provider_input()
    provider["detail"]["detailText"] = "联系电话" + "138" + "0013" + "8000"
    input_path = tmp_path / "provider.json"
    input_path.write_text(json.dumps(provider, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evidence_cli",
            "create",
            "--input",
            str(input_path),
            "--output",
            str(tmp_path / "out.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert b"EVIDENCE_SENSITIVE_DATA" in completed.stderr
    assert ("138" + "0013" + "8000").encode() not in completed.stderr


def test_cli_diff_returns_classified_fail_closed_schema_change(tmp_path):
    left = _bundle()
    right = _bundle()
    right["segments"][0]["schema_version"] = "2.0.0"
    _resign(right)
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    bundles.write_canonical_json(left_path, left)
    bundles.write_canonical_json(right_path, right)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evidence_cli",
            "diff",
            "--left",
            str(left_path),
            "--right",
            str(right_path),
            "--left-expected-sha256",
            left["integrity"]["bundle_sha256"],
            "--right-expected-sha256",
            right["integrity"]["bundle_sha256"],
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 2
    result = json.loads(completed.stderr)
    assert result["status"] == "STOPPED"
    assert result["maximum_bid_yuan"] is None
    assert result["error"]["code"] == "EVIDENCE_SCHEMA_CHANGE"
    assert result["error"]["diagnostics"]["category"] == "schema_changes"


def test_provider_input_loader_rejects_duplicate_nonfinite_and_wrong_envelope(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"x":1,"x":2}', encoding="utf-8")
    assert _error_code(lambda: _load_provider_input(duplicate)) == "EVIDENCE_DUPLICATE_KEY"
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"x":NaN}', encoding="utf-8")
    assert _error_code(lambda: _load_provider_input(nonfinite)) == "EVIDENCE_NONFINITE_NUMBER"
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"x":1}', encoding="utf-8")
    assert _error_code(lambda: _load_provider_input(wrong)) == "EVIDENCE_SCHEMA_INVALID"
