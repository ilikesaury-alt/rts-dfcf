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


# A 股主板/创业板/科创板代码前缀（6 位代码的前 2 位）
_A_SHARE_PREFIXES = ("30", "60", "00", "68", "43", "83", "87", "92")


def is_hk_stock(symbol: str) -> bool:
    """判断是否为港股 symbol。

    雪球 A 股 symbol 带交易所前缀（SZ/SH/BJ），港股为纯数字代码。
    纯数字但符合 A 股代码格式（6 位且以 A 股特征前缀开头）不视为港股，
    避免无前缀的 A 股代码（如数据库残留或外部注入）被误判为港股而过滤。
    """
    if not symbol.isdigit():
        return False
    # 6 位且以 A 股前缀开头 → 视为无前缀的 A 股代码，不当港股
    if len(symbol) == 6 and symbol[:2] in _A_SHARE_PREFIXES:
        return False
    return True
