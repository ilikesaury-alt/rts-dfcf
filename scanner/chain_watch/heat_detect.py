from scanner.chain_watch.chains import match_chain_simple

HEAT_HOT_THRESHOLD = 5
HEAT_WARM_THRESHOLD = 3


def detect_hot_chains(raw_items: list[dict]) -> dict:
    chain_stocks: dict[str, list[dict]] = {}
    chain_bottleneck_active: dict[str, bool] = {}
    chain_rank_changes: dict[str, list[int]] = {}
    chain_volumes: dict[str, list[float]] = {}

    for item in raw_items:
        name = item.get("name", "")
        chain = match_chain_simple(name)
        if chain is None:
            continue

        if chain not in chain_stocks:
            chain_stocks[chain] = []
            chain_bottleneck_active[chain] = False
            chain_rank_changes[chain] = []
            chain_volumes[chain] = []

        chain_stocks[chain].append(item)
        chain_rank_changes[chain].append(abs(item.get("rank_change") or 0))
        chain_volumes[chain].append(item.get("volume", 0) or 0)

        from scanner.chain_watch.chains import match_chains
        node_matches = match_chains(name)
        for _, _, is_bottleneck in node_matches:
            if is_bottleneck:
                chain_bottleneck_active[chain] = True

    result = {}
    for chain_name, stocks in chain_stocks.items():
        count = len(stocks)
        if count >= HEAT_HOT_THRESHOLD:
            heat = "hot"
        elif count >= HEAT_WARM_THRESHOLD:
            heat = "warm"
        else:
            heat = "cold"

        avg_rank_change = (
            sum(chain_rank_changes[chain_name]) / len(chain_rank_changes[chain_name])
            if chain_rank_changes[chain_name] else 0
        )

        result[chain_name] = {
            "heat": heat,
            "stock_count": count,
            "stocks": stocks,
            "bottleneck_active": chain_bottleneck_active[chain_name],
            "avg_rank_change": round(avg_rank_change, 0),
        }

    return dict(sorted(result.items(), key=lambda x: -x[1]["stock_count"]))
