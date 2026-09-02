"""pytest 共享配置。

`smoke` 标记：依赖真实 scanner.db / 外网的集成测试。默认跳过（保持单测快速、密封、
可在无库/CI 环境稳定运行），显式 `--run-smoke` 时运行。

用法：
    python -m pytest tests/               # 只跑单元测试（跳过 smoke 集成测试）
    python -m pytest tests/ --run-smoke   # 含真实库/外网依赖的集成测试
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-smoke",
        action="store_true",
        default=False,
        help="运行真实数据库/外网依赖的 smoke 集成测试",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-smoke"):
        return  # 显式开启：全部运行
    skip_smoke = pytest.mark.skip(reason="真实库/外网集成测试，加 --run-smoke 运行")
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip_smoke)
