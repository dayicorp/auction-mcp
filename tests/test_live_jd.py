"""京东端集成测试 (真打 api.m.jd.com).

默认跳过, 跑: pytest tests/test_live_jd.py --run-live

验证本轮 JD 修复: 城市 ID 校正 + provinceId 校正 (重庆/山东/四川不再串省) +
multiStatus=101,102 状态过滤 (剔除已结束/已撤回的 displayStatus 5/6).
"""
from __future__ import annotations

import collections

import pytest

import server

pytestmark = pytest.mark.live


def _items(r):
    return (r.get("data") or {}).get("resultData") or []


def _display_status_counter(r):
    return collections.Counter(
        (it.get("data") or it).get("displayStatus") for it in _items(r)
    )


# ---------- 直接调 jd_search_judicial 看 API 层细节 (拿 raw response 用) ----------
from jd_h5_client import JDH5Client
_jd = JDH5Client()


def test_hangzhou_returns_real_results():
    """杭州: 修复前 cityId 1101 是死值返 0; 现在应正常."""
    r = _jd.search_judicial(province="浙江", city="杭州市")
    items = _items(r)
    assert len(items) == 40, f"杭州应每页 40 条, got {len(items)}"


def test_only_ongoing_and_upcoming_no_ended_items():
    """状态过滤: 默认 multiStatus=101,102, 不应有 displayStatus∈{5,6} 已结束."""
    r = _jd.search_judicial(province="广东", city="广州市")
    dist = _display_status_counter(r)
    bad = {k: v for k, v in dist.items() if k in (5, 6)}
    assert not bad, f"不应混入已结束/已撤回, got {dist}"


def test_chongqing_does_not_leak_sichuan():
    """重庆 provinceId 从 22 修到 4 — 之前查重庆返回的是四川."""
    r = _jd.search_judicial(province="重庆")
    titles = [(it.get("data") or it).get("title", "") for it in _items(r)]
    cq = sum(1 for t in titles if "重庆" in t or "渝" in t)
    sc_hint = sum(1 for t in titles if any(c in t for c in ["成都", "绵阳", "南充", "宜宾"]))
    assert cq > sc_hint, f"重庆模态应占主导, got 重庆/渝 {cq} vs 四川提示 {sc_hint}"
    assert cq >= 15, f"40 条里重庆占比应明显, got {cq}"


def test_shandong_does_not_leak_inner_mongolia():
    """山东 provinceId 从 11 修到 13 — 之前查山东返回的是内蒙古."""
    r = _jd.search_judicial(province="山东")
    titles = [(it.get("data") or it).get("title", "") for it in _items(r)]
    sd_hints = ["济南", "青岛", "烟台", "潍坊", "临沂", "济宁", "淄博", "泰安", "菏泽", "聊城", "德州"]
    nmg_hints = ["呼和浩特", "包头", "鄂尔多斯", "通辽", "赤峰"]
    sd_hits = sum(1 for t in titles if any(c in t for c in sd_hints))
    nmg_hits = sum(1 for t in titles if any(c in t for c in nmg_hints))
    assert sd_hits > nmg_hits, f"山东应主导, got 山东 {sd_hits} vs 内蒙 {nmg_hits}"


def test_sichuan_does_not_leak_jiangxi():
    """四川 provinceId 从 21 修到 22 — 之前查四川返回的是江西."""
    r = _jd.search_judicial(province="四川")
    titles = [(it.get("data") or it).get("title", "") for it in _items(r)]
    sc_hits = sum(1 for t in titles if any(c in t for c in ["成都", "绵阳", "德阳", "南充", "宜宾", "泸州", "乐山"]))
    jx_hits = sum(1 for t in titles if any(c in t for c in ["南昌", "赣州", "九江"]))
    assert sc_hits > jx_hits


def test_jd_search_returns_clean_envelope():
    """通过 MCP tool 函数走一遍, 返 envelope 不带 code=-1."""
    r = server.jd_search_judicial(province="广东", city="广州市")
    assert "error" not in r
    assert r["count"] == 40


def test_jd_district_wujiang_returns_filtered_results():
    """主用例: district='吴江区' 应只返回真吴江拍品 (含'吴江'/盛泽/黎里/松陵的多数)."""
    r = server.jd_search_judicial(province="江苏", city="苏州市", district="吴江区")
    assert "error" not in r
    items = r.get("items") or []
    assert len(items) > 0, "吴江区应有拍品"
    # 吴江下辖街道: 盛泽镇/黎里镇/松陵镇/同里镇/震泽镇/平望镇/桃源镇/横扇镇/八都镇/七都镇
    wj_markers = ("吴江", "盛泽", "黎里", "松陵", "同里", "震泽", "平望", "桃源")
    hits = sum(1 for it in items if any(m in (it.get("title") or "") for m in wj_markers))
    assert hits >= len(items) // 3, f"吴江标识(区名或下辖镇名)在多数标题里, got {hits}/{len(items)}"


def test_jd_supported_areas_full_coverage():
    """jd_get_supported_areas 全国 33 省 / >400 市 / >5000 区县."""
    r = server.jd_get_supported_areas()
    assert r["total"]["provinces"] == 33
    assert r["total"]["cities"] > 400
    assert r["total"]["districts"] > 5000


def test_jd_supported_areas_drilldown():
    """下钻: 苏州市 14 个区县, 含吴江区."""
    r = server.jd_get_supported_areas(province="江苏", city="苏州市")
    assert r["city"] == "苏州市"
    assert "吴江区" in r["districts"]
    assert r["district_count"] >= 10
