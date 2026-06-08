def is_st(name: str) -> bool:
    return name.startswith("*ST") or name.startswith("ST") or "退" in name


def _strip_exchange(code: str) -> str:
    return code[2:] if code[:2] in ("SH", "SZ", "BJ") and len(code) > 2 else code


def is_gem(code: str) -> bool:
    return _strip_exchange(code).startswith("30")


def is_main_board(code: str) -> bool:
    raw = _strip_exchange(code)
    return raw.startswith(("00", "60"))


def is_allowed_board(code: str) -> bool:
    raw = _strip_exchange(code)
    if raw.startswith(("30", "00", "60")):
        return True
    return False


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
