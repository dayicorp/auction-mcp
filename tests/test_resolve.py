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


# ==================== 回归: 第二轮审计 A — 京东端区县名静默串区 ====================
#
# 根因与第一轮 P0-3 (荆州→荆门) 完全相同, 但活在京东侧: str.rstrip 吃的是**字符集合**,
# 京东原本用一个大集合 "省市区县旗自治区盟自治州" 通吃三级, 把 "定州市" 剥成 "定";
# 再加上四个匹配条件挤在同一遍循环里 (含任意位置子串 `q in ks`), 迭代顺序就决定了结果。
# 修法: 三遍独立循环 (精确 → 去后缀后精确 → 前缀) + 后缀集合按层级拆分, 区县级不含 '州'。

@pytest.mark.parametrize("province,city,district,expected", [
    ("河北", "保定市",   "定州",   "定州市"),      # 曾串到 定兴县
    ("河北", "唐山市",   "滦州",   "滦州市"),      # 曾串到 滦南县
    ("山西", "长治市",   "潞州",   "潞州区"),      # 曾串到 潞城区
    ("河南", "新乡市",   "辉县",   "辉县市"),      # 曾串到 卫辉市
    ("江苏", "连云港市", "海州",   "海州区"),      # 曾串到 东海县
    ("山东", "烟台市",   "莱州",   "莱州市"),      # 曾串到 莱阳市
    ("安徽", "阜阳市",   "颍州",   "颍州区"),      # 曾串到 颍上县
    ("湖北", "襄阳市",   "襄州",   "襄州区"),      # 曾串到 襄城区
    ("湖北", "黄冈市",   "黄州",   "黄州区"),      # 曾串到 黄梅县
    ("江西", "吉安市",   "吉州",   "吉州区"),      # 曾串到 吉安县
    ("陕西", "渭南市",   "华州",   "华州区"),      # 曾串到 华阴市
    ("甘肃", "天水市",   "秦州",   "秦州区"),      # 曾串到 秦安县
    ("甘肃", "酒泉市",   "肃州",   "肃州区"),      # 曾串到 肃北蒙古族自治县
    ("黑龙江", "伊春市", "汤旺",   "汤旺县"),      # 曾串到 汤旺河区
    ("内蒙古", "通辽市", "科尔沁", "科尔沁区"),    # 曾串到 科尔沁左翼中旗
    ("四川", "成都市",   "高新",   "高新区"),      # 曾串到 高新西区
])
def test_jd_natural_short_district_name_not_confused_with_sibling(
        province, city, district, expected):
    """区县的**自然口语简称** (去掉末尾一个后缀字) 不得串到兄弟行政区.

    '辉县' / '黄州' / '海州' 都是这些地方的标准口语名 —— agent 从用户的
    '查一下辉县的法拍房' 里抽出来的就是 '辉县', 不是 '辉县市'。
    京东端没有任何守门 (validate_location_scoped 是阿里专用), 且这里发出的是一个
    **合法的** countyId, 守门就算搬过来也拦不住 —— 唯一的防线就是匹配本身要对。
    """
    params = JDH5Client()._resolve_area(province, city, district)
    assert params.get("multiCountyNames") == expected


def _natural_short(name: str, sfx: str = "省市区县旗盟") -> str:
    """人类会打的简称: 只去掉末尾一个后缀字 (定州市→定州, 吴江区→吴江)."""
    return name[:-1] if len(name) > 2 and name[-1] in sfx else name


# 短名与兄弟行政区**真正同名**的, 无解, 不算缺陷 (如 临夏市/临夏县 简称都是 '临夏').
_JD_INHERENTLY_AMBIGUOUS = {
    ("甘肃", "临夏州",    "临夏市"),
    ("新疆", "和田地区",  "和田县"),
    ("新疆", "伊犁州",    "伊宁县"),
    ("台湾", "中国台湾",  "新竹市"),
    ("台湾", "中国台湾",  "嘉义市"),
}


