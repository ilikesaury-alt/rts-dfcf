# 多字/全词优先匹配（顺序即优先级），避免单字误命中
SECTOR_MULTI_KEYWORDS: dict[str, list[str]] = {
    "半导体": ["半导体", "芯片", "集成电路", "封测"],
    "新能源": ["新能源", "光伏", "风电", "锂电", "锂电池", "储能", "氢能"],
    "医药": ["医药", "医疗", "生物制药", "制药", "生物医药"],
    "电子": ["电子", "电子元器件", "元器件"],
    "计算机": ["计算机", "软件", "数字", "数据", "人工智能", "AI"],
    "通信": ["通信", "通讯", "5G"],
    "消费": ["食品", "饮料", "家电", "白酒", "乳业", "消费"],
    "军工": ["军工", "航天", "航空", "国防"],
    "汽车": ["汽车", "汽车电子", "新能源车", "新能源汽车", "特斯拉", "汽车零部件"],
    "机械": ["机械", "装备", "精密"],
    "有色": ["有色", "金属", "钢铁", "黄金", "铜箔", "铝"],
    "化工": ["化工", "化学", "石化"],
    "地产": ["地产", "房地产", "置业"],
    "金融": ["银行", "证券", "保险", "金融"],
    "传媒": ["传媒", "影视", "游戏", "动漫"],
    "电力": ["电力", "电网", "电气", "电气装备"],
    "交运": ["运输", "物流", "港口", "航运"],
    "环保": ["环保", "水务", "节能"],
    "农业": ["农业", "种业", "养殖"],
}

# 单字白名单（仅在多字未命中时使用，且需精确包含）
SECTOR_SINGLE_KEYWORDS: dict[str, list[str]] = {
    "医药": ["药"],
    "电子": ["电子"],
    "计算机": ["智能"],
    "通信": ["通信"],
    "汽车": ["车"],
    "有色": ["铜", "铝", "金"],
    "化工": ["化"],
    "电力": [],
    "机械": ["机"],
    "传媒": ["影", "游"],
}

# 向后兼容：保留旧 dict 供参考（不再用于匹配）
SECTOR_KEYWORDS = SECTOR_MULTI_KEYWORDS

# 预计算匹配对（模块加载时一次性构建，避免每次 classify_sector 重建）
_SECTOR_PAIRS: list[tuple[str, str]] = []
for _sector, _keywords in SECTOR_MULTI_KEYWORDS.items():
    _SECTOR_PAIRS.extend((_sector, kw) for kw in _keywords)
for _sector, _keywords in SECTOR_SINGLE_KEYWORDS.items():
    _SECTOR_PAIRS.extend((_sector, kw) for kw in _keywords)
_SECTOR_PAIRS.sort(key=lambda x: -len(x[1]))


def classify_sector(name: str) -> str:
    # 统一最长匹配：按关键词长度降序扫描，
    # 保证"电子"优先于"电"、"新能源车"优先于"新能源"，消除子串碰撞。
    for sector, kw in _SECTOR_PAIRS:
        if kw in name:
            return sector
    return "其他"


def get_sector_clusters(stocks: list) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = {}
    for s in stocks:
        sec = classify_sector(s.name)
        if sec not in clusters:
            clusters[sec] = []
        clusters[sec].append(s.symbol)
    return clusters
