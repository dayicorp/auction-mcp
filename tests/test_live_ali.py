"""阿里端集成测试 (真打 h5api.m.taobao.com).

默认跳过, 跑: pytest tests/test_live_ali.py --run-live

注: 拍卖数据实时变化, 数值断言用范围而非精确值.
"""
from __future__ import annotations

import pytest

import server  # MCP 工具函数直接调用 (绕过 stdio)

pytestmark = pytest.mark.live


# -------------------- 主用例: 区县级 (柯桥) --------------------

def test_keqiao_district_returns_scoped_results():
    """柯桥区主用例: dynamic learn 拿到 330621, 全部 items 命中.

    柯桥在 legacy 数据集叫"绍兴县"(名字对不上), 应触发 learn_district_code_from_city
    兜底, 学到 330621 并写缓存. 之前用户 query 时返 13 万条全国乱掺垃圾, 现在必须干净.
    """
    r = server.ali_search_judicial(province="浙江", city="绍兴市", district="柯桥区")

    assert "error" not in r, f"不应有 error, got: {r}"
    assert r.get("validated") is True, "守门应通过 (locationCode 应都在浙江)"
    assert r["matched_district_code"] == "330621", "应学到 Ali 真实柯桥码 330621"
    assert r["count"] > 0
    assert 50 < r["totalCount"] < 500, f"柯桥司法拍卖 ~200 量级, got {r['totalCount']}"
    # locationCode 是真值, 所有 item 必须全是 330621
    for it in r["items"]:
        assert str(it["locationCode"]) == "330621", \
            f"item 应全是柯桥, got locationCode={it['locationCode']}"
    # title 形态多样 (有公司股权/债权类不含地名), 仅要求多数含 "柯桥"
    title_hits = sum(1 for it in r["items"] if "柯桥" in (it["title"] or ""))
    assert title_hits >= len(r["items"]) // 2, \
        f"多数 item title 应含 '柯桥', got {title_hits}/{len(r['items'])}"


def test_shangyu_district_direct_legacy_hit():
    """上虞区: legacy 直接命中 (fuzzy '上虞市'→'上虞'), 不走 learn 兜底."""
    r = server.ali_search_judicial(province="浙江", city="绍兴市", district="上虞区")
    assert "error" not in r
    assert r.get("validated") is True
    assert r["matched_district_code"] == "330682"
    assert r["count"] > 0
    for it in r["items"]:
        assert str(it["locationCode"]) == "330682"


# -------------------- 守门 --------------------

def test_garbage_guard_catches_2020_district_code():
    """传 2020 版柯桥码 330603 (Ali 不认), 阿里返 13 万条全国乱掺数据.
    守门必须捕获 → 返 ali_returned_unscoped_results, 不返垃圾.
    """
    r = server.ali_search_judicial(location_codes=["330603"])

    assert r.get("error") == "ali_returned_unscoped_results"
    assert r["items"] == []
    diag = r["diagnostics"]
    assert diag["expected_prefix"] == "3306"
    # 乱掺样本应混入非浙江省的码
    sample = diag["sample_off_prefix_codes"]
    assert sample, "应给出乱掺样本"


# -------------------- 不回归: 城市/省/全国级 --------------------

def test_shaoxing_city_level_regression():
    """城市级一直可靠, 不应被 district 改造误伤."""
    r = server.ali_search_judicial(province="浙江", city="绍兴市")
    assert "error" not in r
    assert r.get("validated") is True
    assert r["count"] > 0
    assert r["totalCount"] > 500
    # 仅检查至少 80% 在浙江前缀下 (守门已做严格校验, 这里轻量再确认)
    in_zj = sum(1 for it in r["items"] if str(it["locationCode"]).startswith("33"))
    assert in_zj >= 8


def test_guangzhou_city_price_descending():
    """广州: totalCount > 1000, 价格降序."""
    r = server.ali_search_judicial(province="广东", city="广州市")
    assert "error" not in r
    assert r["totalCount"] > 1000
    prices = [it["currentPrice"] for it in r["items"] if it.get("currentPrice")]
    assert len(prices) >= 5
    assert all(prices[i] >= prices[i + 1] for i in range(len(prices) - 1)), \
        f"应价格降序, got {prices}"


def test_nationwide_skips_validation():
    """全国查询不带 location_codes → 跳过守门, 不应当成垃圾."""
    r = server.ali_search_judicial()
    assert "error" not in r
    assert r["count"] > 0
    assert r["totalCount"] > 10000  # 全国十几万


# -------------------- 兜底兜底: client-side title filter --------------------

