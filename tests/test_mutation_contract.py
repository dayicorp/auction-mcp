"""Explicit invariants used by the deterministic safety mutation gate."""
from __future__ import annotations

import json
from pathlib import Path

from ali_h5_client import AliH5Client
from jd_h5_client import JDH5Client


ROOT = Path(__file__).resolve().parents[1]


def test_offline_provider_defaults_remain_fail_closed():
    assert AliH5Client.DEFAULT_STATUS_ORDERS == ["0", "1"]
    assert JDH5Client.DEFAULT_STATUS == "101,102"


def test_required_item_detail_parameter_stays_required_in_public_contract():
    contract = json.loads(
        (ROOT / "mcp_contract.json").read_text(encoding="utf-8")
    )
    parameter = contract["tools"]["ali_pc_get_item_detail"]["parameters"][
        "item_id"
    ]
    assert parameter == {
        "nullable": False,
        "required": True,
        "type": "string",
    }
