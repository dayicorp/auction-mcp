"""Controlled Beike MCP acceptance; run only this file with ``--run-live``."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from statistics import median
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _json_result(result, name: str) -> dict:
    assert not result.isError, f"{name} returned MCP error"
    texts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
    assert len(texts) == 1
    value = json.loads(texts[0])
    assert isinstance(value, dict)
    return value


@pytest.mark.live
def test_real_stdio_beike_positive_and_negative_flows():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="auction-mcp-beike-live-") as temp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "server.py")],
                cwd=temp,
                env=env,
            )
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(parameters, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 15

                        status = _json_result(
                            await session.call_tool("beike_browser_status", {}),
                            "beike_browser_status",
                        )
                        assert status["status"] == "OK"
                        assert status["credential_storage_accessed"] is False

                        search = _json_result(
                            await session.call_tool(
                                "beike_search_xiaoqu",
                                {"city": "江门市", "keyword": "江海花园"},
                            ),
                            "beike_search_xiaoqu",
                        )
                        target = next(
                            item
                            for item in search["candidates"]
                            if item["xiaoqu_id"] == "8895132280985217"
                        )
                        assert "江海区" in target["region"]

                        market = _json_result(
                            await session.call_tool(
                                "beike_get_xiaoqu_market",
                                {
                                    "city": "江门市",
                                    "xiaoqu_id": target["xiaoqu_id"],
                                    "limit": 30,
                                },
                            ),
                            "beike_get_xiaoqu_market",
                        )
                        assert market["status"] == "OK"
                        assert market["statistics"]["valid_count"] >= 1
                        assert market["inventory"]["recommendation_lists_used"] is False
                        prices = []
                        for listing in market["listings"]:
                            assert listing["source_url"].startswith(
                                "https://jiangmen.ke.com/ershoufang/"
                            )
                            assert listing["area_sqm"] > 0
                            assert listing["total_price_wan"] > 0
                            assert listing["unit_price_yuan"] > 0
                            prices.append(listing["unit_price_yuan"])
                        assert market["statistics"]["median_unit_price_yuan"] == median(prices)

                        missing = _json_result(
                            await session.call_tool(
                                "beike_search_xiaoqu",
                                {"city": "江门市", "keyword": "城南五福村"},
                            ),
                            "beike_search_xiaoqu",
                        )
                        assert missing["status"] == "NO_MATCH"
                        assert missing["candidate_count"] == 0
                        assert missing["candidates"] == []
                        print(
                            "BEIKE_LIVE_ACCEPTANCE="
                            + json.dumps(
                                {
                                    "mcp_tool_count": len(tools.tools),
                                    "positive": {
                                        "xiaoqu_id": target["xiaoqu_id"],
                                        "region": target["region"],
                                        "valid_count": market["statistics"]["valid_count"],
                                        "median_unit_price_yuan": market["statistics"][
                                            "median_unit_price_yuan"
                                        ],
                                        "excluded_viewdata_or_ad_count": market[
                                            "inventory"
                                        ]["excluded_viewdata_or_ad_count"],
                                        "recommendation_lists_used": market["inventory"][
                                            "recommendation_lists_used"
                                        ],
                                    },
                                    "negative": {
                                        "keyword": "城南五福村",
                                        "status": missing["status"],
                                        "candidate_count": missing["candidate_count"],
                                    },
                                    "credential_storage_accessed": market[
                                        "credential_storage_accessed"
                                    ],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )

    asyncio.run(run())
