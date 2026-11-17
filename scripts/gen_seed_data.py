#!/usr/bin/env python3
# 广州美食 RAG - 结构化种子数据生成器 (Phase 1a)
# 零第三方依赖, 固定 seed, 可复现. 严格对齐 docs/00-方案规划.md §3.2 的 45 字段 Schema.
# 真实要素: 行政区 / 商圈 / 地铁站与线路 / 粤菜菜名 / GCJ-02 近似坐标.
# 虚构要素: 店名 / 评分 / 人均 / 营业时间 / 评论文字. 全部 synthetic=true.
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

SCHEMA_FIELDS = [
    "shop_id", "name", "aliases", "synthetic", "district", "business_area",
    "address", "geo", "nearest_metro", "parking", "cuisine", "dishes",
    "taste_tags", "spiciness", "sweetness", "oily_level", "price_per_person",
    "price_band", "value_for_money", "scene_tags", "env_tags", "noise_level",
    "private_room", "outdoor_seating", "opening_hours", "is_late_night",
    "is_dim_sum", "queue_min", "reservation", "takeaway", "rating",
    "review_count", "awards", "since_year", "allergens", "avoid_ingredients",
    "dietary_flags", "health_tags", "season_tags", "pros", "cons",
    "service_tags", "source_url", "model_version", "updated_at",
]

DISTRICT_CODE = {
    "荔湾区": "LW", "越秀区": "YX", "天河区": "TH", "海珠区": "HZ",
    "白云区": "BY", "番禺区": "PY", "黄埔区": "HP",
}

# (区, 商圈, 主线路, 主站, 可选副线路, lat, lon) -- 坐标为该商圈中心的 GCJ-02 近似值
AREAS = [
    ("荔湾区", "西华路", "1号线", "陈家祠", None, 23.1290, 113.2450),
    ("荔湾区", "上下九", "1号线", "长寿路", None, 23.1220, 113.2430),
    ("荔湾区", "泮塘", "5号线", "中山八", None, 23.1300, 113.2380),
    ("荔湾区", "沙面", "1号线", "黄沙", None, 23.1190, 113.2420),
    ("越秀区", "北京路", "1号线", "公园前", "2号线", 23.1220, 113.2680),
    ("越秀区", "文明路", "1号线", "农讲所", None, 23.1210, 113.2720),
    ("越秀区", "淘金", "5号线", "淘金", None, 23.1380, 113.2710),
    ("越秀区", "东山口", "1号线", "东山口", "6号线", 23.1280, 113.2810),
    ("天河区", "珠江新城", "3号线", "珠江新城", "5号线", 23.1190, 113.3190),
    ("天河区", "体育西", "1号线", "体育西路", "3号线", 23.1330, 113.3220),
    ("天河区", "岗顶", "3号线", "岗顶", None, 23.1340, 113.3400),
    ("天河区", "林和西", "3号线", "林和西", None, 23.1400, 113.3230),
    ("海珠区", "江南西", "2号线", "江南西", None, 23.0840, 113.2680),
    ("海珠区", "客村", "3号线", "客村", "8号线", 23.0900, 113.3140),
    ("海珠区", "琶洲", "8号线", "琶洲", None, 23.0990, 113.3500),
    ("海珠区", "市二宫", "2号线", "市二宫", None, 23.0960, 113.2700),
    ("白云区", "三元里", "2号线", "三元里", None, 23.1520, 113.2630),
    ("白云区", "西村", "5号线", "西村", None, 23.1480, 113.2520),
    ("番禺区", "市桥", "3号线", "市桥", None, 22.9380, 113.3840),
    ("番禺区", "汉溪长隆", "3号线", "汉溪长隆", "7号线", 22.9900, 113.3750),
    ("黄埔区", "文冲", "5号线", "文冲", None, 23.1100, 113.4740),
    ("黄埔区", "南岗", "13号线", "南岗", None, 23.1140, 113.5080),
]

ALLOWED_ALLERGENS = {"花生", "虾/甲壳类", "麸质", "海鲜", "乳制品", "蛋类", "大豆"}

