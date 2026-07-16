from scanner.sector import classify_sector


def test_collision_car_electronics():
    # "汽车电子" 应归汽车而非电子（子串碰撞修复）
    assert classify_sector("汽车电子") == "汽车"


def test_new_energy_vehicle():
    assert classify_sector("新能源汽车") == "汽车"


def test_electronic_info():
    # "电子信息" 应归电子，而非被计算机的"信息"误吞
    assert classify_sector("电子信息") == "电子"


def test_medical():
    assert classify_sector("医药生物") == "医药"
    assert classify_sector("药明康德") == "医药"


def test_semiconductor():
    assert classify_sector("半导体芯片") == "半导体"


def test_power_equipment():
    assert classify_sector("电力设备") == "电力"


def test_no_false_new_energy():
    # 名字不含任何行业关键词 → 其他，而非被单字"电"误吞
    assert classify_sector("中航机电") in ("机械", "其他")


def test_computer():
    assert classify_sector("计算机软件") == "计算机"


def test_unknown():
    assert classify_sector("某某股份") == "其他"


def test_concept_hint_overrides_name():
    # 名字无行业关键词，但同花顺概念标签含"半导体" → 用概念标签归类
    assert classify_sector("某某股份", concept_hint="半导体器件") == "半导体"
    # 概念标签为空时退化为名字匹配
    assert classify_sector("医药生物", concept_hint="") == "医药"
