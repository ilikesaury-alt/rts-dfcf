from scanner.utils import _strip_exchange, is_gem, is_hk_stock, is_st


class TestIsSt:
    def test_st_prefix(self):
        assert is_st("ST华英")
        assert is_st("ST康美")

    def test_astrisk_st_prefix(self):
        assert is_st("*ST节能")
        assert is_st("*ST中天")

    def test_delisting(self):
        assert is_st("退市金钰")
        assert is_st("退市秋林")

    def test_delisting_period_trailing_tui(self):
        """退市整理期更名为「XX退」（2026-09-11 修复：此前只判开头与含"退市"）。

        实测样本：300029 天龙退 曾以 rank 11 上榜且未被拦截。
        """
        assert is_st("天龙退")
        assert is_st("长生退")
        assert is_st("退")

    def test_delisting_period_leading_tui(self):
        assert is_st("退市海润")

    def test_normal_stock(self):
        assert not is_st("贵州茅台")
        assert not is_st("宁德时代")
        assert not is_st("东方财富")

    def test_normal_stock_containing_tui_not_matched(self):
        """票名中间含「退」但不以退开头/结尾、不含"退市" → 不误杀。

        「退耕还林」这类以退开头的会被判 True（沿用原 startswith 规则），
        真实 A 股简称无此情况，故只断言中间含退的用例。
        """
        assert not is_st("进退科技")
        assert not is_st("永不退缩")


class TestStripExchange:
    def test_sz_prefix(self):
        assert _strip_exchange("SZ300999") == "300999"

    def test_sh_prefix(self):
        assert _strip_exchange("SH600519") == "600519"

    def test_no_prefix(self):
        assert _strip_exchange("300999") == "300999"

    def test_short_code(self):
        assert _strip_exchange("30") == "30"


class TestIsGem:
    def test_gem_with_prefix(self):
        assert is_gem("SZ300999")

    def test_gem_no_prefix(self):
        assert is_gem("300999")

    def test_not_gem(self):
        assert not is_gem("SH600519")
        assert not is_gem("600519")


class TestIsHkStock:
    def test_hk_symbol(self):
        assert is_hk_stock("00700")

    def test_not_hk(self):
        assert not is_hk_stock("SZ300999")
