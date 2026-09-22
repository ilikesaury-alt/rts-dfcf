"""pytest 共享配置。

`smoke` 标记：依赖真实 scanner.db / 外网的集成测试。默认跳过（保持单测快速、密封、
可在无库/CI 环境稳定运行），显式 `--run-smoke` 时运行。

用法：
    python -m pytest tests/               # 只跑单元测试（跳过 smoke 集成测试）
    python -m pytest tests/ --run-smoke   # 含真实库/外网依赖的集成测试
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_caches():
    """清进程级缓存，防用例之间串味（每个用例开始前执行）。

    `scanner.concept._concept_ttl_cache` 是**模块级 dict**（5 分钟 TTL，本意是同一
    扫描进程内跨轮复用）。pytest 单进程连跑多个用例时它照样存活 ⇒ 2026-09-22 实测：
    前一个用例往 `concept_cache` 写了「CPO概念」，后一个用例本该降级到③名称关键词，
    却从进程缓存读回了「CPO概念」—— 用例结果取决于执行顺序（改名/加用例即红）。

    只清缓存不动数据：生产语义（长驻进程 + TTL 复用）不受影响。
    以后再有同款模块级缓存，一并加进来。
    """
    import scanner.concept as concept_mod
    import scanner.kline_fetch as kf_mod

    concept_mod._concept_ttl_cache.clear()
    kf_mod._neg_kline.clear()
    yield


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path, monkeypatch):
    """把日志落盘目录整体重定向到 pytest 的 tmp 目录（每个用例独立）。

    2026-09-15 修：`tests/test_feishu.py::test_push_feishu_failure_does_not_update_state`
    用 `monkeypatch.setattr("scanner.feishu._post_card", lambda card: (False, "飞书返回非 0: xxx"))`
    模拟失败，而 `push_feishu` 失败分支会调用**真实**的 `log_event` —— 于是每次跑单测都往生产
    `logs/feishu_push.log` 追加一行 `push failed: 飞书返回非 0: xxx`（那个 `xxx` 是测试桩的假串，
    飞书真实错误码里没有这一种）。排查「飞书为什么没推送」时，这堆假记录会被当成 18 次真实失败。

    ⚠ 必须打到 **`scanner.log_utils` 自己的命名空间**：它是快照式导入
    （`from scanner.config import LOG_DIR`），改 `scanner.config.LOG_DIR` 对它无效
    （同 `rule_validate` override 传播、`golden_scan` 的 `now_beijing` 覆盖那两处坑）。
    今后若有模块再次快照 `LOG_DIR`，要一并加进来。
    """
    import scanner.log_utils as log_utils

    monkeypatch.setattr(log_utils, "LOG_DIR", str(tmp_path))


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