def test_unified_search_returns_both_sources():
    """unified search_judicial 广州市级 → 两端都有数据, 价格降序合并, 单位归一."""
    r = server.search_judicial(province="广东", city="广州市", limit=15)
    assert "ali" in r["sources"] and "jd" in r["sources"], \
        f"应同时拿到两端, got sources={r['sources']}, errors={r.get('errors')}"
    assert r["count"] > 0
    # 每条 item 应有归一化字段
    for it in r["items"]:
        assert it["platform"] in ("ali", "jd")
        assert "id" in it
        assert "title" in it
        assert "price_yuan" in it
        assert "raw" in it
    # 价格降序
    prices = [it["price_yuan"] for it in r["items"] if it["price_yuan"] is not None]
    assert all(prices[i] >= prices[i + 1] for i in range(len(prices) - 1)), \
        f"应价格降序, got {prices[:5]}"
    # 两端都有贡献
    platforms = {it["platform"] for it in r["items"]}
    assert platforms == {"ali", "jd"}, f"top {len(r['items'])} 内应两端都出现, got {platforms}"


def test_unified_search_normalizes_price_unit():
    """阿里 currentPrice 单位是分, 京东是元; 归一化后 price_yuan 应统一为元 (float)."""
    r = server.search_judicial(province="广东", city="广州市", limit=20)
    for it in r["items"]:
        raw_cp = it["raw"].get("currentPrice")
        if raw_cp is None:
            continue
        if it["platform"] == "ali":
            assert abs(it["price_yuan"] - raw_cp / 100.0) < 0.01, \
                f"阿里应 ÷100 归一, raw={raw_cp}, got price_yuan={it['price_yuan']}"
        else:  # jd
            assert abs(it["price_yuan"] - float(raw_cp)) < 0.01, \
                f"京东原价就是元, got price_yuan={it['price_yuan']} raw={raw_cp}"


def test_unified_search_district_both_sources():
    """unified district 用例: 苏州吴江区 — 两端都应有结果."""
    r = server.search_judicial(province="江苏", city="苏州市", district="吴江区", limit=15)
    assert "ali" in r["sources"]
    assert "jd" in r["sources"]
    assert r["count"] > 0


def test_district_unknown_to_legacy_and_unlearnable_falls_back_to_title_filter():
    """边界: 一个 legacy 数据集没有、city pages 也没出现的虚构区县名 → 走客户端 title 过滤
    (返回空 items 但不报错)."""
    r = server.ali_search_judicial(
        province="浙江", city="绍兴市", district="不存在区xyz_test",
    )
    # 不报 error, 但 items 应为空 (没东西能命中 title)
    assert "error" not in r
    # 走 fallback 时附 _district_fallback 标记 (若走到 title filter 分支)
    # 注: learn 兜底先尝试,失败才到 title_filter; 该区名 learn 也学不到, 应到 title_filter.
    assert r.get("_district_fallback") == "title_filter" or r["count"] == 0


def test_ali_advanced_filters_accept_current_live_options():
    """实时读取筛选值并验证轮次/标签/资产类型可被真实 Ali 搜索接受.

    不硬编码 filter value，避免 Ali 调整选项后测试产生无意义漂移；每次从
    filtersf-nav 选择当前第一个非空值，再分别调用公开 Advanced 工具.
    """
    nav = server.ali_get_filter_options()
    assert "error" not in nav, f"filter nav 应成功, got: {nav}"

    dimensions = {d.get("varName"): d for d in nav.get("dimensions", [])}
    filters = {
        "circs": "circs",
        "tagIds": "tag_ids",
        "zcBizTypes": "zc_biz_types",
    }

    for dimension, argument in filters.items():
        options = [
            option
            for option in dimensions.get(dimension, {}).get("options", [])
            if option.get("value") not in (None, "")
        ]
        assert options, f"{dimension} 应至少有一个当前有效选项"

        selected = str(options[0]["value"])
        result = server.ali_search_judicial(**{argument: [selected]})

        assert "error" not in result, (
            f"{dimension}={selected} 应被真实 Ali API 接受, got: {result}"
        )
        assert result.get("validated") is True
        assert isinstance(result.get("items"), list)

    category_options = [
        option
        for option in dimensions.get("fcatV4Ids", {}).get("options", [])
        if option.get("name") and option.get("value") not in (None, "")
    ]
    assert category_options, "fcatV4Ids 应至少有一个当前有效分类"

    category_name = str(category_options[0]["name"])
    category_result = server.ali_search_judicial(fcat_v4_names=[category_name])
    assert "error" not in category_result, (
        f"分类中文名 {category_name!r} 应被实时解析并由 Ali API 接受, got: {category_result}"
    )
    assert category_result.get("validated") is True
    assert isinstance(category_result.get("items"), list)
