from scanner.config import (
    V_MO_DIVERGENCE_BEAR,
    V_MO_DIVERGENCE_NONE,
    V_MO_MA_FULL,
    V_MO_MA_NONE,
    V_MO_MA_PARTIAL,
    V_MO_VOL_SPIKE,
    V_MO_VOL_STABLE,
    V_MO_VOL_UP,
    V_NF_CONVERGE_PARTIAL,
    V_NF_CONVERGE_STRONG,
    V_NF_HL_CLEAR,
    V_NF_HL_FAIL,
    V_NF_HL_STABLE,
    V_NF_SECTOR_MOD,
    V_NF_SECTOR_STRONG,
    V_NF_SECTOR_WEAK,
    V_PB_MA_DOWN,
    V_PB_MA_FLAT,
    V_PB_MA_UP,
    V_PB_SECTOR_COLD,
    V_PB_SECTOR_DEAD,
    V_PB_SECTOR_HOT,
    V_PB_SHRINK_MOD,
    V_PB_SHRINK_NO,
    V_PB_SHRINK_YES,
)
from scanner.indicators import compute_kdj, compute_macd, compute_rsi
from scanner.sector import classify_sector


def _nf_convergence(closes: list[float], historical_kline: list[dict]) -> tuple[int, str]:
    if len(closes) < 10:
        return 0, "data_short"

    rsi = compute_rsi(closes, period=6)
    macd = compute_macd(closes)
    kdj = compute_kdj(
        [k["high"] for k in historical_kline],
        [k["low"] for k in historical_kline],
        closes,
    )

    hits = 0

    if rsi is not None and rsi < 40:
        hits += 1
    if macd is not None and macd["histogram"] > 0 and macd["histogram_prev"] <= 0:
        hits += 1
    if kdj is not None and kdj["K"] < 20 and kdj["K"] > kdj["D"]:
        hits += 1

    if hits >= 3:
        return V_NF_CONVERGE_STRONG, f"converge_3of3"
    if hits >= 2:
        return V_NF_CONVERGE_PARTIAL, f"converge_{hits}of3"
    return 0, "converge_weak"


def _nf_higher_low(closes: list[float]) -> tuple[int, str]:
    if len(closes) < 10:
        return 0, "data_short"

    recent_zone = min(closes[-5:])
    prev_zone = min(closes[-10:-5])

    if recent_zone > prev_zone * 1.01:
        return V_NF_HL_CLEAR, f"hl_clear_{recent_zone/prev_zone:.3f}"
    if recent_zone > prev_zone * 0.98:
        return V_NF_HL_STABLE, f"hl_stable_{recent_zone/prev_zone:.3f}"
    return V_NF_HL_FAIL, f"hl_fail_{recent_zone/prev_zone:.3f}"


def _nf_sector(name: str, clusters: dict[str, list[str]] | None) -> tuple[int, int]:
    if not clusters:
        return V_NF_SECTOR_WEAK, 0
    sec = classify_sector(name)
    count = len(clusters.get(sec, []))
    if count >= 3:
        return V_NF_SECTOR_STRONG, count
    if count >= 2:
        return V_NF_SECTOR_MOD, count
    return V_NF_SECTOR_WEAK, count


def validate_nf(stock, kline_summary, closes: list[float],
                historical_kline: list[dict], clusters: dict[str, list[str]] | None
                ) -> tuple[bool, int, dict]:
    conv_bonus, conv_detail = _nf_convergence(closes, historical_kline)
    hl_bonus, hl_detail = _nf_higher_low(closes)
    sec_bonus, sec_count = _nf_sector(stock.name, clusters)

    details: dict[str, int | float | str] = {
        "v_nf_convergence": conv_bonus,
        "v_nf_convergence_detail": conv_detail,
        "v_nf_higher_low": hl_bonus,
        "v_nf_higher_low_detail": hl_detail,
        "v_nf_sector": sec_bonus,
        "v_nf_sector_count": sec_count,
    }

    total = conv_bonus + hl_bonus + sec_bonus

    pos_dims = sum(1 for b in (conv_bonus, hl_bonus, sec_bonus) if b > 0)
    passed = pos_dims >= 2

    return passed, total, details


def _mo_ma_alignment(closes: list[float]) -> tuple[int, str]:
    if len(closes) < 20:
        if len(closes) >= 10:
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            if ma5 > ma10:
                return V_MO_MA_PARTIAL, "ma_partial_5gt10"
            return V_MO_MA_NONE, "ma_none"
        return V_MO_MA_NONE, "data_short"

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20

    if ma5 > ma10 > ma20:
        return V_MO_MA_FULL, "ma_full_5gt10gt20"
    if ma5 > ma10:
        return V_MO_MA_PARTIAL, "ma_partial_5gt10"
    return V_MO_MA_NONE, "ma_none"


def _mo_divergence(closes: list[float], historical_kline: list[dict]) -> tuple[int, str]:
    if len(closes) < 10:
        return V_MO_DIVERGENCE_NONE, "data_short"

    rsi = compute_rsi(closes, period=6)
    if rsi is None:
        return V_MO_DIVERGENCE_NONE, "rsi_na"

    if len(closes) >= 5:
        price_high_now = max(closes[-3:])
        price_high_before = max(closes[-8:-3])
        price_up = price_high_now > price_high_before

        if price_up and len(closes) >= 10:
            rsi_vals = []
            for i in range(3, 0, -1):
                seg = closes[:-i] if i > 0 else closes
                r = compute_rsi(seg, period=6)
                if r is not None:
                    rsi_vals.append(r)
            rsi_vals = rsi_vals[-2:] if len(rsi_vals) >= 2 else []
            if len(rsi_vals) == 2 and rsi_vals[-1] < rsi_vals[-2] * 0.95:
                return V_MO_DIVERGENCE_BEAR, "bear_divergence"

    return V_MO_DIVERGENCE_NONE, "no_divergence"


