"""边界测试: 地区参数验证 — 结构非法 + 解析失败 + 合法路径 + Ali 兼容 (零网络).

覆盖 REQUIRED_CORRECTIONS:
  - 结构错误: city_requires_province / district_requires_city
  - 解析错误: region_resolution_failed + diagnostics
  - 未知 province/city/district 时 provider 调用次数为 0
  - 合法全国/省级/市级查询保持可用
  - Ali 动态学码 + title_filter 兜底兼容路径保留
"""
from __future__ import annotations

from typing import Any

import pytest

import server


# ============================================================ helpers

def _mock_providers(monkeypatch):
    """Mock 阿里和京东 HTTP 客户端的 search_judicial, 返回调用计数器.

    Mock 仅隔离网络 + 统计调用次数, 不 mock 解析/验证逻辑.
    """
    ali_calls = {"count": 0}
    jd_calls = {"count": 0}

    def fake_ali_search(*a, **kw):
        ali_calls["count"] += 1
        return {"ret": ["SUCCESS::调用成功"], "data": {"data": {"scenes": []}}}

    def fake_jd_search(*a, **kw):
        jd_calls["count"] += 1
        return {"code": 0, "data": {"resultData": []}}

    monkeypatch.setattr(server.ali, "search_judicial", fake_ali_search)
    monkeypatch.setattr(server.jd, "search_judicial", fake_jd_search)
    return ali_calls, jd_calls


