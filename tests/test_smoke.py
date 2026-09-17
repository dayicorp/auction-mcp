"""冒烟测试 (零网络): MCP 协议层 + 全链路管线.

现有 47 项测试覆盖的是纯函数 (解析/守门/容错), **没有一项跑通过完整管线**,
也没有一项碰过 MCP 协议层。本文件补这两块:

L1  以真实 MCP client 起 server.py 子进程, 走 initialize / tools/list / tools/call
L2  httpx.MockTransport 扮演"完全配合"的上游, 跑 解析→组包→解析响应→归一→合并→信封

L2 的上游**如实按请求的编码返回该地区的正确数据**, 因此任何错误结果都只可能是客户端自身缺陷。
「审计缺陷的回归测试」一节锁的是已修复的 P0-2 / P0-3 / P1-5, 防止回潮。
"""
from __future__ import annotations

import json
import sys
import urllib.parse

import httpx
import pytest

# ============================================================ L2 fixtures

GD_PROVINCE = ["440304", "440305", "440106", "440113", "441302", "440703"]  # 广东各市
GZ_CITY     = ["440106", "440113", "440111", "440115"]                      # 广州各区
JINGMEN     = ["420802", "420804", "420821"]                                # 荆门 4208
JINGZHOU    = ["421002", "421003", "421022", "421024"]                      # 荆州 4210

ALI_PRICE_FEN = 125_000_000_000        # 12.5 亿元, 阿里以**分**计


def _ali_payload(codes: list[str]) -> dict:
    items = [{"itemId": 1049000000000 + i,
              "currentPrice": ALI_PRICE_FEN - i * 100_000_000,
              "extraMap": {"title": f"标的{i}", "locationCode": c,
                           "shopName": f"{c}法院", "status": "进行中"}}
             for i, c in enumerate(codes)]
    return {"ret": ["SUCCESS::调用成功"],
            "data": {"data": {"scenes": [{"schemeList": [
                {"totalCount": len(codes) * 100, "page": 1, "pageSize": 10,
                 "contentList": items}]}]}}}


def _jd_payload(n: int = 3) -> dict:
    return {"code": 0, "data": {"resultData": [
        {"data": {"paimaiId": 900 + i, "title": f"京东标的{i}",
                  "currentPrice": 50_000_000.0 - i * 1_000_000,
                  "province": "广东", "city": "广州市", "displayStatus": 101}}
        for i in range(n)]}}


def _sent_location_codes(request: httpx.Request) -> list[str]:
    """从阿里出站请求里还原实际发出的 locationCodes."""
    qs = urllib.parse.parse_qs(request.content.decode() or "")
    if not qs.get("data"):
        return []
    data = json.loads(qs["data"][0])
    dfv = json.loads(data.get("dfVariables", "{}"))
    filters = json.loads(dfv.get("context", {}).get("_c_searchlistsf-items", "{}"))
    return filters.get("locationCodes") or []


@pytest.fixture
def smoke_server(monkeypatch):
    """server 模块 + 配合型 mock 上游. 返回 (server, sent) — sent 记录所有出站请求."""
    import server

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "taobao" in request.url.host:
            codes = _sent_location_codes(request)
            code = (codes[0] if codes else "") or ""
            if code.startswith("4401"):
                body = GZ_CITY
            elif code.startswith("4208"):
                body = JINGMEN
            elif code.startswith("4210"):
                body = JINGZHOU
            else:
                body = GD_PROVINCE
            resp = httpx.Response(200, json=_ali_payload(body))
            resp.headers["set-cookie"] = "_m_h5_tk=tok0123456789abcdef0123456789ab_1"
            return resp
        return httpx.Response(200, json=_jd_payload())

    transport = httpx.MockTransport(handler)
    for client in (server.ali.s, server.jd.s):
        monkeypatch.setattr(client, "_transport", transport)
        for key in list(client._mounts):
            client._mounts[key] = transport
    monkeypatch.setattr(server.ali, "_tk_token", "tok0123456789abcdef0123456789ab")
    return server, sent


# ============================================================ L2: 管线

