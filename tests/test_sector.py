import pytest
from scanner.sector import classify_sector


class TestClassifySector:
    def test_semiconductor(self):
        assert classify_sector("韦尔股份半导体") == "半导体"
        assert classify_sector("中芯国际芯片") == "半导体"

    def test_new_energy(self):
        assert classify_sector("宁德时代新能源") == "新能源"
        assert classify_sector("隆基绿能光伏") == "新能源"

    def test_pharma(self):
        assert classify_sector("恒瑞医药") == "医药"

    def test_unknown_sector(self):
        assert classify_sector("某某综合企业") == "其他"
        assert classify_sector("") == "其他"
