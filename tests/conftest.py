"""pytest 配置: 让仓库根可 import + 提供 --run-live 开关跳过/启用集成测试."""
from __future__ import annotations
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def pytest_addoption(parser):
    parser.addoption(
        "--run-live", action="store_true", default=False,
        help="跑标了 @pytest.mark.live 的集成测试 (打真 Ali / JD API).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 真打线上 API 的集成测试 (默认跳过, --run-live 启用)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="需要 --run-live 才跑")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
