"""Packaged, read-only runtime data for auction-mcp."""
from __future__ import annotations

from importlib.resources import files
import json
from typing import Any


_RESOURCE_NAMES = frozenset(
    {
        "gb2260.json",
        "gb2260_200712.json",
        "jd_areas.json",
        "mcp_contract.json",
    }
)


def resource_path(name: str):
    """Return a traversable for one packaged runtime resource."""
    if name not in _RESOURCE_NAMES:
        raise ValueError(f"unknown auction-mcp resource: {name!r}")
    return files(__name__).joinpath(name)


def load_json(name: str) -> Any:
    """Load a UTF-8 JSON resource without depending on the current cwd."""
    return json.loads(resource_path(name).read_text(encoding="utf-8"))
