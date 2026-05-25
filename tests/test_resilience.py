"""容错单元测试 (零网络, monkeypatch httpx.Client.get/post).

覆盖两件常驻服务必备的 P0 自愈能力:
1. _m_h5_tk token 过期 → 自动清缓存重 bootstrap + 单次重试.
2. 阿里风控 punish HTML 页 → 不抛 JSONDecodeError, 返结构化 LOCAL_NON_JSON.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

import ali_h5_client
from ali_h5_client import AliH5Client


class _FakeResp:
    """最小化的 httpx.Response stub."""

    def __init__(self, payload: Any = None, status: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            # 模拟非 JSON 响应
            raise ValueError("not json")
        return self._payload


class _FakeCookieJar(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def delete(self, k):
        self.pop(k, None)


def _make_client_with_seed_token(monkeypatch):
    """返一个已 bootstrap 的 client (跳过真实 cookie 获取)."""
    c = AliH5Client()
    # mock 掉 httpx session 的 cookies + bootstrap 行为
    c.s.cookies = _FakeCookieJar({"_m_h5_tk": "seedtoken123_999"})
    c._tk_token = "seedtoken123"
    return c


def test_call_mtop_token_self_heal_on_sign_error(monkeypatch):
    """sign error / token expired → 清 token + 重 bootstrap + 单次重试 → 第二次成功."""
    c = _make_client_with_seed_token(monkeypatch)

    calls = {"do_call": 0, "bootstrap": 0}

    expired_resp = {"ret": ["FAIL_SYS_ILLEGAL_ACCESS::Sign Error!"]}
    ok_resp = {"ret": ["SUCCESS::调用成功"], "data": {"ok": True}}

    def fake_do_call(api, version, data_str, method):
        calls["do_call"] += 1
        return expired_resp if calls["do_call"] == 1 else ok_resp

    def fake_bootstrap():
        calls["bootstrap"] += 1
        c._tk_token = "newtoken456"

    monkeypatch.setattr(c, "_do_call", fake_do_call)
    monkeypatch.setattr(c, "_bootstrap_token", fake_bootstrap)

    out = c.call_mtop("mtop.fake.api", "1.0", {"q": 1}, method="GET")

    assert out["ret"] == ["SUCCESS::调用成功"]
    assert out.get("_token_refreshed") is True
    assert calls["do_call"] == 2, "应该恰好 retry 一次, 不死循环"
    # 1 次 (call_mtop 入口) + 1 次 (自愈) = 2 次
    assert calls["bootstrap"] == 2


def test_call_mtop_no_retry_on_business_failure(monkeypatch):
    """非 token 类业务失败 (e.g. FAIL_SYS_REQUEST_EXPIRED) 不触发自愈, 单次返回."""
    c = _make_client_with_seed_token(monkeypatch)
    calls = {"do_call": 0, "bootstrap": 0}

    def fake_do_call(api, version, data_str, method):
        calls["do_call"] += 1
        return {"ret": ["FAIL_BIZ_SOMETHING_ELSE::xxx"]}

    monkeypatch.setattr(c, "_do_call", fake_do_call)
    monkeypatch.setattr(c, "_bootstrap_token", lambda: calls.update(bootstrap=calls["bootstrap"] + 1))

    out = c.call_mtop("mtop.fake.api", "1.0", {})
    assert out["ret"][0].startswith("FAIL_BIZ_")
    assert "_token_refreshed" not in out
    assert calls["do_call"] == 1, "业务错误不应触发 retry"


def test_call_mtop_returns_local_non_json_on_html_punish(monkeypatch):
    """撞 baxia HTML / x5sec 跳转脚本 → 不抛, 返 LOCAL_NON_JSON 结构."""
    c = _make_client_with_seed_token(monkeypatch)

    html_punish = "<!DOCTYPE html><html><head><script>window.location='x5sec...'</script>"
    fake_resp = _FakeResp(payload=None, status=200, text=html_punish)

    def fake_post(*a, **kw):
        return fake_resp
    def fake_get(*a, **kw):
        return fake_resp

    monkeypatch.setattr(c.s, "post", fake_post)
    monkeypatch.setattr(c.s, "get", fake_get)
    monkeypatch.setattr(c, "_bootstrap_token", lambda: None)

    out = c.call_mtop("mtop.fake.api", "1.0", {}, method="POST")
    assert "LOCAL_NON_JSON" in out["ret"][0]
    assert out["_status"] == 200
    assert "x5sec" in out["_raw_preview"]
    # 不应抛 JSONDecodeError


def test_call_mtop_local_non_json_then_token_marker_does_not_loop(monkeypatch):
    """边界: HTML 响应不该被误判为 token 错误而走 retry 路径 (LOCAL_NON_JSON 不在 token marker 里)."""
    c = _make_client_with_seed_token(monkeypatch)
    calls = {"do_call": 0}

    def fake_do_call(*a, **kw):
        calls["do_call"] += 1
        return {"ret": ["LOCAL_NON_JSON::响应非 JSON, 多为风控页或网关异常"],
                "_status": 200, "_raw_preview": "<html>..."}

    monkeypatch.setattr(c, "_do_call", fake_do_call)
    monkeypatch.setattr(c, "_bootstrap_token", lambda: None)

    out = c.call_mtop("mtop.fake.api", "1.0", {})
    assert calls["do_call"] == 1, "LOCAL_NON_JSON 不应触发 token 自愈 retry"
    assert "_token_refreshed" not in out


def test_jd_client_non_json_response_returns_structured(monkeypatch):
    """JD 端撞 403/WAF 非 JSON → 不抛, 返 code=-1 + LOCAL_NON_JSON."""
    from jd_h5_client import JDH5Client

    c = JDH5Client()

    def fake_post(*a, **kw):
        return _FakeResp(payload=None, status=403, text="<html>403 Forbidden</html>")

    monkeypatch.setattr(c.s, "post", fake_post)

    out = c.search_judicial(province="广东", city="广州市")
    assert out["code"] == -1
    assert out["msg"] == "LOCAL_NON_JSON"
    assert out["_status"] == 403
    assert "403" in out["_raw_preview"]
