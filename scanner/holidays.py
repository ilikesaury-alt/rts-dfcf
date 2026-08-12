"""交易日历：从 holidays.json 加载假期列表，缺失/损坏时回退到内置硬编码集合。

config.py re-export HOLIDAYS，保持既有 `from scanner.config import HOLIDAYS` 导入不破。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLIDAYS_FILE = os.path.join(BASE_DIR, "holidays.json")

_HOLIDAYS_FALLBACK: set[str] = {
    "2025-01-01",                              # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",  # 春节
    "2025-04-04", "2025-04-05", "2025-04-06",                # 清明
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",  # 劳动节
    "2025-05-31", "2025-06-01", "2025-06-02",                # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",                # 国庆
    "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",  # 国庆
    "2026-01-01",                              # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春节
    "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",  # 春节
    "2026-04-05", "2026-04-06",                               # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                # 端午
    "2026-09-30",                          # 国庆前 (调休)
    "2026-10-01", "2026-10-02", "2026-10-03",                # 国庆
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",  # 国庆
}


def _load_holidays_from_file(path: str) -> set[str] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        return None
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


HOLIDAYS: set[str] = _load_holidays_from_file(HOLIDAYS_FILE) or _HOLIDAYS_FALLBACK
