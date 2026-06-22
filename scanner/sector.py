SECTOR_KEYWORDS: dict[str, list[str]] = {
    "半导体": ["半导体", "芯片", "集成电路", "封测"],
    "新能源": ["新能源", "新能", "光伏", "风电", "锂电", "电池", "氢能", "储能"],
    "医药": ["医药", "医疗", "生物", "制药", "药", "医"],
    "电子": ["电子", "元器件"],
    "计算机": ["计算机", "软件", "信息", "数字", "数据", "智能", "AI"],
    "通信": ["通信", "通讯", "5G"],
    "消费": ["消费", "食品", "饮料", "家电", "白酒", "乳业"],
    "军工": ["军工", "航天", "航空", "国防"],
    "汽车": ["汽车", "新能源车", "特斯拉"],
    "机械": ["机械", "装备", "精密"],
    "有色": ["有色", "金属", "钢铁", "黄金", "铜", "铝", "铜箔"],
    "化工": ["化工", "化学", "石化"],
    "地产": ["地产", "房地产", "置业"],
    "金融": ["银行", "证券", "保险", "金融"],
    "传媒": ["传媒", "影视", "游戏", "动漫"],
    "电力": ["电力", "电网", "电气"],
    "交运": ["运输", "物流", "航空", "港口", "航运"],
    "环保": ["环保", "水务", "节能"],
    "农业": ["农业", "种业", "养殖", "牧原"],
}


def classify_sector(name: str) -> str:
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
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
