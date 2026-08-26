# -*- coding: utf-8 -*-
import sqlite3
import sys
from datetime import date, timedelta
from scanner.config import DB_PATH
from scanner.core_themes import core_stock_symbols

sys.stdout = open("_reorder_out.txt", "w", encoding="utf-8")

# 用户提供的终端收盘后列表（档0/档1/档3）
RAW = [
    {"grp": "档0", "code": "SZ300204", "name": "舒泰神", "chg": "+1.92%", "acc5": "-12.05%",
     "price": 21.73, "rank": "40", "sector": "医药生物", "stg": "RBD", "score": 41, "time": "14:45"},
    {"grp": "档0", "code": "SZ300395", "name": "菲利华", "chg": "+6.58%", "acc5": "-11.07%",
     "price": 88.76, "rank": "—", "sector": "半导体概念", "stg": "RBD", "score": 34, "time": "11:24"},
    {"grp": "档1", "code": "SZ301373", "name": "凌玮科技", "chg": "+4.33%", "acc5": "-12.05%",
     "price": 99.10, "rank": "—", "sector": "新材料", "stg": "RBD", "score": 59, "time": "11:24"},
    {"grp": "档1", "code": "SZ301583", "name": "托伦斯", "chg": "+2.77%", "acc5": "-23.62%",
     "price": 126.48, "rank": "—", "sector": "国产芯片", "stg": "RBD", "score": 58, "time": "09:31"},
    {"grp": "档1", "code": "SZ301235", "name": "华康医疗", "chg": "+10.51%", "acc5": "-15.57%",
     "price": 45.72, "rank": "—", "sector": "医药生物", "stg": "RBD", "score": 55, "time": "09:31"},
    {"grp": "档1", "code": "SZ300720", "name": "海川智能", "chg": "+2.89%", "acc5": "-18.12%",
     "price": 73.95, "rank": "14", "sector": "新材料", "stg": "RBD", "score": 47, "time": "14:49"},
    {"grp": "档1", "code": "SZ300571", "name": "平治信息", "chg": "+2.40%", "acc5": "-11.59%",
     "price": 30.69, "rank": "13", "sector": "通信技术", "stg": "RBD", "score": 46, "time": "14:49"},
    {"grp": "档1", "code": "SZ300534", "name": "陇神戎发", "chg": "+2.21%", "acc5": "-13.33%",
     "price": 17.14, "rank": "—", "sector": "医药生物", "stg": "RBD", "score": 43, "time": "09:31"},
    {"grp": "档3", "code": "SZ300927", "name": "江天化学", "chg": "+11.86%", "acc5": "+25.83%",
     "price": 31.88, "rank": "—", "sector": "央国企改革", "stg": "MOM", "score": 80, "time": "10:03"},
    {"grp": "档3", "code": "SZ300377", "name": "赢时胜", "chg": "+2.54%", "acc5": "+0.64%",
     "price": 14.51, "rank": "—", "sector": "计算机", "stg": "NEW", "score": 53, "time": "11:16"},
    {"grp": "档3", "code": "SZ300191", "name": "潜能恒信", "chg": "+7.63%", "acc5": "+16.15%",
     "price": 37.26, "rank": "43", "sector": "趋势股", "stg": "ST", "score": 93, "time": "13:25"},
    {"grp": "档3", "code": "SZ300328", "name": "宜安科技", "chg": "+7.98%", "acc5": "+22.75%",
     "price": 17.32, "rank": "4", "sector": "央国企改革", "stg": "ST", "score": 78, "time": "10:31"},
    {"grp": "档3", "code": "SZ300085", "name": "银之杰", "chg": "+4.30%", "acc5": "+5.60%",
     "price": 30.11, "rank": "—", "sector": "计算机", "stg": "ST", "score": 77, "time": "11:24"},
    {"grp": "档3", "code": "SZ301580", "name": "爱迪特", "chg": "+9.43%", "acc5": "+4.54%",
     "price": 55.70, "rank": "—", "sector": "专精特新", "stg": "ST", "score": 69, "time": "13:14"},
    {"grp": "档3", "code": "SZ300120", "name": "经纬辉开", "chg": "+1.87%", "acc5": "+8.28%",
     "price": 9.28, "rank": "—", "sector": "国产芯片", "stg": "ST", "score": 68, "time": "09:47"},
    {"grp": "档3", "code": "SZ300761", "name": "立华股份", "chg": "+2.81%", "acc5": "+5.21%",
     "price": 19.38, "rank": "45", "sector": "农林牧渔", "stg": "ST", "score": 68, "time": "14:53"},
]

DATE = "2026-08-26"
tier_map = {"档0": 0, "档1": 1, "档3": 3}
c = sqlite3.connect(DB_PATH)

# ★核心票 = 终端名称被高亮的票 = display.py 的 core_stock_symbols 集合
core_syms = core_stock_symbols(c, DATE)
print(f"[调试] core_stock_symbols 命中 {len(core_syms)} 只，样本: "
      f"{sorted(list(core_syms))[:12]}", flush=True)

rows = []
for d in RAW:
    sym = d["code"]
    has_rank = d["rank"] != "—"
    rank_val = int(d["rank"]) if has_rank else 9999
    is_core = sym in core_syms            # ★ 核心票（终端高亮）
    is_new = (d["stg"] == "NEW")          # 陌生面孔
    chg = float(d["chg"].replace("%", "").replace("+", ""))
    rows.append({**d, "tier": tier_map[d["grp"]], "has_rank": has_rank, "rank_val": rank_val,
                 "is_core": is_core, "is_new": is_new, "chg": chg})

# 排序键严格按规则 1→5：
# ① 有榜单排名 → ② 核心票(高亮) → ③ 档位 → ④ 陌生面孔 → ⑤ 名次/涨幅
def sort_key(r):
    return (0 if r["has_rank"] else 1,
            0 if r["is_core"] else 1,
            r["tier"],
            0 if r["is_new"] else 1,
            r["rank_val"],
            r["chg"])   # 涨幅升序：低涨幅在前（用户偏好：涨高了怕）

rows.sort(key=sort_key)

print()
print(f"{'榜单排名':<8}{'名称':<10}{'档位':<6}{'陌生':<5}{'涨幅':<9}{'策略':<5}{'评分':<5}{'5日累计':<9}{'板块'}")
print("-" * 70)
for r in rows:
    rank_s = r["rank"] if r["has_rank"] else "无"
    name_s = ("★" + r["name"]) if r["is_core"] else r["name"]
    new_s = "[新]" if r["is_new"] else ""
    print(f"{rank_s:<8}{name_s:<10}{r['grp']:<6}{new_s:<5}{r['chg']:>+7.2f}%{r['stg']:<5}{r['score']:<5}{r['acc5']:<9}{r['sector']}")

print()
print("图例: ★=核心票(终端名称高亮，即 core_stock_symbols：核心主题成员+20日走强龙头)")
print("      [新]=陌生面孔(strategy=NEW) | 排序: 有榜单排名→核心票→档位→陌生面孔→名次/涨幅(低在前)")
