def is_st(name: str) -> bool:
    return name.startswith("*ST") or name.startswith("ST") or "退市" in name or name.startswith("退")


def _strip_exchange(code: str) -> str:
    if len(code) > 2 and code[:2] in ("SH", "SZ", "BJ"):
        return code[2:]
    if code.startswith("30"):
        return code
    return code


def is_gem(code: str) -> bool:
    return _strip_exchange(code).startswith("30")


def is_hk_stock(symbol: str) -> bool:
    return symbol.isdigit()


def detect_board(symbol: str, code: str) -> str:
    if is_hk_stock(symbol):
        return "港股"
    if is_gem(code):
        return "创业板"
    raw = _strip_exchange(code)
    if raw.startswith("688"):
        return "科创板"
    return "主板"