def test_jd_full_dataset_natural_short_name_roundtrip():
    """全量往返: 5344 个区县逐个用自然简称查, 必须解析回自己.

    阿里侧早有 test_resolve_ali_full_dataset_roundtrip_is_lossless, 京东侧一直没有 ——
    原有的 4 个手写用例全传**全名**, 走的是 `query in candidates` 那条精确分支,
    永远碰不到下面的模糊循环, 于是 24 处串区一直没被发现。这条补上那个覆盖盲区。
    """
    bad = []
    for pname, p in JD_AREAS.items():
        for cname, c in p["cities"].items():
            for dname in c["counties"]:
                q = _natural_short(dname)
                if q == dname:
                    continue
                if (pname, cname, dname) in _JD_INHERENTLY_AMBIGUOUS:
                    continue
                m = _match_name(q, c["counties"], "区县市旗")
                got = m[0] if m else None
                if got != dname:
                    bad.append((pname, cname, dname, q, got))
    assert bad == [], f"{len(bad)} 处简称串区, 前 10: {bad[:10]}"


def test_jd_full_name_roundtrip_still_lossless():
    """全名查询仍必须逐级解析回自己 (防三级匹配改动把精确路径改坏)."""
    bad = []
    for pname, p in JD_AREAS.items():
        for cname, c in p["cities"].items():
            if (_match_name(cname, p["cities"], "市地区州盟自治州") or (None,))[0] != cname:
                bad.append((pname, cname))
            for dname in c["counties"]:
                if (_match_name(dname, c["counties"], "区县市旗") or (None,))[0] != dname:
                    bad.append((pname, cname, dname))
    assert bad == [], f"{len(bad)} 处全名往返误解析, 前 10: {bad[:10]}"


# ==================== 回归: 第二轮审计 B — ali_get_supported_areas 重复匹配 ====================

def test_ali_supported_areas_jingzhou_is_not_jingmen():
    """第一轮 P0-3 只修了 ali_h5_client._pick, server 这个展示工具自己另写了一份
    `cn in x["name"]` 的**任意位置子串**匹配, 荆州→荆门原样复现过。

    这是给 agent 查'有哪些区县可选'的发现型工具 —— 查荆州拿到荆门的区县名单,
    后面整条查询链路再干净也已经错了, 且错在一个不会报错的地方。
    """
    import server
    out = server.ali_get_supported_areas("湖北", "荆州市")
    assert out.get("city") == "荆州市", out
    assert out.get("code") == "421000", out
    names = [d["name"] for d in out.get("districts", [])]
    assert "沙市区" in names and "荆州区" in names
    assert "东宝区" not in names and "掇刀区" not in names   # 荆门的区


def test_ali_supported_areas_full_city_roundtrip():
    """全量往返: 342 个地级市逐个用自己的名字查, 必须返回自己."""
    import server
    from ali_h5_client import GB2260
    bad = []
    for p in GB2260:
        if server.ali_get_supported_areas(p["name"]).get("province") != p["name"]:
            bad.append((p["name"],))
        for c in p.get("children", []):
            if server.ali_get_supported_areas(p["name"], c["name"]).get("city") != c["name"]:
                bad.append((p["name"], c["name"]))
    assert bad == [], f"{len(bad)} 处误解析: {bad[:10]}"


# ==================== 回归: 京东直辖市是 省→区 两层 ====================

@pytest.mark.parametrize("province,city,district,expected", [
    ("上海", "上海市", "浦东新区", "浦东新区"),
    ("北京", "北京市", "朝阳区",   "朝阳区"),
    ("重庆", "重庆市", "渝北区",   "渝北区"),
    ("天津", "天津市", "和平区",   "和平区"),
])
def test_jd_municipality_district_is_resolved(province, city, district, expected):
    """京东的直辖市树是 省 → **区**, 没有"市"这一级.

    不特殊处理的话 "上海市" 匹配不到任何候选, district 连试都不会试一次 ——
    查"上海市浦东新区"会静默返回**全上海** (实测混进黄浦区的标的)。
    这是阿里侧 _municipality_district (第一轮 P0-12) 在京东侧的同类缺口。
    """
    params = JDH5Client()._resolve_area(province, city, district)
    # 直辖市下, 京东的 cities 那一层承载的就是区县
    assert params.get("multiCityNames") == expected, params


def test_jd_municipality_without_district_still_province_only():
    """只给直辖市省名时不该凭空收窄到某个区."""
    params = JDH5Client()._resolve_area("上海")
    assert params.get("multiProvinceNames") == "上海"
    assert "multiCityIds" not in params
