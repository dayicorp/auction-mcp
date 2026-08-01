"""阿里司法拍卖 PC 浏览器适配器.

该链路用于 H5 mtop 不支持、但 PC 页面已验证可用的关键词、价格与
开始时间筛选。浏览器上下文是非持久化的；本模块不读取 Cookie、不导出
storage_state，也不指定用户数据目录。登录与验证码始终交给用户手动完成。
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


PC_HOME_URL = "https://sf.taobao.com/"
PC_KEYWORD_URL = "https://sf.taobao.com/item_list.htm"
_PC_ALLOWED_HOST = "sf.taobao.com"
_PUNISH_MARKERS = ("/_____tmd_____/punish", "x5secdata=")
_VERIFICATION_MARKERS = ("安全验证", "滑动验证", "请完成验证", "请拖动滑块")


class AliPCBrowserError(RuntimeError):
    """带稳定错误码的 PC 浏览器适配器错误."""

    def __init__(self, code: str, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}

    def as_dict(self) -> dict:
        return {
            "error": self.code,
            "message": self.message,
            "diagnostics": self.diagnostics,
        }


def _decimal_yuan(value: int | float | str | Decimal | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AliPCBrowserError("pc_filter_validation_failed", f"{field} 必须是有效金额") from exc
    if not amount.is_finite() or amount < 0:
        raise AliPCBrowserError("pc_filter_validation_failed", f"{field} 必须是非负金额")
    if amount != amount.to_integral_value():
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            f"{field} 单位为元，当前仅接受整数金额",
        )
    return str(int(amount))


def _iso_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            f"{field} 必须使用 YYYY-MM-DD 格式",
        ) from exc
    return parsed.isoformat()


def build_pc_search_url(
    base_url: str = PC_HOME_URL,
    *,
    keyword: str | None = None,
    min_price_yuan: int | float | str | Decimal | None = None,
    max_price_yuan: int | float | str | Decimal | None = None,
    auction_start_from: str | None = None,
    auction_start_to: str | None = None,
) -> str:
    """把已由真实 PC 页面验证的筛选参数合并到 URL.

    关键词搜索会清空页面已有分类与地区筛选，因此与价格/日期或非首页
    base_url 组合时 fail-closed，防止声称不存在的组合能力。
    """
    parts = urlsplit(base_url)
    if parts.scheme != "https" or parts.hostname != _PC_ALLOWED_HOST:
        raise AliPCBrowserError(
            "pc_navigation_blocked",
            "PC 适配器只允许构造 https://sf.taobao.com 页面 URL",
            {"url": base_url},
        )

    keyword = (keyword or "").strip() or None
    minimum = _decimal_yuan(min_price_yuan, "min_price_yuan")
    maximum = _decimal_yuan(max_price_yuan, "max_price_yuan")
    start = _iso_date(auction_start_from, "auction_start_from")
    end = _iso_date(auction_start_to, "auction_start_to")

    if minimum is not None and maximum is not None and int(minimum) > int(maximum):
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "min_price_yuan 不得大于 max_price_yuan",
        )
    if bool(start) != bool(end):
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "auction_start_from 与 auction_start_to 必须同时提供",
        )
    if start and end and start > end:
        raise AliPCBrowserError(
            "pc_filter_validation_failed",
            "auction_start_from 不得晚于 auction_start_to",
        )

    normalized_base = base_url.rstrip("/")
    has_existing_scope = normalized_base not in {PC_HOME_URL.rstrip("/"), PC_KEYWORD_URL.rstrip("/")}
    if keyword and (minimum or maximum or start or end or has_existing_scope):
        raise AliPCBrowserError(
            "pc_keyword_filter_conflict",
            "真实 PC 页面会在关键词搜索时清空分类、地区、价格和时间筛选，不能组合查询",
        )
    if keyword:
        return f"{PC_KEYWORD_URL}?{urlencode({'_input_charset': 'utf-8', 'q': keyword, 'keywordSource': '4'})}"

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # PC 页面入口同时接受 camelCase；规范化为筛选后地址实际使用的 snake_case。
    query.pop("startPrice", None)
    query.pop("endPrice", None)
    if minimum is None:
        query.pop("start_price", None)
    else:
        query["start_price"] = minimum
    if maximum is None:
        query.pop("end_price", None)
    else:
        query["end_price"] = maximum
    if start is None:
        query.pop("auctionStartFrom", None)
        query.pop("auctionStartTo", None)
    else:
        query["auctionStartFrom"] = start
        query["auctionStartTo"] = end or ""
    query.setdefault("auction_source", "0")
    query.setdefault("st_param", "-1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _price_yuan_from_text(text: str) -> float | None:
    match = re.search(
        r"(?:当前价|变卖价|起拍价|开拍价)\s*[¥￥]?\s*([0-9][0-9,.]*)\s*(万|亿)?",
        text,
    )
    if not match:
        return None
    value = Decimal(match.group(1).replace(",", ""))
    if match.group(2) == "万":
        value *= 10_000
    elif match.group(2) == "亿":
        value *= 100_000_000
    return float(value)


def parse_pc_item_records(records: list[dict[str, Any]], limit: int = 20) -> list[dict]:
    """把页面端抽取的卡片记录规范化，按 itemId 去重."""
    items: list[dict] = []
    seen: set[str] = set()
    for record in records:
        href = str(record.get("href") or "")
        id_match = re.search(r"(?:sf_item/|[?&](?:id|itemId)=)(\d{8,})", href)
        if not id_match:
            id_match = re.search(r"(\d{10,})", href)
        if not id_match or id_match.group(1) in seen:
            continue
        text = str(record.get("text") or "").strip()
        if not re.search(r"当前价|变卖价|起拍价|开拍价|评估价|开始时间", text):
            continue
        item_id = id_match.group(1)
        seen.add(item_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = str(record.get("title") or "").strip()
        if not title:
            title = next(
                (line for line in lines if not re.search(r"[¥￥]|当前价|变卖价|起拍价|开拍价|评估价|开始时间", line)),
                "",
            )
        items.append({
            "itemId": item_id,
            "title": title,
            "currentPriceYuan": _price_yuan_from_text(text),
            "url": href,
            "image": record.get("image"),
            "rawText": text,
        })
        if len(items) >= limit:
            break
    return items


def evaluate_pc_live_acceptance(
    result: dict,
    *,
    max_price_yuan: int,
    auction_start_from: str,
    auction_start_to: str,
) -> dict:
    """对一次真实 PC 查询做不含 Cookie/原始页面内容的结构化验收."""
    failures: list[str] = []
    if result.get("error"):
        failures.append(f"query_error:{result['error']}")
    if result.get("state") in {"login_required", "action_required", "stopped"}:
        failures.append(f"session_state:{result['state']}")
    if result.get("source") != "ali_pc_browser":
        failures.append("unexpected_source")
    if result.get("authenticated_session") is not True:
        failures.append("authenticated_session_not_confirmed")

    items = result.get("items") if isinstance(result.get("items"), list) else []
    if not items:
        failures.append("no_items")

    missing_price_ids: list[str] = []
    over_limit_ids: list[str] = []
    prices: list[float] = []
    for item in items:
        item_id = str(item.get("itemId") or "unknown")
        price = item.get("currentPriceYuan")
        if not isinstance(price, (int, float)):
            missing_price_ids.append(item_id)
            continue
        numeric_price = float(price)
        prices.append(numeric_price)
        if numeric_price > max_price_yuan:
            over_limit_ids.append(item_id)
    if missing_price_ids:
        failures.append("items_missing_current_price")
    if over_limit_ids:
        failures.append("items_over_max_price")

    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    result_url = str(result.get("url") or diagnostics.get("url") or "")
    query = dict(parse_qsl(urlsplit(result_url).query, keep_blank_values=True))
    expected_query = {
        "end_price": str(max_price_yuan),
        "auctionStartFrom": auction_start_from,
        "auctionStartTo": auction_start_to,
    }
    mismatched_query = {
        key: {"expected": value, "actual": query.get(key)}
        for key, value in expected_query.items()
        if query.get(key) != value
    }
    if mismatched_query:
        failures.append("verified_query_params_missing")

    samples = [
        {
            "itemId": item.get("itemId"),
            "title": item.get("title"),
            "currentPriceYuan": item.get("currentPriceYuan"),
            "url": item.get("url"),
        }
        for item in items[:3]
    ]
    return {
        "accepted": not failures,
        "failures": failures,
        "evidence": {
            "result_count": len(items),
            "reported_count": result.get("count"),
            "minimum_price_yuan": min(prices) if prices else None,
            "maximum_price_yuan": max(prices) if prices else None,
            "max_price_contract_yuan": max_price_yuan,
            "missing_price_item_ids": missing_price_ids,
            "over_limit_item_ids": over_limit_ids,
            "query_params": {key: query.get(key) for key in expected_query},
            "query_param_mismatches": mismatched_query,
            "query_url": result_url,
            "query_diagnostics": diagnostics,
            "samples": samples,
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        },
    }


class AliPCBrowserClient:
    """基于非持久化 Playwright context 的登录态 PC 查询客户端."""

    def __init__(self, *, headless: bool = False, timeout_ms: int = 30_000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._lock = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def _locked(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self) -> dict:
        lock = await self._locked()
        async with lock:
            if self._page and not self._page.is_closed():
                return await self._status_unlocked()
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return {
                    "error": "pc_browser_dependency_missing",
                    "message": "缺少 playwright；请先安装 requirements.txt",
                }
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    channel="chrome",
                    headless=self.headless,
                )
                # 非持久化 context：不指定 user_data_dir，不导出 storage_state。
                self._context = await self._browser.new_context(locale="zh-CN")
                self._page = await self._context.new_page()
                self._page.set_default_timeout(self.timeout_ms)
                await self._page.goto(PC_HOME_URL, wait_until="domcontentloaded")
                return await self._status_unlocked()
            except Exception as exc:
                await self._close_unlocked()
                return {
                    "error": "pc_browser_start_failed",
                    "message": str(exc),
                }

    async def status(self) -> dict:
        lock = await self._locked()
        async with lock:
            return await self._status_unlocked()

    async def _status_unlocked(self) -> dict:
        if not self._page or self._page.is_closed():
            return {"state": "stopped", "cookie_policy": "browser_memory_only"}
        url = self._page.url
        title = await self._page.title()
        body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
        action = self._action_required(url, title, body)
        if action:
            return action
        login_text = ""
        login_node = self._page.locator("#J_SiteNavLogin")
        if await login_node.count():
            login_text = (await login_node.first.inner_text()).strip()
        authenticated = bool(login_text and "请登录" not in login_text)
        return {
            "state": "ready" if authenticated else "login_required",
            "authenticated": authenticated,
            "url": url,
            "title": title,
            "message": (
                "浏览器会话已登录，可调用 ali_pc_search_judicial"
                if authenticated
                else "请在已打开的 Chrome 窗口手动登录，完成后调用 ali_pc_browser_status"
            ),
            "cookie_policy": "browser_memory_only",
            "cookie_exported": False,
            "cookie_persisted_by_adapter": False,
        }

    @staticmethod
    def _action_required(url: str, title: str, body: str) -> dict | None:
        combined = f"{title}\n{body}"
        if any(marker in url for marker in _PUNISH_MARKERS):
            return {
                "state": "action_required",
                "reason": "risk_control",
                "message": "页面进入 Ali 风控页，请用户在浏览器中手动处理；适配器不会绕过验证",
                "url": url,
            }
        if any(marker in combined for marker in _VERIFICATION_MARKERS):
            return {
                "state": "action_required",
                "reason": "verification",
                "message": "页面要求人工验证，请用户手动完成；适配器不会操作验证码或滑块",
                "url": url,
            }
        if "login.taobao.com" in url:
            return {
                "state": "action_required",
                "reason": "login",
                "message": "请用户在浏览器中手动登录，适配器不会读取或填写凭据",
                "url": url,
            }
        return None

    async def _navigate_exact_link(self, label: str, dimension: str) -> None:
        assert self._page is not None
        hrefs = await self._page.eval_on_selector_all(
            "a[href]",
            """(anchors, wanted) => anchors
                .filter(a => (a.innerText || a.textContent || '').trim() === wanted)
                .map(a => a.href)
                .filter(Boolean)""",
            label,
        )
        unique = [
            href for href in dict.fromkeys(hrefs)
            if urlsplit(href).scheme == "https" and urlsplit(href).hostname == _PC_ALLOWED_HOST
        ]
        if len(unique) != 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法在当前 PC 页面唯一解析{dimension}中文名: {label}",
                {
                    "dimension": dimension,
                    "name": label,
                    "match_count": len(unique),
                    "url": self._page.url,
                },
            )
        await self._page.goto(urljoin(self._page.url, unique[0]), wait_until="domcontentloaded")

    async def _select_option_exact(self, label: str, dimension: str) -> None:
        assert self._page is not None
        selects = await self._page.eval_on_selector_all(
            "select",
            """selects => selects.map((select, index) => ({
                index,
                options: Array.from(select.options).map(o => (o.textContent || '').trim())
            }))""",
        )
        matches = [entry for entry in selects if label in entry["options"]]
        if len(matches) != 1:
            raise AliPCBrowserError(
                "pc_filter_resolution_failed",
                f"无法唯一解析{dimension}选项: {label}",
                {"dimension": dimension, "name": label, "match_count": len(matches)},
            )
        locator = self._page.locator("select").nth(matches[0]["index"])
        await locator.select_option(label=label)
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            # 部分旧页面 change 事件只做同步跳转；后续状态检查负责判定结果。
            pass

    async def _wait_for_items(self, limit: int) -> tuple[list[dict], dict]:
        """轮询等待旧 PC 页面异步渲染拍品卡片，避免 DOMContentLoaded 过早解析."""
        assert self._page is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_ms / 1000
        attempts = 0
        last_candidate_count = 0
        while True:
            attempts += 1
            records = await self._page.eval_on_selector_all(
                "a[href]",
                r"""anchors => anchors.map(a => {
                    const href = a.href || '';
                    if (!/\d{8,}/.test(href) || !/(?:sf_item\/|item\.htm)/.test(href)) return null;
                    let root = a;
                    for (let i = 0; i < 8 && root && root.parentElement; i++) {
                        const parentText = (root.parentElement.innerText || '').trim();
                        if (/当前价|变卖价|起拍价|开拍价|评估价|开始时间/.test(parentText)
                            && parentText.length < 2500) {
                            root = root.parentElement;
                            break;
                        }
                        root = root.parentElement;
                    }
                    const image = root ? root.querySelector('img') : null;
                    return {
                        href,
                        title: (a.getAttribute('title') || a.innerText || '').trim(),
                        text: root ? (root.innerText || '').trim() : '',
                        image: image ? (image.currentSrc || image.src || image.getAttribute('data-ks-lazyload')) : null
                    };
                }).filter(Boolean)""",
            )
            last_candidate_count = len(records)
            items = parse_pc_item_records(records, limit=limit)
            if items:
                return items, {
                    "poll_attempts": attempts,
                    "candidate_record_count": last_candidate_count,
                }
            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                return [], {
                    "poll_attempts": attempts,
                    "candidate_record_count": last_candidate_count,
                }
            await self._page.wait_for_timeout(min(500, remaining_ms))

    async def search(
        self,
        *,
        keyword: str | None = None,
        category: str | None = None,
        province: str | None = None,
        city: str | None = None,
        district: str | None = None,
        asset_type: str | None = None,
        sort: str | None = None,
        status: str | None = None,
        stage: str | None = None,
        min_price_yuan: int | float | str | None = None,
        max_price_yuan: int | float | str | None = None,
        auction_start_from: str | None = None,
        auction_start_to: str | None = None,
        limit: int = 20,
    ) -> dict:
        lock = await self._locked()
        async with lock:
            if not self._page or self._page.is_closed():
                return {
                    "error": "pc_browser_not_started",
                    "message": "请先调用 ali_pc_browser_start，并在打开的 Chrome 窗口完成登录",
                }
            session = await self._status_unlocked()
            if session.get("state") != "ready":
                return session
            if not 1 <= limit <= 100:
                return AliPCBrowserError(
                    "pc_filter_validation_failed", "limit 必须在 1 到 100 之间"
                ).as_dict()

            scoped = any((category, province, city, district, asset_type, sort, status, stage))
            try:
                # 真实页面已验证关键词会清空其他筛选，组合时必须拒绝。
                if keyword and (scoped or min_price_yuan is not None or max_price_yuan is not None
                                or auction_start_from or auction_start_to):
                    raise AliPCBrowserError(
                        "pc_keyword_filter_conflict",
                        "关键词搜索会清空其他 PC 筛选，当前不支持组合",
                    )

                await self._page.goto(PC_HOME_URL, wait_until="domcontentloaded")
                if keyword:
                    target = build_pc_search_url(keyword=keyword)
                    await self._page.goto(target, wait_until="domcontentloaded")
                else:
                    for label, dimension in (
                        (category, "category"),
                        (province, "province"),
                        (city, "city"),
                        (district, "district"),
                        (asset_type, "asset_type"),
                    ):
                        if label:
                            await self._navigate_exact_link(label, dimension)
                    for label, dimension in (
                        (sort, "sort"),
                        (status, "status"),
                        (stage, "stage"),
                    ):
                        if label:
                            await self._select_option_exact(label, dimension)
                    target = build_pc_search_url(
                        self._page.url,
                        min_price_yuan=min_price_yuan,
                        max_price_yuan=max_price_yuan,
                        auction_start_from=auction_start_from,
                        auction_start_to=auction_start_to,
                    )
                    if target != self._page.url:
                        await self._page.goto(target, wait_until="domcontentloaded")

                title = await self._page.title()
                body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                action = self._action_required(self._page.url, title, body)
                if action:
                    return action
                items, parse_diagnostics = await self._wait_for_items(limit)
                if not items:
                    final_body = await self._page.locator("body").inner_text(timeout=self.timeout_ms)
                    return {
                        "error": "pc_result_parse_failed",
                        "message": "PC 页面已加载，但未能识别拍品卡片；未返回未经验证的空结果",
                        "diagnostics": {
                            "url": self._page.url,
                            "title": title,
                            "body_length": len(final_body),
                            "body_has_price_markers": bool(re.search(
                                r"当前价|变卖价|起拍价|开拍价|评估价|开始时间",
                                final_body,
                            )),
                            **parse_diagnostics,
                        },
                    }
                return {
                    "source": "ali_pc_browser",
                    "count": len(items),
                    "items": items,
                    "url": self._page.url,
                    "authenticated_session": True,
                    "cookie_policy": "browser_memory_only",
                }
            except AliPCBrowserError as exc:
                return exc.as_dict()
            except Exception as exc:
                return {
                    "error": "pc_browser_search_failed",
                    "message": str(exc),
                    "diagnostics": {"url": self._page.url if self._page else None},
                }

    async def close(self) -> dict:
        lock = await self._locked()
        async with lock:
            await self._close_unlocked()
            return {
                "state": "stopped",
                "cookie_policy": "browser_memory_only",
                "cookie_exported": False,
                "cookie_persisted_by_adapter": False,
            }

    async def _close_unlocked(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
