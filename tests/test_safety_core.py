"""Exhaustive branch tests for the side-effect-free fail-closed core."""
from __future__ import annotations

import pytest

from safety_core import (
    ali_city_resolution_allowed,
    area_structure_error,
    is_local_socket_address,
)


@pytest.mark.parametrize(
    ("province", "city", "district", "expected"),
    [
        (None, None, None, None),
        (None, "广州市", None, "city_requires_province"),
        (None, "广州市", "天河区", "city_requires_province"),
        ("广东", None, "天河区", "district_requires_city"),
        ("广东", "广州市", "天河区", None),
    ],
)
def test_area_structure_error(province, city, district, expected):
    assert area_structure_error(province, city, district) == expected


@pytest.mark.parametrize(
    ("province_code", "city_code", "province", "city", "expected"),
    [
        ("440000", None, "广东", "广州市", False),
        ("440000", "440100", "广东", "广州市", True),
        ("440000", "440000", "广东", "广东市", False),
        ("110000", "110000", "北京", "北京市", True),
        ("110000", "110000", "北京", "北京区", False),
        ("110000", "110000", "河北", "北京市", False),
    ],
)
def test_ali_city_resolution_allowed(
    province_code, city_code, province, city, expected
):
    assert (
        ali_city_resolution_allowed(province_code, city_code, province, city)
        is expected
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("/tmp/mcp.sock", True),
        (("127.0.0.1", 1234), True),
        (("::1", 1234), True),
        (("localhost", 1234), True),
        (("203.0.113.1", 443), False),
        ((), False),
        (None, False),
    ],
)
def test_is_local_socket_address(address, expected):
    assert is_local_socket_address(address) is expected
