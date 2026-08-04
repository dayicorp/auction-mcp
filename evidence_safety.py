"""Fail-closed primitives for portable auction evidence bundles.

This module contains the small security-critical core used before any evidence
is trusted.  It deliberately has no browser, provider, network, or MCP imports.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse


BUNDLE_SCHEMA_VERSION = "1.0.0"
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_CONTAINER_ITEMS = 10_000
SEGMENT_ORDER = (
    "ali_item_detail",
    "court_notice_facts",
    "beike_community",
    "beike_market",
)
SEGMENT_CONTRACTS = {
    "ali_item_detail": ("ali_pc_browser", "1.0.0", "1.0.0"),
    "court_notice_facts": ("public_court_notice", "1.0.0", "1.0.0"),
    "beike_community": ("beike_browser", "1.0.0", "1.0.0"),
    "beike_market": ("beike_browser", "1.0.0", "1.0.0"),
}

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|cookie|cookies|password|passwd|secret|token|"
    r"access_token|refresh_token|storage_state|local_storage|session_storage|"
    r"browser_storage|notice_body|notice_text|notice_html|raw_notice)(?:$|_)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"\b(?:cookie|authorization|bearer|access[_ -]?token|refresh[_ -]?token)\b",
    re.IGNORECASE,
)
_CN_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
)
_LONG_NUMBER = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_IDENTIFIER_KEYS = {"item_id", "itemid", "xiaoqu_id", "source_url"}
_SENSITIVE_NORMALIZED_KEYS = {
    "authorization",
    "browserstorage",
    "cookie",
    "cookies",
    "localstorage",
    "password",
    "rawnotice",
    "refreshtoken",
    "secret",
    "sessionstorage",
    "storagestate",
    "token",
    "accesstoken",
}


class EvidenceBundleError(RuntimeError):
    """Bounded public failure for unsafe or unverifiable evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}

    def as_result(self) -> dict[str, Any]:
        return {
            "status": "STOPPED",
            "decision": "NEEDS_REVIEW",
            "maximum_bid_yuan": None,
            "error": {
                "code": self.code,
                "message": self.message,
                "diagnostics": self.diagnostics,
            },
        }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_json_value(value: Any, *, path: str, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise EvidenceBundleError(
            "EVIDENCE_INPUT_TOO_DEEP", "证据JSON嵌套层级超过安全上限", {"path": path}
        )
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 10**24:
            raise EvidenceBundleError(
                "EVIDENCE_INTEGER_OUT_OF_RANGE", "证据整数超过安全范围", {"path": path}
            )
        return
    if isinstance(value, float):
        raise EvidenceBundleError(
            "EVIDENCE_FLOAT_FORBIDDEN",
            "证据包禁止浮点数、NaN或Infinity，金额和面积必须使用Decimal字符串",
            {"path": path},
        )
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise EvidenceBundleError(
                "EVIDENCE_INPUT_TOO_LARGE", "证据列表超过安全上限", {"path": path}
            )
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise EvidenceBundleError(
                "EVIDENCE_INPUT_TOO_LARGE", "证据对象超过安全上限", {"path": path}
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceBundleError(
                    "EVIDENCE_INVALID_JSON_TYPE", "证据对象键必须是字符串", {"path": path}
                )
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise EvidenceBundleError(
        "EVIDENCE_INVALID_JSON_TYPE",
        "证据包只允许标准JSON类型",
        {"path": path, "type": type(value).__name__},
    )


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value, path="$", depth=0)
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    data = rendered.encode("utf-8")
    if len(data) > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError(
            "EVIDENCE_INPUT_TOO_LARGE",
            "证据包超过1 MiB安全上限",
            {"bytes": len(data), "maximum_bytes": MAX_BUNDLE_BYTES},
        )
    return data


def parse_json_bytes(data: bytes) -> Any:
    if len(data) > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError(
            "EVIDENCE_INPUT_TOO_LARGE",
            "证据包超过1 MiB安全上限",
            {"bytes": len(data), "maximum_bytes": MAX_BUNDLE_BYTES},
        )

    def reject_constant(value: str) -> None:
        raise EvidenceBundleError(
            "EVIDENCE_NONFINITE_NUMBER", "证据包禁止NaN或Infinity", {"value": value}
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceBundleError(
                    "EVIDENCE_DUPLICATE_KEY", "证据JSON包含重复键", {"key": key}
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except UnicodeDecodeError as exc:
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_UTF8", "证据包必须是UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_JSON", "证据包不是有效JSON", {"line": exc.lineno}
        ) from exc
    canonical_json_bytes(value)
    return value


def normalize_utc_timestamp(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_TIME", "采集时间必须是带时区的ISO-8601时间"
        ) from exc
    if parsed.tzinfo is None:
        raise EvidenceBundleError(
            "EVIDENCE_INVALID_TIME", "采集时间缺少时区"
        )
    utc = parsed.astimezone(timezone.utc)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_canonical_utc(value: str) -> datetime:
    normalized = normalize_utc_timestamp(value)
    if normalized != value:
        raise EvidenceBundleError(
            "EVIDENCE_NONCANONICAL_TIME", "证据采集时间不是规范UTC Z格式", {"value": value}
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _luhn_valid(number: str) -> bool:
    total = 0
    parity = len(number) % 2
    for index, character in enumerate(number):
        digit = int(character)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _sensitive_key(key: str, path: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    notice_body = path == "$.notice" and normalized in {
        "body",
        "content",
        "html",
        "text",
    }
    return (
        _SENSITIVE_KEY.search(key) is not None
        or normalized in _SENSITIVE_NORMALIZED_KEYS
        or notice_body
    )


def scan_sensitive_data(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _sensitive_key(key, path):
                raise EvidenceBundleError(
                    "EVIDENCE_SENSITIVE_DATA",
                    "证据包包含禁止字段",
                    {"kind": "forbidden_key", "path": f"{path}.{key}"},
                )
            scan_sensitive_data(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            scan_sensitive_data(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    leaf = path.rsplit(".", 1)[-1].lower()
    if _SENSITIVE_VALUE.search(value):
        kind = "credential_header"
    elif _CN_MOBILE.search(value):
        kind = "cn_mobile_number"
    elif _CN_ID_CARD.search(value):
        kind = "cn_id_card"
    elif leaf not in _IDENTIFIER_KEYS and any(
        _luhn_valid(match.group(0)) for match in _LONG_NUMBER.finditer(value)
    ):
        kind = "bank_card"
    else:
        return
    raise EvidenceBundleError(
        "EVIDENCE_SENSITIVE_DATA",
        "证据包命中敏感信息红线",
        {"kind": kind, "path": path},
    )


def validate_segment_url(
    name: str,
    source_url: str,
    payload: dict[str, Any],
    *,
    item_id: str,
) -> None:
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise EvidenceBundleError(
            "EVIDENCE_URL_OUT_OF_SCOPE", "证据来源URL越界", {"segment": name}
        )
    valid = False
    if name == "ali_item_detail":
        valid = (
            parsed.hostname == "sf-item.taobao.com"
            and parsed.path == f"/sf_item/{item_id}.htm"
            and not parsed.query
            and str(payload.get("itemId")) == item_id
        )
    elif name == "court_notice_facts":
        valid = (
            parsed.hostname == "sf.taobao.com"
            and re.fullmatch(r"/notice_detail/[0-9]{5,20}\.htm", parsed.path)
            is not None
            and parse_qs(parsed.query).get("item_id") == [item_id]
        )
    elif name == "beike_community":
        valid = (
            parsed.hostname == "jiangmen.ke.com"
            and re.fullmatch(r"/xiaoqu/c?[0-9]{10,20}/", parsed.path) is not None
            and not parsed.query
        )
    elif name == "beike_market":
        valid = (
            parsed.hostname == "jiangmen.ke.com"
            and re.fullmatch(r"/ershoufang/c[0-9]{10,20}/", parsed.path) is not None
            and not parsed.query
        )
    if not valid:
        raise EvidenceBundleError(
            "EVIDENCE_URL_OUT_OF_SCOPE", "证据来源URL越界", {"segment": name}
        )


def validate_segment_contract(segment: dict[str, Any]) -> None:
    name = segment.get("name")
    contract = SEGMENT_CONTRACTS.get(name)
    if contract is None:
        raise EvidenceBundleError(
            "EVIDENCE_UNKNOWN_SEGMENT", "证据段名称未知", {"segment": name}
        )
    actual = (
        segment.get("provider"),
        segment.get("provider_version"),
        segment.get("schema_version"),
    )
    if actual != contract:
        raise EvidenceBundleError(
            "EVIDENCE_UNKNOWN_VERSION",
            "证据段provider或schema版本未知",
            {"segment": name, "actual": list(actual)},
        )
