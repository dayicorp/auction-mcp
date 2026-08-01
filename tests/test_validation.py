"""单元测试: 垃圾结果守门 validate_location_scoped + derive_ali_scope_prefix (零网络).

守门规则: items 的 locationCode 应有 ≥80% 落在请求的城市/省前缀下.
不通过即视为阿里返回了乱掺数据 (e.g. 不认编码时的全国回退).
"""
from __future__ import annotations

import json
from typing import Any

from ali_h5_client import validate_location_scoped, derive_ali_scope_prefix


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


# ============================================================ derive_ali_scope_prefix 纯函数测试


def test_derive_province_2digit():
    """2 位省编码 → 原样返回."""
    assert derive_ali_scope_prefix("44") == "44"


def test_derive_province_6digit():
    """XX0000 省级编码 → 2 位省前缀."""
    assert derive_ali_scope_prefix("440000") == "44"


def test_derive_city_4digit():
    """4 位市编码 → 原样返回."""
    assert derive_ali_scope_prefix("4401") == "4401"


def test_derive_city_6digit():
    """XXXX00 市级编码 → 4 位城市前缀."""
    assert derive_ali_scope_prefix("440100") == "4401"


def test_derive_district_6digit():
    """XXXXXX 区县级编码 → 前 4 位城市范围."""
    assert derive_ali_scope_prefix("440106") == "4401"


def test_derive_none_input():
    """None 输入 → None."""
    assert derive_ali_scope_prefix(None) is None


def test_derive_empty_string():
    """空串 → None."""
    assert derive_ali_scope_prefix("") is None


def test_derive_non_digit():
    """非纯数字 → None."""
    assert derive_ali_scope_prefix("invalid") is None
    assert derive_ali_scope_prefix("44ab") is None


def test_derive_invalid_length():
    """长度不是 2/4/6 → None (禁止静默截断)."""
    assert derive_ali_scope_prefix("4") is None
    assert derive_ali_scope_prefix("440") is None
    assert derive_ali_scope_prefix("44010") is None
    assert derive_ali_scope_prefix("4401000") is None
    assert derive_ali_scope_prefix("44010000") is None


# ============================================================ Ali 省级守门回归测试 (monkeypatch, 零网络)


def _make_ali_success_response(location_codes_in_items: list[str]) -> dict[str, Any]:
    """构造一个 ali.search_judicial 的 SUCCESS 响应, items 带指定 locationCode."""
    content_list = [
        {"itemId": f"item_{i}",
         "extraMap": {"locationCode": lc, "title": f"测试标的{i}"}}
        for i, lc in enumerate(location_codes_in_items)
    ]
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "data": {
                "scenes": [{
                    "schemeList": [{
                        "contentList": content_list,
                        "totalCount": len(content_list),
                        "page": 1,
                        "pageSize": 10,
                    }]
                }]
            }
        },
    }


def test_province_guard_guangdong_pass(monkeypatch):
    """省级查询 province='广东' → 守门前缀应为 '44', 广东下级结果不被误杀.

    修复前 bug: code='440000', code[:4]='4400' → 广东真实编码 4401xx/4403xx 全不匹配 → 误报垃圾.
    修复后: derive_ali_scope_prefix('440000')='44' → 4401xx/4403xx 都以 '44' 开头 → 通过.
    """
    import server

    captured: dict[str, Any] = {}

    def fake_search_judicial(**kwargs):
        captured.update(kwargs)
        # 模拟阿里返回广东下级城市的合法结果
        return _make_ali_success_response(["440103", "440106", "440305", "440306",
                                           "440104", "440105", "440111", "440112",
                                           "440307", "440308"])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(province="广东")

    # 断言不返回 ali_returned_unscoped_results
    assert result.get("error") != "ali_returned_unscoped_results", \
        f"省级合法结果不应被守门拒绝: {result}"
    # 断言 location_codes 参数为 ["440000"]
    assert captured.get("location_codes") == ["440000"]
    # 断言守门使用前缀 "44" (通过 validated=True 间接证明)
    assert result.get("validated") is True
    assert result.get("count") == 10


