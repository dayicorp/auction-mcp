"""单元测试: 垃圾结果守门 validate_location_scoped (零网络).

守门规则: items 的 locationCode 应有 ≥80% 落在请求的城市/省前缀下.
不通过即视为阿里返回了乱掺数据 (e.g. 不认编码时的全国回退).
"""
from __future__ import annotations

from ali_h5_client import validate_location_scoped


def _items(codes):
    """构造最小 item 列表, locationCode 放在 extraMap 里 (与真实响应结构一致)."""
    return [{"extraMap": {"locationCode": c}} for c in codes]


def test_validation_all_in_scope():
    """全部 locationCode 命中前缀 → ok=True."""
    v = validate_location_scoped(_items([330621] * 9 + [330602]), "3306")
    assert v["ok"] is True
    assert v["matched"] == 10
    assert v["total"] == 10
    assert v["sample_off_prefix"] == []


def test_validation_garbage_nationwide():
    """阿里不认编码时返回的全国乱掺样本: 0% 命中浙江前缀 → ok=False."""
    bad_codes = [150602, 440304, 210203, 330106, 330105, 330183, 320903, 310106, 330109, 320114]
    v = validate_location_scoped(_items(bad_codes), "3306")
    assert v["ok"] is False
    assert v["matched"] == 0
    assert len(v["sample_off_prefix"]) > 0
    # 330106/330105/330183/330109 是浙江其他城市, 仍不以 3306 开头, 应被列出
    for off in ["150602", "440304", "210203"]:
        assert off in v["sample_off_prefix"]


def test_validation_threshold_boundary_80_percent():
    """80% 是默认 boundary: 8/10 命中应 ok, 7/10 不 ok."""
    v_ok = validate_location_scoped(_items([330621] * 8 + [150602, 440304]), "3306")
    assert v_ok["ok"] is True

    v_bad = validate_location_scoped(_items([330621] * 7 + [150602, 440304, 210203]), "3306")
    assert v_bad["ok"] is False


def test_validation_empty_items_ok():
    """没 items 视为 ok (空集没东西可乱)."""
    v = validate_location_scoped([], "3306")
    assert v["ok"] is True
    assert v["total"] == 0


def test_validation_2digit_province_prefix():
    """前缀也支持 2 位省级 (e.g. '33' 浙江全省)."""
    v = validate_location_scoped(
        _items([330621, 330106, 330183, 330109, 330602]),
        "33",
    )
    assert v["ok"] is True
    assert v["matched"] == 5


def test_validation_item_top_level_code_fallback():
    """item 顶层 locationCode 也算 (extraMap 缺失时)."""
    items = [
        {"locationCode": 330621},
        {"locationCode": 330602},
        {"extraMap": {"locationCode": 330621}},
    ]
    v = validate_location_scoped(items, "3306")
    assert v["ok"] is True
    assert v["matched"] == 3


def test_validation_sample_off_prefix_capped_at_10():
    """sample 最多 10 条, 防止日志/响应膨胀."""
    bad = [str(i).zfill(6) for i in range(990000, 990050)]  # 50 个外前缀
    v = validate_location_scoped(_items(bad), "3306")
    assert v["ok"] is False
    assert len(v["sample_off_prefix"]) == 10