def _mo_volume_uniformity(historical_kline: list[dict]) -> tuple[int, str]:
    volumes = [k["volume"] for k in historical_kline[-5:]]
    if len(volumes) < 3:
        return 0, "data_short"

    recent_3 = volumes[-3:]
    inc = recent_3[0] <= recent_3[1] <= recent_3[2]
    ratio = max(recent_3) / max(min(recent_3), 0.01) if min(recent_3) > 0 else 99

    if inc and ratio < 2.0:
        return V_MO_VOL_UP, f"vol_up_r{ratio:.1f}"
    if ratio < 1.8:
        return V_MO_VOL_STABLE, f"vol_stable_r{ratio:.1f}"
    return V_MO_VOL_SPIKE, f"vol_spike_r{ratio:.1f}"


def validate_momentum(stock, kline_summary, closes: list[float],
                      historical_kline: list[dict], clusters: dict[str, list[str]] | None
                      ) -> tuple[bool, int, dict]:
    ma_bonus, ma_detail = _mo_ma_alignment(closes)
    div_bonus, div_detail = _mo_divergence(closes, historical_kline)
    vol_bonus, vol_detail = _mo_volume_uniformity(historical_kline)

    details: dict[str, int | float | str] = {
        "v_mo_ma": ma_bonus,
        "v_mo_ma_detail": ma_detail,
        "v_mo_divergence": div_bonus,
        "v_mo_divergence_detail": div_detail,
        "v_mo_volume": vol_bonus,
        "v_mo_volume_detail": vol_detail,
    }

    total = ma_bonus + div_bonus + vol_bonus

    pos_dims = sum(1 for b in (ma_bonus, div_bonus, vol_bonus) if b > 0)
    passed = pos_dims >= 2

    return passed, total, details


def _pb_ma_trend(closes: list[float]) -> tuple[int, str]:
    if len(closes) < 25:
        return V_PB_MA_DOWN, "data_short"

    ma20_now = sum(closes[-20:]) / 20
    ma20_prev = sum(closes[-25:-5]) / 20
    change_pct = (ma20_now - ma20_prev) / max(ma20_prev, 0.01) * 100

    if change_pct > 0.5:
        return V_PB_MA_UP, f"ma20_up_{change_pct:+.1f}%"
    if change_pct > -0.5:
        return V_PB_MA_FLAT, f"ma20_flat_{change_pct:+.1f}%"
    return V_PB_MA_DOWN, f"ma20_down_{change_pct:+.1f}%"


def _pb_shrinkage(kline_summary) -> tuple[int, float]:
    vr = kline_summary.volume_ratio
    if vr < 0.6:
        return V_PB_SHRINK_YES, vr
    if vr < 1.0:
        return V_PB_SHRINK_MOD, vr
    return V_PB_SHRINK_NO, vr


def _pb_sector(name: str, clusters: dict[str, list[str]] | None) -> tuple[int, int]:
    if not clusters:
        return V_PB_SECTOR_DEAD, 0
    sec = classify_sector(name)
    count = len(clusters.get(sec, []))
    if count >= 3:
        return V_PB_SECTOR_HOT, count
    if count >= 1:
        return V_PB_SECTOR_COLD, count
    return V_PB_SECTOR_DEAD, count


def validate_pullback(stock, kline_summary, closes: list[float],
                      historical_kline: list[dict], clusters: dict[str, list[str]] | None
                      ) -> tuple[bool, int, dict]:
    ma_bonus, ma_detail = _pb_ma_trend(closes)
    shr_bonus, shr_vr = _pb_shrinkage(kline_summary)
    sec_bonus, sec_count = _pb_sector(stock.name, clusters)

    details: dict[str, int | float | str] = {
        "v_pb_ma_trend": ma_bonus,
        "v_pb_ma_trend_detail": ma_detail,
        "v_pb_shrinkage": shr_bonus,
        "v_pb_shrinkage_vr": round(shr_vr, 2),
        "v_pb_sector": sec_bonus,
        "v_pb_sector_count": sec_count,
    }

    total = ma_bonus + shr_bonus + sec_bonus

    pos_dims = sum(1 for b in (ma_bonus, shr_bonus, sec_bonus) if b > 0)
    passed = pos_dims >= 2

    return passed, total, details


def validate(cat: str, stock, kline_summary, closes: list[float],
             historical_kline: list[dict], clusters: dict[str, list[str]] | None = None,
             list_presence: dict[str, int] | None = None
             ) -> tuple[bool, int, dict]:
    if cat in ("new_face", "known_new_face"):
        return validate_nf(stock, kline_summary, closes, historical_kline, clusters)
    if cat == "momentum":
        return validate_momentum(stock, kline_summary, closes, historical_kline, clusters)
    if cat == "pullback":
        return validate_pullback(stock, kline_summary, closes, historical_kline, clusters)
    return False, 0, {}
