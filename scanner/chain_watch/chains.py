CHAINS = {
    "AI算力": {
        "keywords": ["光模块", "CPO", "算力", "液冷", "光通信", "AI服务器",
                      "GPU", "800G", "1.6T", "硅光", "MPO", "数据中心"],
        "stock_names": ["中际旭创", "新易盛", "天孚通信", "光迅科技",
                         "华工科技", "德科立", "源杰科技", "长光华芯",
                         "太辰光", "博创科技", "联特科技", "剑桥科技",
                         "铭普光磁", "兆龙互联", "立讯精密"],
        "nodes": [
            {"name": "光芯片/器件", "kw": ["光芯片", "激光器", "探测器", "EML",
                                            "VCSEL", "TEC", "透镜"],
             "bottleneck": True, "weight": 3},
            {"name": "光模块/连接", "kw": ["光模块", "MPO", "连接器", "FA",
                                            "AWG", "WDM"],
             "bottleneck": False, "weight": 2},
            {"name": "服务器/散热", "kw": ["服务器", "液冷", "散热", "交换机",
                                            "数据中心", "算力"],
             "bottleneck": False, "weight": 1},
        ],
    },
    "半导体": {
        "keywords": ["半导体", "芯片", "集成电路", "封测", "晶圆",
                      "存储", "MCU", "模拟芯片", "功率半导体"],
        "stock_names": ["北方华创", "中微公司", "长电科技", "通富微电",
                         "华大九天", "韦尔股份", "卓胜微", "兆易创新",
                         "北京君正", "紫光国微", "士兰微", "华虹公司",
                         "中芯国际", "沪硅产业", "寒武纪", "海光信息",
                         "龙芯中科", "景嘉微", "澜起科技", "圣邦股份",
                         "思瑞浦", "纳芯微", "复旦微电", "安集科技",
                         "中船特气", "华特气体", "容大感光", "南大光电",
                         "上海新阳", "鼎龙股份", "雅克科技", "江丰电子",
                         "有研新材", "飞凯材料", "江波龙", "佰维存储",
                         "朗科科技", "聚辰股份", "普冉股份", "东芯股份",
                         "麦捷科技", "风华高科", "三环集团", "洁美科技"],
        "nodes": [
            {"name": "设备/材料", "kw": ["设备", "材料", "光刻", "掩膜", "硅片",
                                          "沉积", "刻蚀", "清洗", "检测"],
             "bottleneck": True, "weight": 3},
            {"name": "设计/IP", "kw": ["设计", "EDA", "IP", "存储", "MCU",
                                        "模拟", "射频", "SOC"],
             "bottleneck": False, "weight": 2},
            {"name": "封测/制造", "kw": ["封测", "封装", "测试", "先进封装",
                                          "CoWoS", "HBM"],
             "bottleneck": False, "weight": 1},
        ],
    },
    "新能源车": {
        "keywords": ["新能源车", "智能驾驶", "激光雷达", "固态电池",
                      "电驱", "热管理", "汽车电子", "域控"],
        "stock_names": ["比亚迪", "宁德时代", "拓普集团", "德赛西威",
                         "华阳集团", "均胜电子", "伯特利", "保隆科技",
                         "科博达", "星宇股份", "银轮股份", "三花智控",
                         "旭升集团", "文灿股份", "沪光股份", "瑞鹄模具",
                         "常熟汽饰", "新泉股份", "岱美股份"],
        "nodes": [
            {"name": "电池/材料", "kw": ["固态电池", "碳纳米管", "导电剂",
                                          "高压", "锂电", "正极", "负极"],
             "bottleneck": True, "weight": 3},
            {"name": "智能驾驶", "kw": ["智能驾驶", "激光雷达", "域控",
                                          "智驾", "毫米波"],
             "bottleneck": False, "weight": 2},
            {"name": "整车/零部件", "kw": ["整车", "电驱", "热管理",
                                             "汽车电子", "线控"],
             "bottleneck": False, "weight": 1},
        ],
    },
    "光伏储能": {
        "keywords": ["光伏", "储能", "逆变器", "HJT", "钙钛矿",
                      "TOPCon", "BC", "焊带", "银浆"],
        "stock_names": ["阳光电源", "隆基绿能", "通威股份", "晶澳科技",
                         "天合光能", "晶科能源", "德业股份", "锦浪科技",
                         "固德威", "派能科技", "科士达", "上能电气",
                         "盛弘股份", "昱能科技", "禾迈股份", "中信博",
                         "福莱特", "福斯特", "海优新材", "赛伍技术"],
        "nodes": [
            {"name": "电池技术", "kw": ["HJT", "钙钛矿", "TOPCon", "BC",
                                          "异质结", "叠层"],
             "bottleneck": True, "weight": 3},
            {"name": "逆变器/系统", "kw": ["逆变器", "储能", "变流器",
                                             "PCS", "BMS"],
             "bottleneck": False, "weight": 2},
            {"name": "辅材/设备", "kw": ["焊带", "银浆", "金刚线", "设备",
                                          "胶膜", "玻璃"],
             "bottleneck": False, "weight": 1},
        ],
    },
    "机器人": {
        "keywords": ["机器人", "人形", "减速器", "伺服", "空心杯",
                      "丝杠", "力矩", "灵巧手"],
        "stock_names": ["汇川技术", "绿的谐波", "双环传动", "中大力德",
                         "拓斯达", "埃斯顿", "埃夫特", "禾川科技",
                         "昊志机电", "江苏北人", "步科股份", "伟创电气",
                         "儒竞科技", "五洲新春", "长盛轴承", "力星股份",
                         "鼎智科技", "丰立智能", "通力科技", "夏厦精密",
                         "博实股份", "新松机器人", "智迪科技", "罗博特科"],
        "nodes": [
            {"name": "核心部件", "kw": ["减速器", "伺服", "空心杯", "丝杠",
                                          "力矩", "编码器"],
             "bottleneck": True, "weight": 3},
            {"name": "整机/集成", "kw": ["机器人", "人形", "自动化",
                                           "执行器"],
             "bottleneck": False, "weight": 2},
        ],
    },
    "低空经济": {
        "keywords": ["低空", "飞行汽车", "eVTOL", "无人机", "空管"],
        "stock_names": ["亿航智能", "中无人机", "纵横股份", "莱斯信息",
                         "中信海直", "万丰奥威", "亿嘉和", "观典防务",
                         "航天彩虹", "中直股份", "航天电子", "星网宇达",
                         "华设集团", "深城交", "苏交科"],
        "nodes": [
            {"name": "飞行器", "kw": ["飞行汽车", "eVTOL", "无人机",
                                        "evtol"],
             "bottleneck": True, "weight": 3},
            {"name": "基建/运营", "kw": ["空管", "雷达", "导航",
                                           "低空经济"],
             "bottleneck": False, "weight": 2},
        ],
    },
    "军工": {
        "keywords": ["军工", "航天", "航空", "国防", "特种"],
        "stock_names": ["中航沈飞", "中航西飞", "中国船舶", "中国重工",
                         "中航光电", "航天电器", "中航高科", "航发动力",
                         "中航重机", "航天彩虹", "火炬电子", "振华风光",
                         "睿创微纳", "菲利华", "光启技术", "西部超导",
                         "抚顺特钢", "钢研高纳", "图南股份"],
        "nodes": [
            {"name": "特种材料", "kw": ["特种", "复合", "合金", "碳纤维",
                                          "钛合金", "高温合金"],
             "bottleneck": True, "weight": 3},
            {"name": "电子/系统", "kw": ["电子", "雷达", "导航", "信息",
                                          "相控阵"],
             "bottleneck": False, "weight": 2},
        ],
    },
}