def test_pipeline_city_level_returns_scoped_items(smoke_server):
    """基线: 市级查询跑通全链路, 守门放行, 条目落在该市前缀下."""
    server, _ = smoke_server
    r = server.ali_search_judicial(province="广东", city="广州市")
    assert "error" not in r, r
    assert r["count"] == len(GZ_CITY)
    assert all(str(it["locationCode"]).startswith("4401") for it in r["items"])


def test_pipeline_outgoing_ali_request_is_wellformed(smoke_server):
    """出站组包: sign 为 32 位 MD5, appKey 正确, 固定价格降序 + 仅进行中/即将开始."""
    server, sent = smoke_server
    server.ali_search_judicial(province="广东", city="广州市")
    req = next(r for r in sent if "taobao" in r.url.host)
    params = dict(req.url.params)
    assert len(params["sign"]) == 32 and int(params["sign"], 16) >= 0
    assert params["appKey"] == "12574478"
    data = json.loads(urllib.parse.parse_qs(req.content.decode())["data"][0])
    filters = json.loads(json.loads(data["dfVariables"])["context"]["_c_searchlistsf-items"])
    assert filters["sort"] == "501"
    assert filters["statusOrders"] == ["0", "1"]


def test_pipeline_outgoing_jd_request_carries_resolved_ids(smoke_server):
    """京东出站: 中文名已解析成真实 id (广东=19, 广州=1601), 固定排序/状态."""
    server, sent = smoke_server
    server.jd_search_judicial(province="广东", city="广州市")
    req = next(r for r in sent if "jd.com" in r.url.host)
    body = json.loads(urllib.parse.parse_qs(req.content.decode())["body"][0])
    sp = body["searchParamsObj"]
    assert sp["multiProvinceIds"] == 19 and sp["multiCityIds"] == 1601
    assert sp["sortField"] == "7" and sp["multiStatus"] == "101,102"


def test_pipeline_unified_merges_both_sources_and_normalizes_unit(smoke_server):
    """统一工具: 双端都进结果, 阿里分→元 (÷100), 价格严格降序."""
    server, _ = smoke_server
    r = server.search_judicial(province="广东", city="广州市", limit=10)
    assert set(r["sources"]) == {"ali", "jd"}
    assert "errors" not in r
    platforms = {it["platform"] for it in r["items"]}
    assert platforms == {"ali", "jd"}
    top_ali = next(it for it in r["items"] if it["platform"] == "ali")
    assert top_ali["price_yuan"] == pytest.approx(ALI_PRICE_FEN / 100.0)
    prices = [it["price_yuan"] for it in r["items"]]
    assert prices == sorted(prices, reverse=True)


def test_pipeline_unified_degrades_when_one_source_fails(smoke_server, monkeypatch):
    """单端异常时统一工具照常返回另一端 + 结构化 errors (已有行为, 锁住不回归)."""
    server, _ = smoke_server

    def boom(*a, **k):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr(server.ali, "search_judicial", boom)
    r = server.search_judicial(province="广东", city="广州市", limit=5)
    assert r["sources"] == ["jd"]
    assert "ali" in r["errors"]
    assert r["count"] > 0


# ============================================================ L2: 审计缺陷的回归测试

def test_province_level_query_not_falsely_rejected(smoke_server):
    """上游返回的全是合法广东标的, 省级查询不该被自己的守门判成 unscoped."""
    server, _ = smoke_server
    r = server.ali_search_judicial(province="广东")
    assert r.get("error") != "ali_returned_unscoped_results", r.get("diagnostics")
    assert r["count"] == len(GD_PROVINCE)


def test_jingzhou_does_not_silently_resolve_to_jingmen(smoke_server):
    """查荆州必须发荆州的码 4210xx, 且拿回荆州的标的 — 不能静默串到荆门."""
    server, sent = smoke_server
    r = server.ali_search_judicial(province="湖北", city="荆州市")
    codes = _sent_location_codes(next(x for x in sent if "taobao" in x.url.host))
    assert codes == ["421000"], f"实际发出 {codes} (420800 = 荆门市)"
    assert all(str(it["locationCode"]).startswith("4210") for it in r["items"])


def test_single_source_tools_return_structured_error_on_network_failure(smoke_server, monkeypatch):
    """单源工具遇网络故障应像统一工具那样返回结构化错误, 而非抛异常."""
    server, _ = smoke_server

    def boom(*a, **k):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr(server.ali, "search_judicial", boom)
    r = server.ali_search_judicial(province="广东", city="广州市")
    assert isinstance(r, dict) and r.get("error")