# 刻意避开广州真实老字号字号(陶陶居/惠如楼/南园/北园等), 全部为虚构组合
NAME_HEADS = [
    "穗香", "粤和", "广记", "金棠", "顺意", "西关", "羊城", "荣记",
    "丰泽", "宝华", "满堂", "盈丰", "老广",
    "福来", "合兴", "明记", "财记", "有记", "新桥", "桥头", "榕荫", "塘边",
]
NAME_TAILS = ["居", "轩", "楼", "馆", "号", "坊", "阁", "斋", "厨", "灶", "铺", "庭"]

# 原型: 菜系/价格/菜品(含过敏原)与环境强绑定, 避免"肠粉店人均300"这类失真数据.
ARCHETYPES = [
    {
        "key": "dimsum", "suffix": "早茶", "cuisine": ["粤菜", "广式点心"],
        "districts": ["荔湾区", "越秀区", "天河区", "海珠区", "白云区", "番禺区"],
        "price": (45, 120), "areas": ["北京路", "西华路", "珠江新城", "淘金", "江南西", "市桥"],
        "dishes": [("水晶虾饺", ["虾/甲壳类", "海鲜"]), ("干蒸烧卖", ["麸质"]),
                   ("叉烧包", ["麸质"]), ("流沙包", ["乳制品", "蛋类", "麸质"]),
                   ("豉汁凤爪", ["花生"]), ("艇仔粥", ["海鲜", "花生", "麸质"])],
        "taste": ["清淡", "鲜甜", "豉油香"], "env": ["茶楼", "热闹", "有转盘"],
        "noise": "吵", "scenes": ["早茶", "家庭聚餐", "带爸妈"],
        "spice": (0, 1), "sweet": (2, 3), "oil": (1, 2),
        "park": 0.35, "room": 0.75, "outdoor": 0.05, "reserve": 0.85, "takeaway": 0.8,
        "hours": [("06:30", "14:30")], "dimsum": True, "late": False, "queue": (10, 30),
        "health": [], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票"], "pros": ["虾饺皮薄馅靓", "茶位够足", "老广味道"],
        "cons": ["周末要等位", "推车点心慢", "大厅较吵"],
        "awards": ["老字号", "必吃榜"], "since": (1985, 2005),
    },
    {
        "key": "changfen", "suffix": "肠粉", "cuisine": ["粤菜", "粥粉面"],
        "districts": ["荔湾区", "越秀区", "海珠区", "白云区"],
        "price": (12, 35), "areas": ["西华路", "上下九", "文明路", "市二宫", "三元里", "西村"],
        "dishes": [("鲜虾肠粉", ["虾/甲壳类", "麸质", "海鲜"]), ("及第粥", ["麸质"]),
                   ("艇仔粥", ["海鲜", "花生"]), ("牛肉肠粉", ["麸质"]), ("斋肠", ["麸质"])],
        "taste": ["清淡", "米香浓郁", "豉油偏甜"], "env": ["街边小店", "烟火气", "需拼桌"],
        "noise": "吵", "scenes": ["早餐", "一人食", "本地街坊"],
        "spice": (0, 0), "sweet": (1, 2), "oil": (0, 1),
        "park": 0.05, "room": 0.0, "outdoor": 0.1, "reserve": 0.0, "takeaway": 0.95,
        "hours": [("06:00", "14:00")], "dimsum": True, "late": False, "queue": (5, 20),
        "health": ["低油"], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": ["香菜"],
        "service": [], "pros": ["肠粉皮薄透亮", "米香浓郁", "平靓正"],
        "cons": ["下午常售罄", "座位少要拼桌", "环境嘈杂"],
        "awards": ["老字号", "必吃榜"], "since": (1990, 2010),
    },
    {
        "key": "siumei", "suffix": "烧腊", "cuisine": ["粤菜", "烧腊"],
        "districts": ["荔湾区", "越秀区", "天河区", "海珠区", "白云区", "番禺区"],
        "price": (22, 55), "areas": ["西华路", "上下九", "岗顶", "江南西", "市桥", "三元里"],
        "dishes": [("深井烧鹅", ["麸质"]), ("蜜汁叉烧", ["麸质"]), ("白切鸡", []),
                   ("豉油鸡", ["麸质"]), ("玫瑰豉油鸡", ["麸质"])],
        "taste": ["咸香", "油润", "肉香"], "env": ["档口", "明档挂味", "出餐快"],
        "noise": "适中", "scenes": ["一人食", "家庭聚餐", "本地街坊"],
        "spice": (0, 0), "sweet": (2, 3), "oil": (2, 3),
        "park": 0.1, "room": 0.0, "outdoor": 0.05, "reserve": 0.0, "takeaway": 0.98,
        "hours": [("10:00", "21:00")], "dimsum": False, "late": False, "queue": (5, 25),
        "health": [], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": [], "pros": ["烧鹅皮脆肉嫩", "斩料够快", "豉油香"],
        "cons": ["偏油腻", "高峰排队", "堂食位少"],
        "awards": ["老字号"], "since": (1988, 2012),
    },
    {
        "key": "chaoshan", "suffix": "牛肉火锅", "cuisine": ["潮汕菜", "火锅"],
        "districts": ["天河区", "海珠区", "番禺区", "越秀区"],
        "price": (80, 180), "areas": ["体育西", "客村", "珠江新城", "汉溪长隆", "岗顶"],
        "dishes": [("手打牛肉丸", ["麸质"]), ("吊龙", []), ("匙柄", []),
                   ("沙茶酱", ["花生", "海鲜"]), ("潮汕粿条", ["麸质"])],
        "taste": ["清鲜", "牛肉原味", "沙茶浓香"], "env": ["热闹", "大圆桌", "明档现切"],
        "noise": "吵", "scenes": ["家庭聚餐", "朋友聚会", "夜宵"],
        "spice": (0, 1), "sweet": (1, 2), "oil": (1, 2),
        "park": 0.6, "room": 0.55, "outdoor": 0.1, "reserve": 0.9, "takeaway": 0.2,
        "hours": [("11:00", "14:00"), ("17:00", "02:00")], "dimsum": False, "late": True, "queue": (15, 40),
        "health": ["低糖"], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票", "可提前订位"], "pros": ["牛肉现切", "汤底清甜", "沙茶够香"],
        "cons": ["沙茶含花生需留意", "排队久", "价格偏高"],
        "awards": ["必吃榜"], "since": (2000, 2018),
    },
    {
        "key": "kejia", "suffix": "客家菜", "cuisine": ["客家菜"],
        "districts": ["越秀区", "天河区", "海珠区", "白云区", "番禺区"],
        "price": (50, 110), "areas": ["淘金", "东山口", "岗顶", "琶洲", "文冲", "市桥"],
        "dishes": [("盐焗鸡", []), ("酿豆腐", ["麸质"]), ("梅菜扣肉", []),
                   ("客家盆菜", ["海鲜"]), ("腌面", ["麸质"])],
        "taste": ["咸香", "家常", "浓郁"], "env": ["家常菜馆", "大桌", "包间安静"],
        "noise": "适中", "scenes": ["家庭聚餐", "带爸妈", "商务宴请"],
        "spice": (0, 1), "sweet": (1, 2), "oil": (2, 3),
        "park": 0.7, "room": 0.8, "outdoor": 0.05, "reserve": 0.85, "takeaway": 0.6,
        "hours": [("11:00", "21:30")], "dimsum": False, "late": False, "queue": (5, 20),
        "health": [], "season": ["全年", "秋冬"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票", "可提前订位"], "pros": ["盐焗鸡入味", "分量足", "家常味"],
        "cons": ["口味偏咸", "位置稍偏", "上菜慢"],
        "awards": ["老字号"], "since": (1992, 2010),
    },
    {
        "key": "seafood", "suffix": "海鲜酒家", "cuisine": ["粤菜", "海鲜"],
        "districts": ["天河区", "越秀区", "海珠区", "黄埔区", "番禺区"],
        "price": (150, 380), "areas": ["珠江新城", "淘金", "琶洲", "南岗", "汉溪长隆"],
        "dishes": [("清蒸石斑", ["海鲜"]), ("白灼虾", ["虾/甲壳类", "海鲜"]),
                   ("姜葱炒蟹", ["海鲜", "虾/甲壳类"]), ("椒盐濑尿虾", ["海鲜", "虾/甲壳类", "麸质"]),
                   ("蒜蓉粉丝蒸扇贝", ["海鲜", "麸质"])],
        "taste": ["清鲜", "原汁原味", "豉油香"], "env": ["高档酒楼", "包间", "安静"],
        "noise": "安静", "scenes": ["商务宴请", "家庭聚餐", "约会"],
        "spice": (0, 0), "sweet": (1, 2), "oil": (1, 2),
        "park": 0.95, "room": 1.0, "outdoor": 0.1, "reserve": 1.0, "takeaway": 0.3,
        "hours": [("11:00", "22:00")], "dimsum": False, "late": False, "queue": (0, 15),
        "health": ["低糖", "低油"], "season": ["全年", "秋季"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票", "可提前订位"], "pros": ["海鲜新鲜", "包间安静", "服务周到"],
        "cons": ["价格偏高", "点菜需问价", "周末需提前订"],
        "awards": ["黑珍珠一钻", "米其林必比登", "必吃榜"], "since": (1998, 2015),
    },
    {
        "key": "sichuan", "suffix": "川菜", "cuisine": ["川菜"],
        "districts": ["天河区", "越秀区", "海珠区", "白云区", "黄埔区"],
        "price": (45, 100), "areas": ["岗顶", "北京路", "江南西", "三元里", "文冲"],
        "dishes": [("水煮鱼", ["海鲜", "花生"]), ("麻婆豆腐", ["麸质"]),
                   ("宫保鸡丁", ["花生"]), ("回锅肉", []), ("口水鸡", ["花生", "麸质"])],
        "taste": ["麻辣", "重口", "下饭"], "env": ["热闹", "红油香气", "大桌"],
        "noise": "吵", "scenes": ["朋友聚会", "一人食", "夜宵"],
        "spice": (3, 4), "sweet": (1, 2), "oil": (3, 4),
        "park": 0.35, "room": 0.3, "outdoor": 0.05, "reserve": 0.6, "takeaway": 0.9,
        "hours": [("11:00", "14:00"), ("17:00", "01:00")], "dimsum": False, "late": True, "queue": (10, 30),
        "health": [], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票"], "pros": ["麻辣够劲", "下饭", "分量足"],
        "cons": ["油偏重", "辣度不易调", "等位较久"],
        "awards": ["必吃榜"], "since": (2002, 2018),
    },
    {
        "key": "hunan", "suffix": "湘菜", "cuisine": ["湘菜"],
        "districts": ["天河区", "海珠区", "白云区", "黄埔区", "番禺区"],
        "price": (45, 95), "areas": ["珠江新城", "客村", "西村", "南岗", "市桥"],
        "dishes": [("剁椒鱼头", ["海鲜"]), ("小炒黄牛肉", []), ("农家腊肉", []),
                   ("干锅田鸡", []), ("擂辣椒皮蛋", ["蛋类"])],
        "taste": ["香辣", "镬气足", "下饭"], "env": ["家常湘菜馆", "热闹", "明厨"],
        "noise": "吵", "scenes": ["朋友聚会", "家庭聚餐", "一人食"],
        "spice": (3, 4), "sweet": (1, 1), "oil": (2, 3),
        "park": 0.4, "room": 0.35, "outdoor": 0.05, "reserve": 0.65, "takeaway": 0.85,
        "hours": [("11:00", "21:30")], "dimsum": False, "late": False, "queue": (8, 25),
        "health": [], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": ["可开发票"], "pros": ["香辣下饭", "锅气足", "分量实在"],
        "cons": ["辣度偏高", "上菜慢", "环境一般"],
        "awards": ["必吃榜"], "since": (2005, 2019),
    },
    {
        "key": "halal", "suffix": "清真", "cuisine": ["清真菜", "西北菜"],
        "districts": ["越秀区", "天河区", "白云区", "海珠区"],
        "price": (28, 75), "areas": ["北京路", "淘金", "三元里", "客村"],
        "dishes": [("兰州牛肉面", ["麸质", "花生"]), ("手抓羊肉", []),
                   ("烤羊肉串", []), ("牛肉饼", ["麸质"]), ("羊肉泡馍", ["麸质"])],
        "taste": ["清鲜", "香料味", "汤浓"], "env": ["清真小店", "简洁", "出餐快"],
        "noise": "适中", "scenes": ["一人食", "家庭聚餐", "本地街坊"],
        "spice": (1, 2), "sweet": (0, 1), "oil": (1, 2),
        "park": 0.15, "room": 0.1, "outdoor": 0.05, "reserve": 0.2, "takeaway": 0.95,
        "hours": [("10:00", "22:00")], "dimsum": False, "late": False, "queue": (5, 20),
        "health": ["低糖"], "season": ["全年"],
        "dietary": ["清真", "不含猪肉"], "avoid_base": ["猪肉", "酒精"],
        "service": ["可开发票"], "pros": ["汤头清", "羊肉嫩", "分量足"],
        "cons": ["店面小", "高峰排队", "停车难"],
        "awards": [], "since": (2000, 2016),
    },
    {
        "key": "vegan", "suffix": "素食", "cuisine": ["素食", "粤菜"],
        "districts": ["越秀区", "天河区", "海珠区", "荔湾区"],
        "price": (60, 150), "areas": ["东山口", "珠江新城", "江南西", "沙面"],
        "dishes": [("罗汉斋", ["麸质", "大豆"]), ("素鹅", ["麸质", "大豆"]),
                   ("杂菌汤", []), ("腐竹时蔬", ["大豆"]), ("南瓜粥", [])],
        "taste": ["清淡", "食材本味", "精致"], "env": ["素雅", "安静", "适合拍照"],
        "noise": "安静", "scenes": ["约会", "一人食", "带爸妈"],
        "spice": (0, 0), "sweet": (1, 2), "oil": (0, 1),
        "park": 0.45, "room": 0.35, "outdoor": 0.3, "reserve": 0.8, "takeaway": 0.7,
        "hours": [("11:00", "21:00")], "dimsum": False, "late": False, "queue": (0, 10),
        "health": ["低油", "低糖", "低嘌呤", "控卡"], "season": ["全年", "春季"],
        "dietary": ["全素", "不含猪肉"], "avoid_base": ["猪肉", "海鲜", "蛋类", "乳制品"],
        "service": ["可开发票", "可提前订位"], "pros": ["食材新鲜", "摆盘精致", "清爽"],
        "cons": ["价格偏高", "味道偏淡", "位置难找"],
        "awards": ["必吃榜"], "since": (2008, 2019),
    },
    {
        "key": "night", "suffix": "大排档", "cuisine": ["粤菜", "宵夜"],
        "districts": ["荔湾区", "海珠区", "天河区", "番禺区", "黄埔区"],
        "price": (40, 95), "areas": ["上下九", "江南西", "岗顶", "市桥", "文冲"],
        "dishes": [("啫啫煲", ["海鲜", "麸质"]), ("干炒牛河", ["麸质"]),
                   ("蒜蓉生蚝", ["海鲜"]), ("椒盐皮皮虾", ["海鲜", "虾/甲壳类"])],
        "taste": ["镬气足", "咸鲜", "重口"], "env": ["大排档", "露天位", "烟火气"],
        "noise": "吵", "scenes": ["夜宵", "朋友聚会", "本地街坊"],
        "spice": (1, 2), "sweet": (1, 2), "oil": (2, 3),
        "park": 0.25, "room": 0.0, "outdoor": 0.9, "reserve": 0.1, "takeaway": 0.9,
        "hours": [("18:00", "02:00")], "dimsum": False, "late": True, "queue": (10, 35),
        "health": [], "season": ["全年", "夏季"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": [],
        "service": [], "pros": ["镬气足", "夜宵够味", "氛围热闹"],
        "cons": ["油烟较大", "环境嘈杂", "收摊较早"],
        "awards": ["必吃榜"], "since": (2003, 2017),
    },
    {
        "key": "noodle", "suffix": "面家", "cuisine": ["粤菜", "粥粉面"],
        "districts": ["荔湾区", "越秀区", "海珠区", "白云区", "天河区"],
        "price": (15, 42), "areas": ["西华路", "文明路", "市二宫", "西村", "林和西"],
        "dishes": [("云吞面", ["麸质", "海鲜"]), ("牛腩粉", ["麸质"]),
                   ("艇仔粥", ["海鲜", "花生"]), ("鱼蛋粉", ["麸质", "海鲜"])],
        "taste": ["汤鲜", "清爽", "豉油香"], "env": ["小档", "翻台快", "街坊生意"],
        "noise": "适中", "scenes": ["一人食", "早餐", "本地街坊"],
        "spice": (0, 0), "sweet": (1, 2), "oil": (0, 1),
        "park": 0.05, "room": 0.0, "outdoor": 0.1, "reserve": 0.0, "takeaway": 0.95,
        "hours": [("07:00", "21:00")], "dimsum": False, "late": False, "queue": (5, 18),
        "health": ["低油"], "season": ["全年"],
        "dietary": ["含猪肉", "非清真"], "avoid_base": ["香菜"],
        "service": [], "pros": ["汤底鲜", "出餐快", "一人食友好"],
        "cons": ["座位少", "汤偏咸", "高峰拼桌"],
        "awards": ["老字号"], "since": (1995, 2012),
    },
]

# 中心商圈溢价; 老城区平价
PRICE_FACTOR = {"珠江新城": 1.25, "体育西": 1.2, "北京路": 1.15, "淘金": 1.15,
                "西华路": 0.9, "上下九": 0.9, "文明路": 0.9, "泮塘": 0.9}


def price_band(v: int) -> str:
    if v <= 30:
        return "0-30"
    if v <= 60:
        return "31-60"
    if v <= 100:
        return "61-100"
    if v <= 200:
        return "101-200"
    return "200+"


def clamp(v: int, lo: int = 0, hi: int = 5) -> int:
    return max(lo, min(hi, v))


def make_shop(rng: random.Random, arch: dict, area: tuple, seq: int) -> dict:
    district, business, line, station, line2, lat, lon = area
    idx = seq + 1

    head = rng.choice(NAME_HEADS)
    tail = rng.choice(NAME_TAILS)
    name = f"{head}{tail}·{business}{arch['suffix']}"
    aliases = [f"{head}{tail}", f"{business}{arch['suffix']}"]

    price = int(round(rng.randint(*arch["price"]) * PRICE_FACTOR.get(business, 1.0)))
    dishes_pool = arch["dishes"]
    k = min(len(dishes_pool), rng.randint(2, 4))
    chosen = rng.sample(dishes_pool, k)
    dishes = [d[0] for d in chosen]
    allergens = sorted({a for d in chosen for a in d[1] if a in ALLOWED_ALLERGENS})

    metro = [{"line": line, "station": station, "walk_min": rng.randint(3, 12)}]
    if line2:
        metro.append({"line": line2, "station": station, "walk_min": metro[0]["walk_min"]})

    hours = [{"open": o, "close": c, "days": [1, 2, 3, 4, 5, 6, 7]} for o, c in arch["hours"]]

    awards = []
    if rng.random() < 0.45:
        awards = rng.sample(arch["awards"], k=rng.randint(1, min(2, len(arch["awards"])))) if arch["awards"] else []
    since = rng.randint(*arch["since"])
    if "老字号" in awards:
        since = min(since, 1995)

    avoid = list(arch["avoid_base"])
    if not arch["avoid_base"] and rng.random() < 0.25:
        avoid.append("香菜")

    health = list(arch["health"])
    oil = clamp(rng.randint(*arch["oil"]))
    if oil <= 1 and "低油" not in health:
        health.append("低油")

    pros = rng.sample(arch["pros"], k=min(len(arch["pros"]), 2))
    cons = rng.sample(arch["cons"], k=min(len(arch["cons"]), 2))
    service = list(arch["service"])
    if rng.random() < 0.5 and "可开发票" not in service:
        service.append("可开发票")

    return {
        "shop_id": f"GZ-{DISTRICT_CODE[district]}-{idx:04d}",
        "name": name,
        "aliases": aliases,
        "synthetic": True,
        "district": district,
        "business_area": business,
        "address": f"{district}{business}{rng.choice(['内街', '主街', '横街', '沿江'])} xxx 号",
        "geo": {"lat": round(lat + rng.uniform(-0.0025, 0.0025), 4),
                "lon": round(lon + rng.uniform(-0.0025, 0.0025), 4)},
        "nearest_metro": metro,
        "parking": rng.random() < arch["park"],
        "cuisine": list(arch["cuisine"]),
        "dishes": dishes,
        "taste_tags": list(arch["taste"]),
        "spiciness": clamp(rng.randint(*arch["spice"])),
        "sweetness": clamp(rng.randint(*arch["sweet"])),
        "oily_level": oil,
        "price_per_person": price,
        "price_band": price_band(price),
        "value_for_money": round(rng.uniform(3.6, 4.9), 1),
        "scene_tags": list(arch["scenes"]),
        "env_tags": list(arch["env"]),
        "noise_level": arch["noise"],
        "private_room": rng.random() < arch["room"],
        "outdoor_seating": rng.random() < arch["outdoor"],
        "opening_hours": hours,
        "is_late_night": arch["late"],
        "is_dim_sum": arch["dimsum"],
        "queue_min": rng.randint(*arch["queue"]),
        "reservation": rng.random() < arch["reserve"],
        "takeaway": rng.random() < arch["takeaway"],
        "rating": round(rng.uniform(3.7, 4.9), 1),
        "review_count": rng.randint(60, 4200),
        "awards": awards,
        "since_year": since,
        "allergens": allergens,
        "avoid_ingredients": avoid,
        "dietary_flags": list(arch["dietary"]),
        "health_tags": health,
        "season_tags": list(arch["season"]),
        "pros": pros,
        "cons": cons,
        "service_tags": service,
        "source_url": None,
        "model_version": "bge-m3@2026-11",
        "updated_at": "2026-11-16T00:00:00+08:00",
    }


def validate(shops: list[dict]) -> tuple[list[str], dict]:
    errors = []
    for i, s in enumerate(shops):
        tag = s.get("shop_id", f"#{i}")
        keys = set(s.keys())
        if keys != set(SCHEMA_FIELDS):
            errors.append(f"{tag}: 字段不匹配 缺={set(SCHEMA_FIELDS)-keys} 多={keys-set(SCHEMA_FIELDS)}")
            continue
        if s["district"] not in DISTRICT_CODE:
            errors.append(f"{tag}: district 非法 {s['district']}")
        if s["noise_level"] not in {"安静", "适中", "吵"}:
            errors.append(f"{tag}: noise_level 非法")
        if s["price_band"] != price_band(s["price_per_person"]):
            errors.append(f"{tag}: price_band 与人均不一致")
        if not (3.5 <= s["rating"] <= 5.0):
            errors.append(f"{tag}: rating 越界")
        if not (22.7 <= s["geo"]["lat"] <= 23.5 and 113.0 <= s["geo"]["lon"] <= 113.8):
            errors.append(f"{tag}: geo 不在广州范围")
        if not all(a in ALLOWED_ALLERGENS for a in s["allergens"]):
            errors.append(f"{tag}: allergens 含非法值")
        if not all(isinstance(v, bool) for v in
                   [s["synthetic"], s["parking"], s["private_room"], s["outdoor_seating"],
                    s["is_late_night"], s["is_dim_sum"], s["reservation"], s["takeaway"]]):
            errors.append(f"{tag}: 布尔字段类型错误")
        for f in ["spiciness", "sweetness", "oily_level"]:
            if not (0 <= s[f] <= 5):
                errors.append(f"{tag}: {f} 越界")
        if not s["synthetic"]:
            errors.append(f"{tag}: synthetic 必须为 true")
        if s["is_late_night"] and not any(h["close"] in {"01:00", "02:00", "00:00"} for h in s["opening_hours"]):
            errors.append(f"{tag}: is_late_night 与营业时段不一致")
        if s["is_dim_sum"] and not any(h["open"] <= "07:00" for h in s["opening_hours"]):
            errors.append(f"{tag}: is_dim_sum 与营业时段不一致")
        if "老字号" in s["awards"] and s["since_year"] > 1995:
            errors.append(f"{tag}: 老字号但开业年份过晚")
        if "全素" in s["dietary_flags"] and set(s["allergens"]) & {"虾/甲壳类", "海鲜"}:
            errors.append(f"{tag}: 全素却含海鲜过敏原")

    ids = [s["shop_id"] for s in shops]
    names = [s["name"] for s in shops]
    if len(set(ids)) != len(ids):
        errors.append("shop_id 重复")
    if len(set(names)) != len(names):
        errors.append("name 重复")

    stats = {
        "总数": len(shops),
        "按区": dict(Counter(s["district"] for s in shops)),
        "按菜系": dict(Counter(s["cuisine"][0] for s in shops)),
        "按价格档": dict(Counter(s["price_band"] for s in shops)),
        "含过敏原": dict(Counter(a for s in shops for a in s["allergens"])),
        "硬约束字段非空率": {
            f: round(sum(1 for s in shops if s[f]) / len(shops), 3)
            for f in ["district", "business_area", "geo", "nearest_metro", "price_per_person",
                      "allergens", "avoid_ingredients", "dietary_flags", "opening_hours"]
        },
        "字段数": len(SCHEMA_FIELDS),
    }
    return errors, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30, help="生成店数, 默认 30 (确认后再放大到 300)")
    ap.add_argument("--seed", type=int, default=20261116, help="随机种子, 默认 20261116")
    ap.add_argument("--out", default="data/raw/shops.jsonl", help="输出 JSONL 路径")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    base_quota = {"荔湾区": 5, "越秀区": 5, "天河区": 5, "海珠区": 4,
                  "白云区": 3, "番禺区": 4, "黄埔区": 4}
    base_total = sum(base_quota.values())  # 30
    base_plan = []
    for d, n in base_quota.items():
        base_plan.extend([d] * n)
    # 基础分布重复扩展到 count: 300 = 10 x 30, 各区比例保持不变
    plan = (base_plan * (args.count // base_total + 1))[:args.count]
    rng.shuffle(plan)

    # 前 12 家保证每种原型至少出现一次, 其余按适配区随机

    shops = []
    used_names = set()
    for i, dist in enumerate(plan):
        candidates = [a for a in ARCHETYPES if dist in a["districts"]]
        if i < len(ARCHETYPES):
            wanted = ARCHETYPES[i]["key"]
            arch = next((a for a in candidates if a["key"] == wanted), None) or rng.choice(candidates)
        else:
            arch = rng.choice(candidates)
        pool = [a for a in AREAS if a[0] == dist and a[1] in arch["areas"]]
        if not pool:
            pool = [a for a in AREAS if a[0] == dist]
        area = rng.choice(pool)

        shop = make_shop(rng, arch, area, i)
        while shop["name"] in used_names:
            shop = make_shop(rng, arch, area, i + 100)
        used_names.add(shop["name"])
        shop["shop_id"] = f"GZ-{DISTRICT_CODE[dist]}-{i+1:04d}"
        shops.append(shop)

    errors, stats = validate(shops)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s in shops:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"[生成] {out.resolve()}  count={len(shops)}  seed={args.seed}")
    print(f"[校验] 字段数={stats['字段数']}  错误数={len(errors)}")
    if errors:
        for e in errors[:30]:
            print("  ERROR:", e)
    print("[分布] 按区:", stats["按区"])
    print("[分布] 按菜系:", stats["按菜系"])
    print("[分布] 按价格档:", stats["按价格档"])
    print("[分布] 含过敏原:", stats["含过敏原"])
    print("[检查] 硬约束字段非空率:", stats["硬约束字段非空率"])
    print("[样例] 前 2 条:")
    for s in shops[:2]:
        print(json.dumps(s, ensure_ascii=False, indent=2))

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