def test_province_guard_garbage_still_rejected(monkeypatch):
    """省级查询垃圾结果守门仍有效: 大部分编码不以 '44' 开头 → 仍报 ali_returned_unscoped_results.

    证明修复没有关闭垃圾结果守门.
    """
    import server

    def fake_search_judicial(**kwargs):
        # 模拟阿里返回全国乱掺数据 (大部分不是广东)
        return _make_ali_success_response(["150602", "440304", "210203", "330106",
                                           "330105", "330183", "320903", "310106",
                                           "330109", "320114"])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(province="广东")

    # 断言仍返回 ali_returned_unscoped_results
    assert result.get("error") == "ali_returned_unscoped_results"
    assert result.get("items") == []
    assert result["diagnostics"]["expected_prefix"] == "44"


def test_city_guard_guangzhou_4digit_prefix(monkeypatch):
    """城市级查询广州市 (440100) → 守门前缀应为 '4401', 城市级守门语义不变."""
    import server

    captured: dict[str, Any] = {}

    def fake_search_judicial(**kwargs):
        captured.update(kwargs)
        # 模拟广州市下级合法结果
        return _make_ali_success_response(["440103", "440106", "440104", "440105",
                                           "440111", "440112", "440113", "440114",
                                           "440115", "440117"])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(province="广东", city="广州市")

    # 断言守门通过
    assert result.get("error") != "ali_returned_unscoped_results"
    assert result.get("validated") is True
    # 断言 location_codes 为广州市编码
    assert captured.get("location_codes") == ["440100"]


def test_city_guard_rejects_other_city_results(monkeypatch):
    """城市级查询广州, 但阿里返回深圳结果 → 守门拒绝 (4位前缀区分城市)."""
    import server

    def fake_search_judicial(**kwargs):
        # 模拟阿里返回深圳 (4403xx) 为主的结果, 不是广州 (4401xx)
        return _make_ali_success_response(["440303", "440304", "440305", "440306",
                                           "440307", "440308", "440309", "440310",
                                           "440311", "440103"])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(province="广东", city="广州市")

    # 9/10 不以 4401 开头 → 守门拒绝
    assert result.get("error") == "ali_returned_unscoped_results"
    assert result["diagnostics"]["expected_prefix"] == "4401"


def test_invalid_city_fail_closed_guard(monkeypatch):
    """无效 city='不存在市' 在 Ali 单源入口解析预检即被拒绝, 不发起 provider 调用.

    场景: '不存在市' 在 GB2260 2020 和 JD 地区树均无法解析
    → _validate_ali_pc_resolution 识别为 city 解析失败 → 返回 region_resolution_failed.
    P0 的守门 fail-closed 逻辑保留作二重防线, 但正常不会触发了.
    """
    import server

    ali_calls = {"count": 0}

    def fake_search_judicial(**kwargs):
        ali_calls["count"] += 1
        return _make_ali_success_response(["440103", "440106", "440305", "440306",
                                           "440104", "440105", "440111", "440112",
                                           "440307", "440308"])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(province="广东", city="不存在市")

    # 断言在解析预检层即被拒绝, 不发起网络调用
    assert result.get("error") == "region_resolution_failed"
    assert result["diagnostics"]["resolution"] == "city"
    assert ali_calls["count"] == 0


# ============================================================ Ali Advanced filter 透传


def test_ali_search_passes_zc_biz_types_to_provider(monkeypatch):
    """公开 Ali Advanced 工具应原样透传资产类型编码列表."""
    import server

    captured: dict[str, Any] = {}

    def fake_search_judicial(**kwargs):
        captured.update(kwargs)
        return _make_ali_success_response([])

    monkeypatch.setattr(server.ali, "search_judicial", fake_search_judicial)

    result = server.ali_search_judicial(zc_biz_types=["1", "7"])

    assert result.get("error") is None
    assert captured["zc_biz_types"] == ["1", "7"]


def test_ali_client_serializes_zc_biz_types_filter(monkeypatch):
    """Ali client 应把资产类型编码序列化为 mtop filter 的 zcBizTypes."""
    from ali_h5_client import AliH5Client

    client = AliH5Client()
    captured: dict[str, Any] = {}

    def fake_call_mtop(api, version, data, method="POST"):
        captured.update({"api": api, "version": version, "data": data, "method": method})
        return {"ret": ["SUCCESS::调用成功"]}

    monkeypatch.setattr(client, "call_mtop", fake_call_mtop)

    client.search_judicial(zc_biz_types=["1", "7"])

    df_variables = json.loads(captured["data"]["dfVariables"])
    filters = json.loads(df_variables["context"]["_c_searchlistsf-items"])
    assert filters["zcBizTypes"] == ["1", "7"]