def _make_ali_success_response(items_with_title: list[tuple[str, str]]) -> dict[str, Any]:
    """构造 ali.search_judicial SUCCESS 响应. items_with_title: [(locationCode, title)]."""
    content_list = [
        {"itemId": f"item_{i}",
         "extraMap": {"locationCode": lc, "title": title}}
        for i, (lc, title) in enumerate(items_with_title)
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


# ============================================================ 结构错误: city 无 province

class TestCityWithoutProvince:
    """city 有值但 province 为空 → city_requires_province."""

    def test_unified_city_no_province(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(city="广州市")
        assert r["error"] == "city_requires_province"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0

    def test_ali_city_no_province(self, monkeypatch):
        ali_c, _ = _mock_providers(monkeypatch)
        r = server.ali_search_judicial(city="广州市")
        assert r["error"] == "city_requires_province"
        assert ali_c["count"] == 0

    def test_jd_city_no_province(self, monkeypatch):
        _, jd_c = _mock_providers(monkeypatch)
        r = server.jd_search_judicial(city="广州市")
        assert r["error"] == "city_requires_province"
        assert jd_c["count"] == 0


# ============================================================ 结构错误: district 无 city

class TestDistrictWithoutCity:
    """district 有值但 city 为空 → district_requires_city."""

    def test_unified_district_no_city(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东", district="天河区")
        assert r["error"] == "district_requires_city"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0

    def test_ali_district_no_city(self, monkeypatch):
        ali_c, _ = _mock_providers(monkeypatch)
        r = server.ali_search_judicial(province="广东", district="天河区")
        assert r["error"] == "district_requires_city"
        assert ali_c["count"] == 0

    def test_jd_district_no_city(self, monkeypatch):
        _, jd_c = _mock_providers(monkeypatch)
        r = server.jd_search_judicial(province="广东", district="天河区")
        assert r["error"] == "district_requires_city"
        assert jd_c["count"] == 0


# ============================================================ 结构错误: city+district 无 province

class TestCityDistrictWithoutProvince:
    """city 和 district 有值但 province 为空 → city_requires_province."""

    def test_unified_city_district_no_province(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(city="广州市", district="天河区")
        assert r["error"] == "city_requires_province"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0


# ============================================================ 解析错误: 未知 province

class TestUnknownProvince:
    """未知 province → region_resolution_failed, provider 零调用."""

    def test_unified_unknown_province(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="火星省")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "province"
        assert r["diagnostics"]["province"] == "火星省"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0

    def test_jd_unknown_province(self, monkeypatch):
        _, jd_c = _mock_providers(monkeypatch)
        r = server.jd_search_judicial(province="火星省")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "province"
        assert jd_c["count"] == 0


# ============================================================ 解析错误: 已知 province + 未知 city

class TestUnknownCity:
    """已知 province + 未知 city → region_resolution_failed, provider 零调用."""

    def test_unified_unknown_city(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东", city="不存在市xyz")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert r["diagnostics"]["province"] == "广东"
        assert r["diagnostics"]["city"] == "不存在市xyz"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0

    def test_jd_unknown_city(self, monkeypatch):
        _, jd_c = _mock_providers(monkeypatch)
        r = server.jd_search_judicial(province="广东", city="不存在市xyz")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert jd_c["count"] == 0

    def test_unified_city_not_in_province(self, monkeypatch):
        """city 属于其他省 (杭州市不属于广东) → region_resolution_failed."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东", city="杭州市")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0


# ============================================================ 解析错误: JD 未知 district

class TestJdUnknownDistrict:
    """JD 未知 district → region_resolution_failed, provider 零调用."""

    def test_jd_unknown_district(self, monkeypatch):
        _, jd_c = _mock_providers(monkeypatch)
        r = server.jd_search_judicial(province="广东", city="广州市", district="火星区")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "district"
        assert r["diagnostics"]["district"] == "火星区"
        assert jd_c["count"] == 0

    def test_unified_unknown_district(self, monkeypatch):
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东", city="广州市", district="火星区")
        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "district"
        assert ali_c["count"] == 0
        assert jd_c["count"] == 0


# ============================================================ 合法路径

class TestValidPaths:
    """合法输入仍调用 Ali 和 JD, 不关闭任何 provider."""

    def test_national_query_valid(self, monkeypatch):
        """全国查询: 不传地区参数, 双端正常调用."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial()
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1

    def test_province_only_valid(self, monkeypatch):
        """合法 province-only."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东")
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1

    def test_province_city_valid(self, monkeypatch):
        """合法 province+city."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="浙江", city="杭州市")
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1

    def test_province_city_district_valid(self, monkeypatch):
        """合法 province+city+district."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="江苏", city="苏州市", district="吴江区")
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1

    def test_fuzzy_province_name(self, monkeypatch):
        """模糊匹配省名 '广东省' 也应通过."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东省")
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1

    def test_fuzzy_city_name(self, monkeypatch):
        """模糊匹配市名 '广州' (不带'市') 也应通过."""
        ali_c, jd_c = _mock_providers(monkeypatch)
        r = server.search_judicial(province="广东", city="广州")
        assert r.get("error") is None
        assert ali_c["count"] == 1
        assert jd_c["count"] == 1


# ============================================================ Ali 单源: 解析失败预检 + 动态学码 + title_filter

class TestAliResolutionPrecheck:
    """Ali 单源入口: 未知 province/city 在任何 provider 调用前返回 region_resolution_failed."""

    def test_ali_unknown_province_resolution_failed(self, monkeypatch):
        """未知 province → region_resolution_failed, search=0, learn=0."""
        search_calls = {"count": 0}
        learn_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return {"ret": ["SUCCESS::调用成功"], "data": {"data": {"scenes": []}}}

        def fake_learn(*a, **kw):
            learn_calls["count"] += 1
            return None

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)
        monkeypatch.setattr(server.ali, "learn_district_code_from_city", fake_learn)

        r = server.ali_search_judicial(province="火星省")

        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "province"
        assert r["diagnostics"]["province"] == "火星省"
        assert "city" in r["diagnostics"]
        assert "district" in r["diagnostics"]
        assert search_calls["count"] == 0
        assert learn_calls["count"] == 0

    def test_ali_unknown_city_resolution_failed(self, monkeypatch):
        """已知 province + 未知 city → region_resolution_failed, search=0, learn=0."""
        search_calls = {"count": 0}
        learn_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return {"ret": ["SUCCESS::调用成功"], "data": {"data": {"scenes": []}}}

        def fake_learn(*a, **kw):
            learn_calls["count"] += 1
            return None

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)
        monkeypatch.setattr(server.ali, "learn_district_code_from_city", fake_learn)

        r = server.ali_search_judicial(province="广东", city="不存在市")

        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert r["diagnostics"]["province"] == "广东"
        assert r["diagnostics"]["city"] == "不存在市"
        assert "district" in r["diagnostics"]
        assert search_calls["count"] == 0
        assert learn_calls["count"] == 0

    def test_ali_vintage_city_downgrade_rejected(self, monkeypatch):
        """现代/JD认识但Ali旧表降级到省级的城市，也必须零调用拒绝."""
        search_calls = {"count": 0}
        learn_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return {"ret": ["SUCCESS::调用成功"], "data": {"data": {"scenes": []}}}

        def fake_learn(*a, **kw):
            learn_calls["count"] += 1
            return None

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)
        monkeypatch.setattr(server.ali, "learn_district_code_from_city", fake_learn)

        # 现代表为 420600；Ali pre-2013 解析当前会降级为省码 420000。
        r = server.ali_search_judicial(province="湖北", city="襄阳市")

        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert search_calls["count"] == 0
        assert learn_calls["count"] == 0

    def test_unified_vintage_mismatch_keeps_jd_only(self, monkeypatch):
        """聚合入口不得让Ali扩大范围，但仍可返回JD这一可解析数据源."""
        ali_calls, jd_calls = _mock_providers(monkeypatch)

        r = server.search_judicial(province="湖北", city="襄阳市")

        assert ali_calls["count"] == 0
        assert jd_calls["count"] == 1
        assert r["errors"]["ali"]["error"] == "region_resolution_failed"
        assert r["errors"]["ali"]["diagnostics"]["resolution"] == "city"
        assert r["sources"] == ["jd"]

    def test_ali_direct_municipality_city_valid(self, monkeypatch):
        """直辖市省市同码是合法输入，守门应使用2位省级范围而非XX00."""
        search_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return _make_ali_success_response([
                ("110105", "北京市朝阳区某房产"),
                ("110108", "北京市海淀区某房产"),
            ])

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)

        r = server.ali_search_judicial(province="北京", city="北京市")

        assert r.get("error") is None
        assert r["validated"] is True
        assert search_calls["count"] == 1

    def test_ali_direct_municipality_lookalike_rejected(self, monkeypatch):
        """直辖市只接受精确别名；“北京区”不能因去后缀后同名而被放行."""
        search_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return _make_ali_success_response([("110105", "北京市朝阳区某房产")])

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)

        r = server.ali_search_judicial(province="北京", city="北京区")

        assert r["error"] == "region_resolution_failed"
        assert r["diagnostics"]["resolution"] == "city"
        assert search_calls["count"] == 0

    def test_ali_valid_province_only_still_works(self, monkeypatch):
        """合法 province-only 保持可用."""
        search_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return _make_ali_success_response([("440103", "广州某房产")])

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)

        r = server.ali_search_judicial(province="广东")
        assert r.get("error") is None or r.get("error") != "region_resolution_failed"
        assert search_calls["count"] == 1

    def test_ali_valid_province_city_still_works(self, monkeypatch):
        """合法 province+city 保持可用."""
        search_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return _make_ali_success_response([("440103", "广州某房产")])

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)

        r = server.ali_search_judicial(province="广东", city="广州市")
        assert r.get("error") is None or r.get("error") != "region_resolution_failed"
        assert search_calls["count"] == 1