STOCK_TO_CHAIN: dict[str, str] = {}
for chain_name, chain_def in CHAINS.items():
    for stock_name in chain_def.get("stock_names", []):
        STOCK_TO_CHAIN[stock_name] = chain_name


def match_chains(name: str) -> list[tuple[str, str, bool]]:
    results = []

    chain_name = STOCK_TO_CHAIN.get(name)
    if chain_name:
        chain_def = CHAINS[chain_name]
        matched_any_node = False
        for node in chain_def["nodes"]:
            if any(kw.lower() in name.lower() for kw in node["kw"]):
                results.append((chain_name, node["name"], node["bottleneck"]))
                matched_any_node = True
        if not matched_any_node:
            results.append((chain_name, "其他", False))
        return results

    for chain_name, chain_def in CHAINS.items():
        chain_kw = chain_def["keywords"]
        if not any(kw.lower() in name.lower() for kw in chain_kw):
            continue
        matched_any_node = False
        for node in chain_def["nodes"]:
            if any(kw.lower() in name.lower() for kw in node["kw"]):
                results.append((chain_name, node["name"], node["bottleneck"]))
                matched_any_node = True
        if not matched_any_node:
            results.append((chain_name, "其他", False))
    return results


def match_chain_simple(name: str) -> str | None:
    if name in STOCK_TO_CHAIN:
        return STOCK_TO_CHAIN[name]
    for chain_name, chain_def in CHAINS.items():
        if any(kw.lower() in name.lower() for kw in chain_def["keywords"]):
            return chain_name
    return None
