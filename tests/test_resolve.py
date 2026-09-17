"""单元测试: 区域解析 (零网络).

核心断言: resolve_area_ali 对 pre-2013 vintage 返回阿里 server 实际接受的编码,
而 resolve_area (2020 版) 返回现代编码 — 两者在 2013 改名/改码的区县上不同.

JD 端: JD_AREAS 树由 getAreaInfoMap 一次性拉取(33省/455市/5344区县), 名字模糊匹配.
"""
from __future__ import annotations

import pytest

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


# -------------------- 回归: 同名邻区静默串区 (审计 P0-3 / P1-4) --------------------

def test_resolve_ali_jingzhou_is_not_jingmen():
    """'荆州市' 不得串到 '荆门市'.

    rstrip 吃的是字符集合, "荆州市" 会被连剥 市/州 只剩 "荆", 子串匹配先命中排在前面的
    荆门市(420800). 且守门的 expected_prefix 由同一个错码导出, 结构性发现不了 —
    用户会拿到一份看起来完全正常、实为隔壁城市的数据.
    """
    assert resolve_area_ali("湖北", "荆州市") == "421000"
    assert resolve_area_ali("湖北", "荆门市") == "420800"


@pytest.mark.parametrize("province,city,district,expected", [
    ("河北省", "石家庄市", "井陉县",   "130121"),   # 曾串到 井陉矿区 130107
    ("河南省", "鹤壁市",   "淇县",     "410622"),   # 曾串到 淇滨区   410611
    ("河南省", "新乡市",   "辉县市",   "410782"),   # 曾串到 卫辉市   410781
    ("湖南省", "岳阳市",   "岳阳县",   "430621"),   # 曾串到 岳阳楼区 430602
    ("广东省", "梅州市",   "梅县",     "441421"),   # 曾串到 梅江区   441402
    ("内蒙古自治区", "鄂尔多斯市", "鄂托克旗", "150624"),  # 曾串到 鄂托克前旗 150623
    ("山西省", "运城市",   "绛县",     "140826"),   # 曾串到 新绛县   140825
])
def test_resolve_ali_sibling_district_collisions(province, city, district, expected):
    """'X县' 不得被吃成 'X' 后命中同名的 'X区'/'X矿区'/'X前旗'."""
    assert resolve_area_ali(province, city, district) == expected


def test_resolve_ali_full_dataset_roundtrip_is_lossless():
    """全量往返: 数据集里每个市/区县用自己的名字查, 必须解析回自己的编码.

    这条能一次性锁住所有同名邻区串区 — 历史上 legacy 有 24 处 (荆州整城 10 个区县 +
    14 个同名邻区) 解析错误.
    """
    from ali_h5_client import GB2260_LEGACY, _pad_code
    bad = []
    for p in GB2260_LEGACY:
        for c in p.get("children", []):
            if resolve_area_ali(p["name"], c["name"]) != _pad_code(c["code"]):
                bad.append((p["name"], c["name"]))
            for d in c.get("children", []):
                if resolve_area_ali(p["name"], c["name"], d["name"]) != _pad_code(d["code"]):
                    bad.append((p["name"], c["name"], d["name"]))
    assert bad == [], f"{len(bad)} 处往返误解析, 前 10: {bad[:10]}"


# -------------------- 回归: 直辖市区县 --------------------

@pytest.mark.parametrize("province,city,district,expected", [
    ("上海", "上海市", "浦东新区", "310115"),
    ("北京", "北京市", "朝阳区",   "110105"),
    ("重庆", "重庆市", "渝北区",   "500112"),
    ("天津", "天津市", "和平区",   "120101"),
])
def test_resolve_ali_municipality_districts(province, city, district, expected):
    """直辖市的区县挂在 '市辖区'/'县' 中间节点下, 市名匹配不到它.

    不跨中间节点找的话, 4 个直辖市的**所有**区县级查询都会退化到省级码.
    """
    assert resolve_area_ali(province, city, district) == expected


# -------------------- 回归: 守门前缀粒度 (审计 P0-2) --------------------

@pytest.mark.parametrize("code,expected", [
    ("440000", "44"),      # 省级 → 2 位. 取 [:4] 得 '4400', 无真实区县码以此开头
    ("330000", "33"),
    ("440100", "4401"),    # 市级 → 4 位
    ("330600", "3306"),
    ("330621", "3306"),    # 区县级 → 退到市级粒度校验, 够用且不误杀
    ("310115", "3101"),    # 直辖市区县
])
def test_scope_prefix_granularity(code, expected):
    from ali_h5_client import scope_prefix
    assert scope_prefix(code) == expected


def test_scope_prefix_handles_empty():
    from ali_h5_client import scope_prefix
    assert scope_prefix(None) is None
    assert scope_prefix("") is None
