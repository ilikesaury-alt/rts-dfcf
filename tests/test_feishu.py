"""飞书推送测试。"""
from scanner.feishu import push_feishu


def test_empty_recommendations_no_push(monkeypatch):
    """回归（2026-08-17 审查修复）：无任何推荐时不推空卡片——此前全空时
    has_change=False 但 (now-0)>=FEISHU_MIN_INTERVAL 恒真，每 5 分钟推一张
    "0新 0动"空卡刷屏（早盘/清淡日）。空卡无信息量。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setattr("scanner.feishu._last_push_time", 0.0)
    monkeypatch.setattr("scanner.feishu._last_push_symbols", set())
    assert push_feishu([], [], [], gem_total=10) is False