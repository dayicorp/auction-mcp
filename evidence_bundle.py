"""Versioned, canonical auction evidence bundles and offline replay.

Provider results enter through an explicit allowlist and are normalized into a
credential-free JSON bundle.  Replay imports no provider or browser modules and
performs no network access.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
import hmac
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from asset_analysis import build_asset_analysis
from evidence_safety import (
    BUNDLE_SCHEMA_VERSION,
    SEGMENT_CONTRACTS,
    SEGMENT_ORDER,
    EvidenceBundleError,
    canonical_json_bytes,
    normalize_utc_timestamp,
    parse_canonical_utc,
    parse_json_bytes,
    scan_sensitive_data,
    sha256_hex,
    validate_segment_contract,
    validate_segment_url,
)


SCHEMA_ID = "urn:auction-mcp:evidence-bundle:1.0.0"
ALLOWED_PROVENANCE_MODES = {
    "AUTHORIZED_PROVIDER_RESULTS",
    "ANONYMIZED_HISTORICAL_FIXTURE",
}
_TOP_LEVEL_KEYS = {
    "$schema",
    "analysis_inputs",
    "bundle_schema_version",
    "integrity",
    "manifest",
    "provenance",
    "segments",
}
_DETAIL_INPUT_KEYS = {
    "announcementUrl",
    "appraisalPriceYuan",
    "auctionStartAt",
    "depositYuan",
    "detailText",
    "incrementYuan",
    "itemId",
    "location",
    "source",
    "stage",
    "startingPriceYuan",
    "status",
    "title",
    "url",
}
_DETAIL_REQUIRED_INPUT_KEYS = {
    "announcementUrl",
    "detailText",
    "itemId",
    "location",
    "startingPriceYuan",
    "title",
    "url",
}
_DETAIL_PAYLOAD_KEYS = _DETAIL_INPUT_KEYS - {"source"}
_NOTICE_FACT_KEYS = {
    "as_is_delivery",
    "balance_deadline",
    "buyer_advances_seller_tax",
    "court_clear_handover",
    "household_registration_not_handled",
    "lease_disclosed",
    "no_defect_warranty",
    "occupancy_disclosed",
    "possible_arrears_buyer_risk",
    "regret_deposit_forfeited",
    "transfer_not_guaranteed",
    "viewing_deadline",
}
_COMMUNITY_PAYLOAD_KEYS = {"name", "region", "xiaoqu_id"}
_MARKET_DETAIL_KEYS = {
    "address",
    "facts",
    "listing_average_unit_price_yuan",
    "name",
    "source_url",
    "xiaoqu_id",
}
_LISTING_KEYS = {
    "area_sqm",
    "house_info",
    "source_url",
    "title",
    "total_price_wan",
    "unit_price_yuan",
}
_MONEY_FIELDS = {
    "appraisalPriceYuan",
    "depositYuan",
    "incrementYuan",
    "startingPriceYuan",
}
_BOOL_NOTICE_FIELDS = _NOTICE_FACT_KEYS - {"balance_deadline", "viewing_deadline"}


@lru_cache(maxsize=1)
def _machine_schema_validator() -> Draft202012Validator:
    """Load and compile the packaged Draft 2020-12 contract once."""
    schema_bytes = resources.files("auction_mcp_assets").joinpath(
        "evidence_bundle_schema.json"
    ).read_bytes()
    schema = parse_json_bytes(schema_bytes)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_ASSET_INVALID",
            "内置证据包JSON Schema无效",
        ) from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_machine_readable_schema(value: dict[str, Any]) -> None:
    """Validate a bundle against the packaged machine-readable schema."""
    errors = sorted(
        _machine_schema_validator().iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        raise EvidenceBundleError(
            "EVIDENCE_JSON_SCHEMA_INVALID",
            "证据包不符合Draft 2020-12 JSON Schema",
            {"path": error.json_path, "validator": str(error.validator)},
        )


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", f"{field}必须是对象", {"field": field}
        )
    return value


def _require_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID",
            f"{field}字段集合不符合契约",
            {
                "field": field,
                "missing": sorted(expected - actual),
                "unexpected": sorted(actual - expected),
            },
        )


def _require_input_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    field: str,
) -> None:
    actual = set(value)
    if not required <= actual or actual - required - optional:
        raise EvidenceBundleError(
            "EVIDENCE_PROVIDER_CONTRACT_DRIFT",
            f"{field} provider字段发生漂移",
            {
                "field": field,
                "missing": sorted(required - actual),
                "unexpected": sorted(actual - required - optional),
            },
        )


def _text(value: Any, field: str, *, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", f"{field}不是有效文本", {"field": field}
        )
    return value.strip()


def _optional_text(value: Any, field: str, *, maximum: int = 20_000) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=maximum)


def _decimal_string(
    value: Any,
    field: str,
    *,
    places: str = "0.01",
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        parsed = None
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            parsed = None
    if parsed is None or not parsed.is_finite() or abs(parsed) > Decimal("1e15"):
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_DECIMAL", f"{field}必须是有限Decimal", {"field": field}
        )
    return format(parsed.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def _normalize_analysis_inputs(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "expected_address",
        "item_id",
        "screenshot_area_sqm",
        "screenshot_starting_price_yuan",
        "screenshot_title",
    }
    _require_exact_keys(value, allowed, "analysis_inputs")
    item_id = _text(value["item_id"], "analysis_inputs.item_id", maximum=20)
    if not item_id.isdigit() or not 8 <= len(item_id) <= 20:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", "item_id必须是8至20位数字"
        )
    return {
        "expected_address": _optional_text(
            value["expected_address"], "analysis_inputs.expected_address", maximum=160
        ),
        "item_id": item_id,
        "screenshot_area_sqm": _decimal_string(
            value["screenshot_area_sqm"],
            "analysis_inputs.screenshot_area_sqm",
            allow_none=True,
        ),
        "screenshot_starting_price_yuan": _decimal_string(
            value["screenshot_starting_price_yuan"],
            "analysis_inputs.screenshot_starting_price_yuan",
            allow_none=True,
        ),
        "screenshot_title": _optional_text(
            value["screenshot_title"], "analysis_inputs.screenshot_title", maximum=160
        ),
    }


def _normalize_detail(value: dict[str, Any], item_id: str) -> dict[str, Any]:
    _require_input_keys(
        value,
        required=_DETAIL_REQUIRED_INPUT_KEYS,
        optional=_DETAIL_INPUT_KEYS - _DETAIL_REQUIRED_INPUT_KEYS,
        field="detail",
    )
    if str(value["itemId"]) != item_id:
        raise EvidenceBundleError(
            "EVIDENCE_CONFLICT", "阿里详情itemId与分析输入冲突"
        )
    result: dict[str, Any] = {}
    for key in sorted(_DETAIL_PAYLOAD_KEYS):
        if key in _MONEY_FIELDS:
            result[key] = _decimal_string(
                value.get(key), f"detail.{key}", allow_none=key != "startingPriceYuan"
            )
        elif key in _DETAIL_REQUIRED_INPUT_KEYS:
            result[key] = _text(value.get(key), f"detail.{key}")
        else:
            result[key] = _optional_text(value.get(key), f"detail.{key}")
    return result


def _normalize_notice(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    _require_input_keys(
        value,
        required=_NOTICE_FACT_KEYS | {"source_url"},
        optional=set(),
        field="notice",
    )
    result: dict[str, Any] = {}
    for key in sorted(_NOTICE_FACT_KEYS):
        if key in _BOOL_NOTICE_FIELDS:
            if not isinstance(value[key], bool):
                raise EvidenceBundleError(
                    "EVIDENCE_SCHEMA_INVALID", f"notice.{key}必须是布尔值"
                )
            result[key] = value[key]
        else:
            result[key] = _optional_text(value[key], f"notice.{key}", maximum=40)
    return _text(value["source_url"], "notice.source_url", maximum=500), result


def _normalize_community(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    _require_input_keys(
        value,
        required=_COMMUNITY_PAYLOAD_KEYS | {"source_url"},
        optional=set(),
        field="community",
    )
    result = {
        key: _text(value[key], f"community.{key}", maximum=200)
        for key in sorted(_COMMUNITY_PAYLOAD_KEYS)
    }
    if not result["xiaoqu_id"].isdigit() or not 10 <= len(result["xiaoqu_id"]) <= 20:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", "xiaoqu_id必须是10至20位数字"
        )
    return _text(value["source_url"], "community.source_url", maximum=500), result


def _normalize_market(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    _require_input_keys(
        value,
        required={"detail", "listing_source_url", "listings", "status"},
        optional=set(),
        field="market",
    )
    if value["status"] != "OK":
        raise EvidenceBundleError(
            "EVIDENCE_PROVIDER_NOT_OK", "贝壳市场provider结果不是OK"
        )
    detail = _require_object(value["detail"], "market.detail")
    _require_input_keys(
        detail,
        required=_MARKET_DETAIL_KEYS,
        optional=set(),
        field="market.detail",
    )
    facts = _require_object(detail["facts"], "market.detail.facts")
    normalized_facts: dict[str, str] = {}
    for key in sorted(facts):
        normalized_facts[_text(key, "market.detail.facts.key", maximum=80)] = _text(
            facts[key], f"market.detail.facts.{key}", maximum=160
        )
    normalized_detail = {
        "address": _text(detail["address"], "market.detail.address", maximum=200),
        "facts": normalized_facts,
        "listing_average_unit_price_yuan": _decimal_string(
            detail["listing_average_unit_price_yuan"],
            "market.detail.listing_average_unit_price_yuan",
        ),
        "name": _text(detail["name"], "market.detail.name", maximum=160),
        "source_url": _text(detail["source_url"], "market.detail.source_url", maximum=500),
        "xiaoqu_id": _text(detail["xiaoqu_id"], "market.detail.xiaoqu_id", maximum=20),
    }
    listings = value["listings"]
    if not isinstance(listings, list) or not 1 <= len(listings) <= 30:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", "market.listings数量必须为1至30"
        )
    normalized_listings: list[dict[str, Any]] = []
    for index, listing_value in enumerate(listings):
        listing = _require_object(listing_value, f"market.listings[{index}]")
        _require_input_keys(
            listing,
            required=_LISTING_KEYS,
            optional=set(),
            field=f"market.listings[{index}]",
        )
        normalized_listings.append(
            {
                "area_sqm": _decimal_string(
                    listing["area_sqm"], f"market.listings[{index}].area_sqm"
                ),
                "house_info": _text(
                    listing["house_info"], f"market.listings[{index}].house_info", maximum=500
                ),
                "source_url": _text(
                    listing["source_url"], f"market.listings[{index}].source_url", maximum=500
                ),
                "title": _text(
                    listing["title"], f"market.listings[{index}].title", maximum=200
                ),
                "total_price_wan": _decimal_string(
                    listing["total_price_wan"],
                    f"market.listings[{index}].total_price_wan",
                ),
                "unit_price_yuan": _decimal_string(
                    listing["unit_price_yuan"],
                    f"market.listings[{index}].unit_price_yuan",
                ),
            }
        )
    normalized_listings.sort(key=lambda item: item["source_url"])
    if len({item["source_url"] for item in normalized_listings}) != len(
        normalized_listings
    ):
        raise EvidenceBundleError(
            "EVIDENCE_DUPLICATE_SAMPLE", "贝壳挂牌样本URL重复"
        )
    return _text(value["listing_source_url"], "market.listing_source_url", maximum=500), {
        "detail": normalized_detail,
        "listings": normalized_listings,
        "status": "OK",
    }


def _collection_times(value: str | dict[str, str]) -> dict[str, str]:
    if isinstance(value, str):
        normalized = normalize_utc_timestamp(value)
        return {name: normalized for name in SEGMENT_ORDER}
    if not isinstance(value, dict) or set(value) != set(SEGMENT_ORDER):
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_INVALID", "collected_at必须覆盖全部四个证据段"
        )
    result = {name: normalize_utc_timestamp(value[name]) for name in SEGMENT_ORDER}
    previous = None
    for name in SEGMENT_ORDER:
        current = parse_canonical_utc(result[name])
        if previous is not None and current < previous:
            raise EvidenceBundleError(
                "EVIDENCE_TIME_ROLLBACK", "证据采集时间发生倒退", {"segment": name}
            )
        previous = current
    return result


def _segment(
    name: str,
    source_url: str,
    collected_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    provider, provider_version, schema_version = SEGMENT_CONTRACTS[name]
    base = {
        "collected_at": collected_at,
        "name": name,
        "payload": payload,
        "provider": provider,
        "provider_version": provider_version,
        "schema_version": schema_version,
        "source_url": source_url,
    }
    return {**base, "sha256": sha256_hex(canonical_json_bytes(base))}


def create_evidence_bundle(
    *,
    detail: dict[str, Any],
    notice: dict[str, Any],
    community: dict[str, Any],
    market: dict[str, Any],
    analysis_inputs: dict[str, Any],
    collected_at: str | dict[str, str],
    provenance_mode: str,
    provenance_note: str,
    live_collection: bool,
) -> dict[str, Any]:
    """Normalize provider results into one verified canonical bundle."""
    raw = {
        "analysis_inputs": analysis_inputs,
        "community": community,
        "detail": detail,
        "market": market,
        "notice": notice,
        "provenance_note": provenance_note,
    }
    scan_sensitive_data(raw)
    if provenance_mode not in ALLOWED_PROVENANCE_MODES:
        raise EvidenceBundleError(
            "EVIDENCE_PROVENANCE_INVALID", "证据来源模式未知"
        )
    if not isinstance(live_collection, bool):
        raise EvidenceBundleError(
            "EVIDENCE_PROVENANCE_INVALID", "live_collection必须是布尔值"
        )
    if provenance_mode == "ANONYMIZED_HISTORICAL_FIXTURE" and live_collection:
        raise EvidenceBundleError(
            "EVIDENCE_PROVENANCE_CONFLICT", "历史fixture不得声明为Live采集"
        )
    note = _text(provenance_note, "provenance_note", maximum=240)
    inputs = _normalize_analysis_inputs(analysis_inputs)
    item_id = inputs["item_id"]
    normalized_detail = _normalize_detail(_require_object(detail, "detail"), item_id)
    notice_url, normalized_notice = _normalize_notice(_require_object(notice, "notice"))
    community_url, normalized_community = _normalize_community(
        _require_object(community, "community")
    )
    market_url, normalized_market = _normalize_market(_require_object(market, "market"))
    times = _collection_times(collected_at)
    segments = [
        _segment("ali_item_detail", normalized_detail["url"], times["ali_item_detail"], normalized_detail),
        _segment("court_notice_facts", notice_url, times["court_notice_facts"], normalized_notice),
        _segment("beike_community", community_url, times["beike_community"], normalized_community),
        _segment("beike_market", market_url, times["beike_market"], normalized_market),
    ]
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
        for segment in segments
    ]
    manifest_base = {"segments": manifest_segments}
    manifest = {
        **manifest_base,
        "manifest_sha256": sha256_hex(canonical_json_bytes(manifest_base)),
    }
    bundle_base = {
        "$schema": SCHEMA_ID,
        "analysis_inputs": inputs,
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "manifest": manifest,
        "provenance": {
            "live_collection": live_collection,
            "mode": provenance_mode,
            "note": note,
        },
        "segments": segments,
    }
    bundle = {
        **bundle_base,
        "integrity": {
            "algorithm": "sha256",
            "bundle_sha256": sha256_hex(canonical_json_bytes(bundle_base)),
        },
    }
    verify_evidence_bundle(bundle)
    return bundle


def _validate_provenance(value: Any) -> None:
    provenance = _require_object(value, "provenance")
    _require_exact_keys(provenance, {"live_collection", "mode", "note"}, "provenance")
    if provenance["mode"] not in ALLOWED_PROVENANCE_MODES:
        raise EvidenceBundleError("EVIDENCE_PROVENANCE_INVALID", "证据来源模式未知")
    if not isinstance(provenance["live_collection"], bool):
        raise EvidenceBundleError("EVIDENCE_PROVENANCE_INVALID", "live_collection必须是布尔值")
    _text(provenance["note"], "provenance.note", maximum=240)
    if provenance["mode"] == "ANONYMIZED_HISTORICAL_FIXTURE" and provenance["live_collection"]:
        raise EvidenceBundleError(
            "EVIDENCE_PROVENANCE_CONFLICT", "历史fixture不得声明为Live采集"
        )


def _validate_payload(name: str, payload: Any) -> None:
    value = _require_object(payload, f"segments.{name}.payload")
    if name == "ali_item_detail":
        _require_exact_keys(value, _DETAIL_PAYLOAD_KEYS, f"segments.{name}.payload")
        _normalize_detail({**value, "source": "ali_pc_browser"}, str(value["itemId"]))
    elif name == "court_notice_facts":
        _require_exact_keys(value, _NOTICE_FACT_KEYS, f"segments.{name}.payload")
        _normalize_notice({**value, "source_url": "https://sf.taobao.com/notice_detail/10000.htm?item_id=12345678"})
    elif name == "beike_community":
        _require_exact_keys(value, _COMMUNITY_PAYLOAD_KEYS, f"segments.{name}.payload")
        _normalize_community({**value, "source_url": "https://jiangmen.ke.com/xiaoqu/c1234567890/"})
    elif name == "beike_market":
        _require_exact_keys(value, {"detail", "listings", "status"}, f"segments.{name}.payload")
        _normalize_market({**value, "listing_source_url": "https://jiangmen.ke.com/ershoufang/c1234567890/"})
    else:
        raise EvidenceBundleError("EVIDENCE_UNKNOWN_SEGMENT", "证据段名称未知")


def _validate_listing_urls(market_payload: dict[str, Any]) -> None:
    detail_url = market_payload["detail"]["source_url"]
    parsed_detail = urlparse(detail_url)
    if parsed_detail.scheme != "https" or parsed_detail.hostname != "jiangmen.ke.com":
        raise EvidenceBundleError("EVIDENCE_URL_OUT_OF_SCOPE", "贝壳详情来源URL越界")
    seen: set[str] = set()
    for listing in market_payload["listings"]:
        url = listing["source_url"]
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "jiangmen.ke.com"
            or not parsed.path.startswith("/ershoufang/")
            or not parsed.path.endswith(".html")
            or parsed.query
        ):
            raise EvidenceBundleError("EVIDENCE_URL_OUT_OF_SCOPE", "贝壳挂牌来源URL越界")
        if url in seen:
            raise EvidenceBundleError("EVIDENCE_DUPLICATE_SAMPLE", "贝壳挂牌样本URL重复")
        seen.add(url)


def verify_evidence_bundle(
    bundle: dict[str, Any],
    *,
    original_bytes: bytes | None = None,
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate schema, redlines, URLs, time order, and all integrity layers."""
    value = _require_object(bundle, "bundle")
    _require_exact_keys(value, _TOP_LEVEL_KEYS, "bundle")
    if value["$schema"] != SCHEMA_ID or value["bundle_schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise EvidenceBundleError("EVIDENCE_UNKNOWN_VERSION", "证据包schema版本未知")
    _validate_provenance(value["provenance"])
    inputs = _normalize_analysis_inputs(_require_object(value["analysis_inputs"], "analysis_inputs"))
    segments = value["segments"]
    if not isinstance(segments, list):
        raise EvidenceBundleError("EVIDENCE_SCHEMA_INVALID", "segments必须是列表")
    names = [segment.get("name") if isinstance(segment, dict) else None for segment in segments]
    if len(set(names)) != len(names):
        raise EvidenceBundleError("EVIDENCE_DUPLICATE_SEGMENT", "证据段重复")
    if tuple(names) != SEGMENT_ORDER:
        raise EvidenceBundleError(
            "EVIDENCE_REQUIRED_SEGMENT_MISSING",
            "证据段缺失或顺序不符合契约",
            {"actual": names, "expected": list(SEGMENT_ORDER)},
        )
    previous_time = None
    expected_manifest: list[dict[str, Any]] = []
    segment_map: dict[str, dict[str, Any]] = {}
    segment_keys = {
        "collected_at",
        "name",
        "payload",
        "provider",
        "provider_version",
        "schema_version",
        "sha256",
        "source_url",
    }
    for segment_value in segments:
        segment = _require_object(segment_value, "segment")
        _require_exact_keys(segment, segment_keys, f"segment.{segment['name']}")
        validate_segment_contract(segment)
        current_time = parse_canonical_utc(segment["collected_at"])
        if previous_time is not None and current_time < previous_time:
            raise EvidenceBundleError(
                "EVIDENCE_TIME_ROLLBACK", "证据采集时间发生倒退", {"segment": segment["name"]}
            )
        previous_time = current_time
        _validate_payload(segment["name"], segment["payload"])
        validate_segment_url(
            segment["name"],
            segment["source_url"],
            segment["payload"],
            item_id=inputs["item_id"],
        )
        base = {key: segment[key] for key in segment_keys - {"sha256"}}
        actual_hash = sha256_hex(canonical_json_bytes(base))
        if not hmac.compare_digest(str(segment["sha256"]), actual_hash):
            raise EvidenceBundleError(
                "EVIDENCE_SEGMENT_HASH_MISMATCH", "证据段SHA-256不一致", {"segment": segment["name"]}
            )
        manifest_item = {
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
        expected_manifest.append(manifest_item)
        segment_map[segment["name"]] = segment
    detail_payload = segment_map["ali_item_detail"]["payload"]
    notice_source = segment_map["court_notice_facts"]["source_url"]
    if detail_payload["announcementUrl"] != notice_source:
        raise EvidenceBundleError("EVIDENCE_CONFLICT", "详情公告URL与公告证据段冲突")
    community_payload = segment_map["beike_community"]["payload"]
    market_payload = segment_map["beike_market"]["payload"]
    if community_payload["xiaoqu_id"] != market_payload["detail"]["xiaoqu_id"]:
        raise EvidenceBundleError("EVIDENCE_CONFLICT", "小区ID在证据段之间冲突")
    _validate_listing_urls(market_payload)
    manifest = _require_object(value["manifest"], "manifest")
    _require_exact_keys(manifest, {"manifest_sha256", "segments"}, "manifest")
    if manifest["segments"] != expected_manifest:
        raise EvidenceBundleError("EVIDENCE_MANIFEST_MISMATCH", "manifest内容与证据段不一致")
    manifest_base = {"segments": expected_manifest}
    expected_manifest_hash = sha256_hex(canonical_json_bytes(manifest_base))
    if not hmac.compare_digest(str(manifest["manifest_sha256"]), expected_manifest_hash):
        raise EvidenceBundleError("EVIDENCE_MANIFEST_HASH_MISMATCH", "manifest SHA-256不一致")
    integrity = _require_object(value["integrity"], "integrity")
    _require_exact_keys(integrity, {"algorithm", "bundle_sha256"}, "integrity")
    if integrity["algorithm"] != "sha256":
        raise EvidenceBundleError("EVIDENCE_UNKNOWN_VERSION", "整包哈希算法未知")
    bundle_base = {key: value[key] for key in _TOP_LEVEL_KEYS - {"integrity"}}
    expected_bundle_hash = sha256_hex(canonical_json_bytes(bundle_base))
    if not hmac.compare_digest(str(integrity["bundle_sha256"]), expected_bundle_hash):
        raise EvidenceBundleError("EVIDENCE_BUNDLE_HASH_MISMATCH", "整包SHA-256不一致")
    if expected_bundle_sha256 is not None and not hmac.compare_digest(
        expected_bundle_sha256, expected_bundle_hash
    ):
        raise EvidenceBundleError(
            "EVIDENCE_CUSTODY_HASH_MISMATCH",
            "整包SHA-256与独立保管的链路摘要不一致",
        )
    scan_sensitive_data(value)
    validate_machine_readable_schema(value)
    canonical = canonical_json_bytes(value)
    if original_bytes is not None and original_bytes != canonical:
        raise EvidenceBundleError(
            "EVIDENCE_NONCANONICAL_BYTES", "证据文件字节不是规范JSON，可能被篡改"
        )
    return {
        "status": "OK",
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_sha256": expected_bundle_hash,
        "manifest_sha256": expected_manifest_hash,
        "segments": len(segments),
        "maximum_bid_yuan": None,
    }


def load_evidence_bundle(
    path: str | Path,
    *,
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    data = Path(path).read_bytes()
    value = parse_json_bytes(data)
    bundle = _require_object(value, "bundle")
    verify_evidence_bundle(
        bundle,
        original_bytes=data,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    return bundle


def write_canonical_json(path: str | Path, value: Any) -> None:
    Path(path).write_bytes(canonical_json_bytes(value))


def _report_json_value(value: Any) -> Any:
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, dict):
        return {key: _report_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_report_json_value(item) for item in value]
    return value


def replay_evidence_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the P3.8 analysis using only verified in-memory evidence."""
    verify_evidence_bundle(bundle)
    segments = {segment["name"]: segment for segment in bundle["segments"]}
    inputs = bundle["analysis_inputs"]
    detail = deepcopy(segments["ali_item_detail"]["payload"])
    notice = {
        **deepcopy(segments["court_notice_facts"]["payload"]),
        "source_url": segments["court_notice_facts"]["source_url"],
    }
    community = {
        **deepcopy(segments["beike_community"]["payload"]),
        "source_url": segments["beike_community"]["source_url"],
    }
    market = deepcopy(segments["beike_market"]["payload"])
    market["listing_source_url"] = segments["beike_market"]["source_url"]
    report = build_asset_analysis(
        item_id=inputs["item_id"],
        detail=detail,
        notice=notice,
        community=community,
        market=market,
        expected_address=inputs["expected_address"],
        screenshot_title=inputs["screenshot_title"],
        screenshot_area_sqm=inputs["screenshot_area_sqm"],
        screenshot_starting_price_yuan=inputs["screenshot_starting_price_yuan"],
        retrieved_at=bundle["segments"][-1]["collected_at"],
    )
    if report.get("maximum_bid_yuan") is not None:
        raise EvidenceBundleError(
            "EVIDENCE_UNSAFE_ANALYSIS", "离线重放不得生成最高出价"
        )
    return _report_json_value(report)


def _change(path: str, before: Any, after: Any) -> dict[str, Any]:
    return {"after": after, "before": before, "path": path}


def _compare_fields(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    prefix: str,
    keys: set[str],
) -> list[dict[str, Any]]:
    return [
        _change(f"{prefix}.{key}", before.get(key), after.get(key))
        for key in sorted(keys)
        if before.get(key) != after.get(key)
    ]


def _schema_changes(
    left: dict[str, Any], right: dict[str, Any]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in ("$schema", "bundle_schema_version"):
        if left.get(key) != right.get(key):
            changes.append(_change(key, left.get(key), right.get(key)))
    left_segments = left.get("segments")
    right_segments = right.get("segments")
    if not isinstance(left_segments, list) or not isinstance(right_segments, list):
        return changes
    left_by_name = {
        item.get("name"): item
        for item in left_segments
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    right_by_name = {
        item.get("name"): item
        for item in right_segments
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for name in sorted(set(left_by_name) & set(right_by_name)):
        changes.extend(
            _compare_fields(
                left_by_name[name],
                right_by_name[name],
                prefix=f"segments.{name}",
                keys={"provider", "provider_version", "schema_version"},
            )
        )
    return sorted(changes, key=lambda item: item["path"])


def ensure_diff_schema_compatible(
    left: dict[str, Any], right: dict[str, Any]
) -> None:
    """Classify schema drift but refuse to interpret incompatible payloads."""
    changes = _schema_changes(left, right)
    if changes:
        raise EvidenceBundleError(
            "EVIDENCE_SCHEMA_CHANGE",
            "证据包schema或provider版本不同，拒绝跨版本字段解释",
            {"category": "schema_changes", "changes": changes},
        )


def diff_evidence_bundles(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    """Return deterministic field-level changes grouped by forensic meaning."""
    ensure_diff_schema_compatible(left, right)
    verify_evidence_bundle(left)
    verify_evidence_bundle(right)
    left_segments = {item["name"]: item for item in left["segments"]}
    right_segments = {item["name"]: item for item in right["segments"]}
    schema_changes: list[dict[str, Any]] = []
    source_changes: list[dict[str, Any]] = []
    for name in SEGMENT_ORDER:
        left_segment = left_segments[name]
        right_segment = right_segments[name]
        source_changes.extend(
            _compare_fields(
                left_segment,
                right_segment,
                prefix=f"segments.{name}",
                keys={"source_url"},
            )
        )
    legal_fact_changes = _compare_fields(
        left_segments["court_notice_facts"]["payload"],
        right_segments["court_notice_facts"]["payload"],
        prefix="segments.court_notice_facts.payload",
        keys=_NOTICE_FACT_KEYS,
    )
    left_detail = left_segments["ali_item_detail"]["payload"]
    right_detail = right_segments["ali_item_detail"]["payload"]
    price_changes = _compare_fields(
        left_detail,
        right_detail,
        prefix="segments.ali_item_detail.payload",
        keys=_MONEY_FIELDS,
    )
    left_market = left_segments["beike_market"]["payload"]
    right_market = right_segments["beike_market"]["payload"]
    price_changes.extend(
        _compare_fields(
            left_market["detail"],
            right_market["detail"],
            prefix="segments.beike_market.payload.detail",
            keys={"listing_average_unit_price_yuan"},
        )
    )
    left_listings = {item["source_url"]: item for item in left_market["listings"]}
    right_listings = {item["source_url"]: item for item in right_market["listings"]}
    sample_additions = [
        {"sample_id": key, "value": right_listings[key]}
        for key in sorted(set(right_listings) - set(left_listings))
    ]
    sample_removals = [
        {"sample_id": key, "value": left_listings[key]}
        for key in sorted(set(left_listings) - set(right_listings))
    ]
    field_changes: list[dict[str, Any]] = []
    listing_price_keys = {"total_price_wan", "unit_price_yuan"}
    for key in sorted(set(left_listings) & set(right_listings)):
        price_changes.extend(
            _compare_fields(
                left_listings[key],
                right_listings[key],
                prefix=f"samples.{key}",
                keys=listing_price_keys,
            )
        )
        field_changes.extend(
            _compare_fields(
                left_listings[key],
                right_listings[key],
                prefix=f"samples.{key}",
                keys=_LISTING_KEYS - listing_price_keys - {"source_url"},
            )
        )
    field_changes.extend(
        _compare_fields(
            left_detail,
            right_detail,
            prefix="segments.ali_item_detail.payload",
            keys=_DETAIL_PAYLOAD_KEYS - _MONEY_FIELDS,
        )
    )
    field_changes.extend(
        _compare_fields(
            left_segments["beike_community"]["payload"],
            right_segments["beike_community"]["payload"],
            prefix="segments.beike_community.payload",
            keys=_COMMUNITY_PAYLOAD_KEYS,
        )
    )
    categories = {
        "field_changes": sorted(field_changes, key=lambda item: item["path"]),
        "legal_fact_changes": sorted(legal_fact_changes, key=lambda item: item["path"]),
        "price_changes": sorted(price_changes, key=lambda item: item["path"]),
        "sample_additions": sample_additions,
        "sample_removals": sample_removals,
        "schema_changes": sorted(schema_changes, key=lambda item: item["path"]),
        "source_changes": sorted(source_changes, key=lambda item: item["path"]),
    }
    counts = {key: len(value) for key, value in categories.items()}
    return {
        "categories": categories,
        "changed": any(counts.values()),
        "left_bundle_sha256": left["integrity"]["bundle_sha256"],
        "maximum_bid_yuan": None,
        "right_bundle_sha256": right["integrity"]["bundle_sha256"],
        "status": "OK",
        "summary": counts,
    }