class TestAliCompatibility:
    """Ali district 动态学码 + title_filter 兜底路径保留."""

    def test_ali_district_dynamic_learning_success(self, monkeypatch):
        """动态学码成功: legacy 未收录 → 确定进入 learn 路径, learn=1."""
        search_calls = {"count": 0}
        learn_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            return _make_ali_success_response([("330621", "柯桥区某房产")])

        def fake_learn(city_code, district_name, **kw):
            learn_calls["count"] += 1
            return "330621"  # 模拟学码成功

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)
        monkeypatch.setattr(server.ali, "learn_district_code_from_city", fake_learn)

        # 柯桥区在 GB2260_LEGACY 中名为绍兴县, resolve_area_ali 无法直接命中
        # → 确定进入 learn 路径
        r = server.ali_search_judicial(province="浙江", city="绍兴市", district="柯桥区")

        # 严格断言: 学码被调用恰好 1 次
        assert learn_calls["count"] == 1
        # 学码成功 → 不返回错误
        assert r.get("error") is None or r.get("error") != "district_code_unresolvable"
        assert r.get("matched_district_code") == "330621"

    def test_ali_district_title_filter_fallback(self, monkeypatch):
        """动态学码失败 → 城市级查询 + title_filter 兜底, 无条件证明兜底发生."""
        search_calls = {"count": 0}
        learn_calls = {"count": 0}

        def fake_search(**kw):
            search_calls["count"] += 1
            # 返回城市级结果, 部分 title 含 district 名
            return _make_ali_success_response([
                ("330602", "绍兴市柯桥区某房产"),
                ("330602", "绍兴市越城区某房产"),
                ("330602", "柯桥区另一标的"),
            ])

        def fake_learn(city_code, district_name, **kw):
            learn_calls["count"] += 1
            return None  # 学码失败

        monkeypatch.setattr(server.ali, "search_judicial", fake_search)
        monkeypatch.setattr(server.ali, "learn_district_code_from_city", fake_learn)

        r = server.ali_search_judicial(province="浙江", city="绍兴市", district="柯桥区")

        # 严格断言: 学码被调用 1 次
        assert learn_calls["count"] == 1
        # 严格断言: title_filter 兜底发生 (禁止条件式 if)
        assert r["_district_fallback"] == "title_filter"
        # ali.search_judicial 被调用 (城市级查询)
        assert search_calls["count"] == 1
        # title 过滤后只保留含 "柯桥" 的 items
        for item in r.get("items", []):
            assert "柯桥" in (item.get("title") or "")