# ============================================================ L1: MCP 协议层

@pytest.mark.anyio
async def test_mcp_protocol_handshake_and_zero_network_tools():
    """真起 server.py 子进程, 走完整 stdio 协议: 6 个工具注册 + 零网络工具端到端可调."""
    mcp_client = pytest.importorskip("mcp.client.stdio")
    from mcp import ClientSession, StdioServerParameters

    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(root, "server.py")])
    async with mcp_client.stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            assert set(names) == {
                "search_judicial", "ali_search_judicial", "ali_get_filter_options",
                "ali_get_supported_areas", "jd_search_judicial", "jd_get_supported_areas",
            }
            res = await session.call_tool("jd_get_supported_areas",
                                          {"province": "江苏", "city": "苏州市"})
            assert not res.isError
            payload = json.loads(res.content[0].text)
            assert payload["city"] == "苏州市"
            assert "吴江区" in payload["districts"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ============================================================ L2: 参数与语义回归

@pytest.mark.parametrize("limit,expected", [
    (0, 1),          # 下界
    (-1, 1),         # 曾让 items[:-1] 静默丢掉最后一条
    (10 ** 9, 50),   # 上界 MAX_LIMIT
    ("abc", 20),     # 非数字 → 默认值
])
def test_limit_is_clamped(smoke_server, limit, expected):
    """limit 归一到 [1, 50]. 负数尤其危险: 不报错, 只是悄悄少一条."""
    server, _ = smoke_server
    r = server.search_judicial(province="广东", city="广州市", limit=limit)
    assert r["count"] == min(expected, len(GZ_CITY) + 3)


def test_page_is_clamped(smoke_server):
    """页码归一到 ≥1, 不把 0/负数透传给上游."""
    server, _ = smoke_server
    assert server.search_judicial(province="广东", city="广州市", page=-5)["page"] == 1
    assert server.search_judicial(province="广东", city="广州市", page=0)["page"] == 1


def test_unresolvable_area_returns_structured_error(smoke_server):
    """省份解析不出来时直接报错, 而不是发出 ['0000'] / [''] 这类无意义编码去打上游.

    历史行为: 未知省份 + district 会先用 '0000' 翻 5 页学码, 再用 '' 查一次,
    且因 expected_prefix 为 None 而**绕过守门**.
    """
    server, sent = smoke_server
    r = server.ali_search_judicial(province="火星省", city="火星市", district="火星区")
    assert r["error"] == "area_not_resolved"
    assert sent == [], "解析失败不应产生任何上游请求"


def test_ali_single_source_exposes_price_yuan(smoke_server):
    """单源阿里结果也带 price_yuan (元).

    阿里 currentPrice 是**分**, 京东同名字段是**元**, 相差 100 倍. 单源只给 currentPrice
    的话, LLM 直接汇报会把 1.25 亿读成 1250 亿.
    """
    server, _ = smoke_server
    r = server.ali_search_judicial(province="广东", city="广州市")
    top = r["items"][0]
    assert top["currentPrice"] == ALI_PRICE_FEN
    assert top["price_yuan"] == pytest.approx(ALI_PRICE_FEN / 100.0)


def test_learn_district_code_negative_result_is_cached(smoke_server, monkeypatch):
    """学不到的区县要负缓存: 第二次查同一个区县不得再打上游.

    历史行为: 失败结果不入缓存, 每次重查都重新翻 max_pages 页 (实测 6 次上游 / 728ms).
    """
    import ali_h5_client
    monkeypatch.setattr(ali_h5_client, "_DISTRICT_CODE_CACHE", {})
    server, sent = smoke_server

    # 上游返回的标的标题里没有该区县名 → 必然学不到
    first = server.ali.learn_district_code_from_city("330600", "不存在区")
    n_first = len(sent)
    assert first is None
    assert n_first > 0, "首次应真的去翻页"

    sent.clear()
    second = server.ali.learn_district_code_from_city("330600", "不存在区")
    assert second is None
    assert sent == [], f"第二次应命中负缓存, 实际又打了 {len(sent)} 次上游"
