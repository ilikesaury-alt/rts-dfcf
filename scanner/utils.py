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
