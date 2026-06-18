from scanner.sector import classify_sector

YI = 100_000_000


def _rank_to_rank_change(rank, proxy_map):
    for threshold_rank, rc_val in sorted(proxy_map.items()):
        if rank <= threshold_rank:
            return rc_val
    return 0


def score_new_face(today_pct, accumulated, vol_ratio, recent_3_pcts,
                   stock_rank, stock_value, params):
    p = params["new_face"]
    score = 0
    tp = p["today_pct"]

    if tp["golden_min"] <= today_pct <= tp["golden_max"]:
        score += tp["golden_score"]
    elif today_pct < tp["golden_min"]:
        score += tp["low_score"]
    elif today_pct > tp["golden_max"]:
        if today_pct > tp["golden_max"] + tp.get("overheat_threshold_delta", 2):
            score += tp["overheat_score"]
        else:
            score += tp["high_score"]

    acc = p["accumulated"]
    if acc["sweet_min"] < accumulated < acc["sweet_max"]:
        score += acc["sweet_score"]
    if accumulated >= acc["warn_threshold"]:
        score += acc["warn_penalty"]
    if accumulated >= acc["danger_threshold"]:
        score += acc["danger_penalty"]

    bt = p["bottom"]
    no_heavy_loss = all(pct > bt["max_daily_loss"] for pct in recent_3_pcts)
    volume_surge = vol_ratio > bt["min_vol_ratio"]
    if no_heavy_loss and volume_surge:
        score += bt["confirmed_score"]
    if volume_surge:
        score += bt["volume_surge_score"]

    rc = p["rank_change"]
    rc_val = _rank_to_rank_change(stock_rank, params["rank_proxy"])
    if rc_val >= rc["strong_threshold"]:
        score += rc["strong_score"]
    elif rc_val >= rc["medium_threshold"]:
        score += rc["medium_score"]

    v = p["value"]
    if stock_value >= v["high_threshold"]:
        score += v["high_score"]
    elif stock_value >= v["medium_threshold"]:
        score += v["medium_score"]

    cb = p["combo"]
    if today_pct <= cb["max_today_pct"] and accumulated < cb["max_accumulated"]:
        score += cb["score"]

    return score


def score_momentum(today_pct, accumulated, vol_ratio, recent_5_pcts,
                   stock_rank, stock_value, params):
    p = params["momentum"]
    score = 0

    if today_pct <= 0:
        return 0

    tp = p["today_pct"]
    if today_pct >= tp.get("overheat_threshold", 8.0):
        return 0
    if tp["golden_min"] <= today_pct <= tp["golden_max"]:
        score += tp["golden_score"]
    elif today_pct < tp["golden_min"]:
        score += tp["low_score"]

    acc = p["accumulated"]
    if accumulated < acc["sweet_min"]:
        return 0
    if accumulated >= acc["danger_threshold"]:
        score += acc["danger_score"]
    elif accumulated >= acc["high_threshold"]:
        score += acc["high_score"]
    elif accumulated >= acc["mid_threshold"]:
        score += acc["mid_score"]
    else:
        score += acc["sweet_score"]

    vol = p["volume"]
    if vol["healthy_min"] < vol_ratio < vol["healthy_max"]:
        score += vol["healthy_score"]
    elif vol_ratio >= vol["surge_min"]:
        score += vol["surge_score"]
    elif vol_ratio <= vol["low_max"]:
        score += vol["low_score"]

    no_crash = p["no_crash"]
    has_crash_day = any(pct <= no_crash["crash_threshold"] for pct in recent_5_pcts)
    recent_2_return = sum(recent_5_pcts[-2:]) if len(recent_5_pcts) >= 2 else 0
    if not has_crash_day and recent_2_return > no_crash["recent_2_return"]:
        score += no_crash["score"]

    rc = p["rank_change"]
    rc_val = _rank_to_rank_change(stock_rank, params["rank_proxy"])
    if rc_val >= rc["strong_threshold"]:
        score += rc["strong_score"]
    elif rc_val >= rc["medium_threshold"]:
        score += rc["medium_score"]

    v = p["value"]
    if stock_value >= v["high_threshold"]:
        score += v["high_score"]
    elif stock_value >= v["medium_threshold"]:
        score += v["medium_score"]

    return score
