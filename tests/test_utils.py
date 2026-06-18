import pytest
from scanner.utils import is_st, _strip_exchange, is_gem, is_hk_stock, detect_board


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

    def test_normal_stock(self):
        assert not is_st("贵州茅台")
        assert not is_st("宁德时代")
        assert not is_st("东方财富")


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


class TestDetectBoard:
    def test_hk(self):
        assert detect_board("00700", "") == "港股"

    def test_gem(self):
        assert detect_board("SZ300999", "300999") == "创业板"

    def test_kcb(self):
        assert detect_board("SH688001", "688001") == "科创板"

    def test_main_board(self):
        assert detect_board("SH600519", "600519") == "主板"
