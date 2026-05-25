"""单元测试: 区域解析 (零网络).

核心断言: resolve_area_ali 对 pre-2013 vintage 返回阿里 server 实际接受的编码,
而 resolve_area (2020 版) 返回现代编码 — 两者在 2013 改名/改码的区县上不同.

JD 端: JD_AREAS 树由 getAreaInfoMap 一次性拉取(33省/455市/5344区县), 名字模糊匹配.
"""
from __future__ import annotations

from ali_h5_client import resolve_area, resolve_area_ali
from jd_h5_client import JDH5Client, JD_AREAS, _match_name


# -------------------- Ali vintage (pre-2013) --------------------

def test_resolve_ali_shaoxing_city_level():
    """城市级 4 位前缀新旧版一致."""
    assert resolve_area_ali("浙江", "绍兴市") == "330600"


def test_resolve_ali_shangyu_district_via_legacy_name():
    """上虞区: legacy 数据里叫"上虞市", fuzzy 匹配命中, 直出 330682."""
    assert resolve_area_ali("浙江", "绍兴市", "上虞区") == "330682"


def test_resolve_ali_zhuji_city_district():
    """诸暨市: 县级市, legacy=2020 同名同码."""
    assert resolve_area_ali("浙江", "绍兴市", "诸暨市") == "330681"


def test_resolve_ali_keqiao_NOT_in_legacy():
    """柯桥区在 legacy 数据集**不存在** (那时叫绍兴县, 330621).

    resolve_area_ali 找不到时返回城市级编码 (330600), 这是 server.py 据以
    fallback 到 learn_district_code_from_city 的信号.
    """
    code = resolve_area_ali("浙江", "绍兴市", "柯桥区")
    assert code == "330600", f"柯桥不该在 legacy 命中, 应回退到城市级, got {code}"


def test_resolve_ali_province_level():
    assert resolve_area_ali("广东") == "440000"
    assert resolve_area_ali("浙江") == "330000"


def test_resolve_ali_fuzzy_match_province():
    """模糊匹配: '广东' 等于 '广东省'."""
    assert resolve_area_ali("广东省") == resolve_area_ali("广东") == "440000"


def test_resolve_ali_fuzzy_match_city():
    """'广州' 等于 '广州市'."""
    assert resolve_area_ali("广东", "广州") == resolve_area_ali("广东", "广州市") == "440100"


def test_resolve_ali_unknown_province():
    assert resolve_area_ali("火星省") is None


# -------------------- 2020 版对照 --------------------

def test_resolve_2020_keqiao_returns_330603():
    """resolve_area (2020 版) 给柯桥区返回 330603 — 阿里不认这个码, 故仅用于人类展示."""
    assert resolve_area("浙江", "绍兴市", "柯桥区") == "330603"


def test_resolve_2020_vs_ali_diverge():
    """2013 改了码的区, 两个解析器给出不同结果, 这是设计."""
    ali_code = resolve_area_ali("浙江", "绍兴市", "柯桥区")
    new_code = resolve_area("浙江", "绍兴市", "柯桥区")
    # 柯桥不在 legacy → ali_code 是 330600 (城市级回退);
    # 在 2020 版有 → new_code 是 330603 (区级). 必然不同.
    assert ali_code != new_code


# -------------------- JD area tree (33 prov / 455 city / 5344 county) --------------------

def test_jd_areas_tree_loaded():
    """jd_areas.json 加载后省份数 33."""
    assert len(JD_AREAS) == 33
    # 各级数量级 sanity check
    cities = sum(len(p["cities"]) for p in JD_AREAS.values())
    counties = sum(len(c["counties"]) for p in JD_AREAS.values()
                                       for c in p["cities"].values())
    assert cities > 400
    assert counties > 5000


def test_jd_resolve_district_wujiang():
    """主用例: 吴江区 ID == 39628 (浏览器抓到的真值)."""
    c = JDH5Client()
    sp = c._resolve_area("江苏", "苏州市", "吴江区")
    assert sp["multiProvinceIds"] == 12
    assert sp["multiCityIds"] == 988
    assert sp["multiCountyIds"] == 39628
    assert sp["multiCountyNames"] == "吴江区"


def test_jd_resolve_known_city_ids():
    """已实测的城市 ID 不回归: 杭州 1213 / 深圳 1607 / 南京 904."""
    c = JDH5Client()
    assert c._resolve_area("浙江", "杭州市")["multiCityIds"] == 1213
    assert c._resolve_area("广东", "深圳市")["multiCityIds"] == 1607
    assert c._resolve_area("江苏", "南京市")["multiCityIds"] == 904


def test_jd_resolve_provinceid_corrections():
    """provinceId 校正不回归: 重庆=4 (非22), 山东=13 (非11), 四川=22 (非21)."""
    c = JDH5Client()
    assert c._resolve_area("重庆")["multiProvinceIds"] == 4
    assert c._resolve_area("山东")["multiProvinceIds"] == 13
    assert c._resolve_area("四川")["multiProvinceIds"] == 22


def test_jd_resolve_fuzzy_match():
    """模糊匹配: '广东省'='广东', '广州'='广州市'."""
    c = JDH5Client()
    assert c._resolve_area("广东省")["multiProvinceIds"] == 19
    assert c._resolve_area("广东", "广州")["multiCityIds"] == 1601


def test_jd_resolve_unknown_silent_skip():
    """未知层级 silent skip, 不报错也不乱传错码."""
    c = JDH5Client()
    sp = c._resolve_area("火星省")
    assert sp == {}, "未知 province 不应返回任何 id"
    sp = c._resolve_area("江苏", "不存在市xyz")
    assert "multiCityIds" not in sp, "未知 city 不传 cityId"
    assert sp["multiProvinceIds"] == 12  # 但 province 还在
    sp = c._resolve_area("江苏", "苏州市", "不存在区xyz")
    assert "multiCountyIds" not in sp, "未知 district 不传 countyId"
    assert sp["multiCityIds"] == 988
