"""Pure fail-closed decisions shared by the server and offline guards.

This module deliberately has no provider, browser, MCP, or filesystem side
effects.  Keeping the critical decisions small makes their branch coverage and
mutation resistance independently enforceable.
"""
from __future__ import annotations

from typing import Any


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def area_structure_error(
    province: str | None,
    city: str | None,
    district: str | None,
) -> str | None:
    """Return the first missing-parent error, or ``None`` when valid."""
    if city is not None and province is None:
        return "city_requires_province"
    if district is not None and city is None:
        return "district_requires_city"
    return None


def ali_city_resolution_allowed(
    province_code: str,
    city_code: str | None,
    province: str,
    city: str,
) -> bool:
    """Reject Ali city resolution downgrades except exact municipalities."""
    if city_code is None:
        return False
    if city_code != province_code:
        return True
    municipality = {
        "11": "北京",
        "12": "天津",
        "31": "上海",
        "50": "重庆",
    }.get(province_code[:2])
    if municipality is None:
        return False
    aliases = {municipality, f"{municipality}市"}
    return province in aliases and city in aliases


def is_local_socket_address(address: Any) -> bool:
    """Allow local IPC and loopback while rejecting every external socket."""
    if isinstance(address, str):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    return address[0] in LOOPBACK_HOSTS
