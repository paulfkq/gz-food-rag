"""检索层第 1 环：槽位解析（Query Understanding）。

一句话职责：把用户随口说的中文，翻译成一个【结构化的条件对象】，
后面的过滤器、召回、融合都只认这个对象，不再去猜用户原话。

为什么必须有这一步（新手最容易忽略）：
    用户说 "人均30以内的肠粉，别太吵，不要香菜"
    如果直接把这句话丢给向量检索，模型只能"模糊感觉"到这些条件，
    "30以内"会被稀释成"便宜点"，"不要香菜"甚至可能被理解成"想要香菜"。
    所以必须先把话拆成两类：
      - 硬约束(hard)：必须满足，否则这家店不能出现（价格上限/行政区/过敏原/忌口…）
      - 软偏好(soft)：只影响排序，不满足也能接受（安静/氛围/口味…）
    硬约束会被编译成 Qdrant 的精确过滤条件；软偏好进入语义召回。

本文件提供两种解析器：
    1. RuleSlotParser（默认，零依赖零网络）：词典 + 正则。可跑通、可单测、可解释。
    2. llm_parse（可选，需配 OPENAI_API_KEY）：function calling，覆盖更复杂的口语。
    两者产出同一个 SearchSlots 对象，上层无感——这就是"可插拔"的好处。

面试讲点：为什么默认规则版而不是一上来就 LLM？
    - 硬约束错一次就是事故（把含花生的店推荐给过敏用户），规则版可穷举、可单测、100%确定；
    - LLM 只负责"规则覆盖不到的长尾口语"，且输出必须过 Pydantic 风格校验；
    - 这是"关键路径用确定性代码，非关键路径用 LLM"的典型分层。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import config

# ---------------------------------------------------------------------------
# 1. 槽位数据结构（就是方案里的 SearchSlots，这里用 dataclass 实现，零依赖）
# ---------------------------------------------------------------------------

@dataclass
class SearchSlots:
    """一次查询拆出来的所有条件。字段含义见每个注释。"""
    # ===== 硬约束：进 Qdrant pre-filter，必须 100% 满足 =====
    district: str | None = None              # 行政区，如 "越秀区"
    business_area: str | None = None         # 商圈，如 "西华路"
    price_max: int | None = None             # 人均上限，"人均30以内" -> 30
    price_min: int | None = None             # 人均下限
    exclude_allergens: list[str] = field(default_factory=list)   # 过敏原黑名单：店里【含有】这些 = 排除
    require_avoid: list[str] = field(default_factory=list)       # 忌口白名单：要求店【明确标注不使用】
    cuisines: list[str] = field(default_factory=list)            # 菜系，如 ["粤菜"]
    dietary_flags: list[str] = field(default_factory=list)       # 饮食规范，如 ["清真","全素"]
    max_walk_min: int | None = None          # 地铁步行时长上限
    metro_lines: list[str] = field(default_factory=list)         # 指定地铁线
    noise_level: str | None = None           # 硬噪声要求，"必须安静" -> "安静"
    private_room: bool = False               # 要包间
    outdoor_seating: bool = False            # 要户外/露天位
    is_late_night: bool = False              # 要夜宵/深夜营业
    is_dim_sum: bool = False                 # 要早茶
    open_now: bool = False                   # 现在正在营业（按时段粗判）

    # ===== 软偏好：只影响排序/语义召回，不满足也能接受 =====
    soft: list[str] = field(default_factory=list)   # 如 ["安静","性价比高"]

    # ===== 元信息 =====
    sort_by: str = "综合"                      # 综合/评分/人均/距离
    conflicts: list[str] = field(default_factory=list)  # 检测到的冲突
    need_clarify: bool = False
    clarify_question: str | None = None
    parser: str = "rule"                       # 由哪个解析器产出，写进 trace

    def has_hard(self) -> bool:
        """是否至少有一个硬约束（决定要不要加 Qdrant filter）。"""
        return any([
            self.district, self.business_area, self.price_max is not None,
            self.price_min is not None, self.exclude_allergens, self.require_avoid,
            self.cuisines, self.dietary_flags, self.max_walk_min is not None,
            self.metro_lines, self.noise_level, self.private_room,
            self.outdoor_seating, self.is_late_night, self.is_dim_sum, self.open_now,
        ])

    def summary(self) -> str:
        parts = []
        if self.district: parts.append(f"区={self.district}")
        if self.business_area: parts.append(f"商圈={self.business_area}")
        if self.price_max is not None: parts.append(f"人均≤{self.price_max}")
        if self.price_min is not None: parts.append(f"人均≥{self.price_min}")
        if self.cuisines: parts.append(f"菜系={'/'.join(self.cuisines)}")
        if self.exclude_allergens: parts.append(f"排除过敏原={'/'.join(self.exclude_allergens)}")
        if self.require_avoid: parts.append(f"要求不含={'/'.join(self.require_avoid)}")
        if self.dietary_flags: parts.append(f"规范={'/'.join(self.dietary_flags)}")
        if self.max_walk_min is not None: parts.append(f"地铁步行≤{self.max_walk_min}min")
        if self.metro_lines: parts.append(f"地铁线={'/'.join(self.metro_lines)}")
        if self.noise_level: parts.append(f"噪声={self.noise_level}")
        if self.private_room: parts.append("要包间")
        if self.outdoor_seating: parts.append("要户外位")
        if self.is_late_night: parts.append("要夜宵")
        if self.is_dim_sum: parts.append("要早茶")
        if self.open_now: parts.append("现在营业")
        if self.sort_by != "综合": parts.append(f"排序={self.sort_by}")
        if self.soft: parts.append(f"软偏好={'/'.join(self.soft)}")
        return " | ".join(parts) or "无条件"


# ---------------------------------------------------------------------------
# 2. 词典（新词加这里即可扩展，不用改逻辑）
# ---------------------------------------------------------------------------

# 过敏原：这些词出现在"不要/过敏"语境时 -> 排除【含有】它的店
ALLERGEN_WORDS = ["麸质", "海鲜", "花生", "虾", "甲壳", "蛋", "大豆", "乳制品", "牛奶", "奶"]
ALLERGEN_CANON = {  # 口语 -> 数据里的标准词
    "虾": "虾/甲壳类", "甲壳": "虾/甲壳类", "虾/甲壳类": "虾/甲壳类",
    "奶": "乳制品", "牛奶": "乳制品", "乳制品": "乳制品",
    "蛋": "蛋类", "蛋类": "蛋类", "麸质": "麸质", "海鲜": "海鲜",
    "花生": "花生", "大豆": "大豆",
}

# 忌口食材：这些词出现在否定语境时 -> 要求店【明确标注不使用】
AVOID_WORDS = ["香菜", "猪肉", "酒精", "酒", "葱", "辣"]
AVOID_CANON = {"酒": "酒精", "猪肉": "猪肉", "香菜": "香菜", "酒精": "酒精", "葱": "葱"}

# 粤语/口语同义词 -> 标准软偏好词（方案 §4.6 静态词典）
SYNONYMS = {
    "抵食": "性价比高", "平靓正": "性价比高", "便宜": "性价比高",
    "镬气": "镬气足", "有镬气": "镬气足",
    "唔好辣": "不辣", "不要太辣": "不辣", "别太辣": "不辣",
    "老广": "本地街坊", "街坊": "本地街坊",
    "安静": "安静", "别太吵": "安静", "不要太吵": "安静", "不吵": "安静",
    "适合拍照": "适合拍照", "出片": "适合拍照",
    "约会": "约会", "带爸妈": "带爸妈", "家庭": "家庭聚餐",
    "一人": "一人食", "一个人": "一人食", "商务": "商务宴请",
    "早茶": "早茶", "宵夜": "夜宵", "夜宵": "夜宵",
    "停车": "有停车位", "包间": "包间", "露天": "露天位",
}

# 常见广州商圈（数据里出现频次高的），用于从 query 里识别商圈
BUSINESS_AREAS = [
    "珠江新城", "体育西", "体育西路", "西华路", "上下九", "北京路", "文明路",
    "江南西", "东山口", "淘金", "岗顶", "林和西", "客村", "三元里", "西村",
    "沙面", "市二宫", "琶洲", "汉溪长隆", "市桥", "南岗", "文冲",
]
# "体育西路" 归到数据里的 "体育西"
BA_CANON = {"体育西路": "体育西"}

METRO_LINES = ["1号线", "2号线", "3号线", "4号线", "5号线", "6号线", "8号线",
               "广佛线", "APM线", "21号线"]


# ---------------------------------------------------------------------------
# 3. 规则解析器（默认）
# ---------------------------------------------------------------------------

def _detect_conflicts(s: SearchSlots, text: str) -> None:
    """冲突检测：规则版与 LLM 版【共用】，原地修改 s（新手向重点）。

    为什么必须抽成公共函数（实测踩坑）：
        原先这段逻辑只写在 RuleSlotParser.parse() 里。默认优先走 LLM 槽位解析后，
        同一句"人均30米其林"：规则版 need_clarify=True（正确追问），
        LLM 版却是 False —— 它把"米其林"归到软偏好，于是检索照常返回店、
        LLM 照常生成答案，用户期望的"二次确认"完全失效。

    这是一个通用教训：**跨路径必须一致的确定性规则，不能只挂在某一条分支上**。
    凡是"检测到 X 就不检索/不生成"这类硬规则，必须提到公共层，
    规则版和 LLM 版都过一遍，否则任何一条新分支都可能静默绕过它。

    参数：
        s    - 已解析好的 SearchSlots（LLM 版需先做白名单归一化）
        text - 原始 query（高端词/裸价格检测要看原文，不能只看槽位）
    """
    s.conflicts = []

    # 冲突 1：商圈与行政区不匹配（如"越秀区 + 珠江新城"）
    if s.district and s.business_area:
        ba_district = {"珠江新城": "天河区", "体育西": "天河区", "西华路": "荔湾区",
                       "上下九": "荔湾区", "江南西": "海珠区", "东山口": "越秀区",
                       "市桥": "番禺区", "南岗": "黄埔区", "文冲": "黄埔区"}
        if s.business_area in ba_district and ba_district[s.business_area] != s.district:
            s.conflicts.append(
                f"{s.business_area}属于{ba_district[s.business_area]}，与{s.district}冲突")

    # 冲突 2：全素与荤菜/火锅菜系
    if "全素" in s.dietary_flags and s.cuisines and any(
            c in s.cuisines for c in ["烧腊", "海鲜", "火锅", "川菜"]):
        s.conflicts.append("全素与荤菜/火锅菜系冲突")

    # 冲突 3：低预算 + 高端榜单（方案 §4.3 的经典例子）
    # 价格兜底：LLM 偶尔漏提 price_max，这里从原文再提一次，避免冲突漏检。
    price_max = s.price_max
    if price_max is None:
        m = re.search(r"(?:人均|价钱|预算)\s*(\d{1,4})", text)
        if not m:
            m = re.search(r"(\d{1,4})\s*(?:元|块)", text)
        if m:
            price_max = int(m.group(1))
    luxury = [w for w in ["米其林", "黑珍珠", "高端", "五星"] if w in text]
    if luxury and price_max is not None and price_max < 100:
        s.conflicts.append(f"{'/'.join(luxury)}(人均通常>¥100)与人均≤¥{price_max}冲突")

    if s.conflicts:
        s.need_clarify = True
        s.clarify_question = "；".join(s.conflicts) + "，要改哪个条件？"


class RuleSlotParser:
    """词典 + 正则的确定性槽位解析。

    设计原则：宁可漏，不可错。识别不出来的字段一律留 None，
    交给语义召回去兜底，绝不硬猜（猜错硬约束 = 推荐了不该推荐的店）。
    """

    def parse(self, query: str) -> SearchSlots:
        s = SearchSlots()
        text = query.strip()

        # --- 3.1 价格：人均80以内 / 30以下 / 50-100 / 100左右 ---
        # 注意顺序：先匹配区间，再匹配上限，避免 "50-100" 被 "50" 抢走
        m = re.search(r"人均?\s*(\d+)\s*[-~到至]\s*(\d+)", text)
        if m:
            s.price_min, s.price_max = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"(?:人均|价钱|预算)\s*(\d+)\s*(?:元|块)?\s*(?:以内|以下|之内|以下|左右|上下)", text)
            if not m:
                m = re.search(r"(\d+)\s*(?:元|块)?\s*(?:以内|以下|之内)", text)
            if m:
                s.price_max = int(m.group(1))
            else:
                # 裸写 "人均30" / "30块的粤菜" 也当作上限（面试场景常见口语）
                m = re.search(r"人均\s*(\d{1,4})", text)
                if not m:
                    m = re.search(r"(?:大约|大概|预算)?\s*(\d{1,4})\s*(?:元|块)", text)
                if m:
                    s.price_max = int(m.group(1))

        # --- 3.2 行政区 / 商圈 ---
        for d in config.DISTRICTS:
            if d in text or d.replace("区", "") in text and f"{d.replace('区','')}区" in text:
                s.district = d
                break
        for ba in BUSINESS_AREAS:
            if ba in text:
                s.business_area = BA_CANON.get(ba, ba)
                break

        # --- 3.3 菜系 ---
        for c in config.CUISINES:
            if c in text:
                s.cuisines.append(c)

        # --- 3.4 饮食规范（优先级高于普通词，"清真"和"素食"） ---
        for word, canon in config.DIETARY_FLAGS.items():
            if word in text and canon not in s.dietary_flags:
                s.dietary_flags.append(canon)

        # --- 3.5 过敏原 / 忌口：处理否定词（这是红线，最容易出错） ---
        s.exclude_allergens, s.require_avoid = self._parse_negatives(text)

        # --- 3.6 地铁线与步行时长 ---
        for line in METRO_LINES:
            if line in text:
                s.metro_lines.append(line)
        m = re.search(r"(?:地铁|走路|步行)?\s*(\d+)\s*分钟(?:内|以内|之内)?", text)
        if m:
            s.max_walk_min = int(m.group(1))

        # --- 3.7 硬性的设施/时段要求 ---
        if any(k in text for k in ["必须安静", "要包间", "有包间", "独立包间"]): s.private_room = "包间" in text
        if "包间" in text: s.private_room = True
        if any(k in text for k in ["露天", "户外位", "室外"]): s.outdoor_seating = True
        if any(k in text for k in ["夜宵", "宵夜", "深夜", "凌晨"]): s.is_late_night = True
        if "早茶" in text: s.is_dim_sum = True
        if any(k in text for k in ["现在营业", "还开着", "现在开着"]): s.open_now = True

        # "必须安静"才算硬约束；"别太吵"只算软偏好
        if "必须安静" in text:
            s.noise_level = "安静"
        else:
            for k in ["别太吵", "不要太吵", "不吵", "安静"]:
                if k in text:
                    s.noise_level = None  # 明确不设硬约束
                    break

        # --- 3.8 软偏好（同义词归一） ---
        for word, canon in SYNONYMS.items():
            if word in text and canon not in s.soft:
                s.soft.append(canon)
        # 价格低但没写"以内"（如"便宜点"）只做软偏好，不做硬上限
        if "便宜" in text and s.price_max is None:
            s.sort_by = "人均"

        # --- 3.9 排序意图 ---
        if any(k in text for k in ["最近", "离我近", "附近最近"]): s.sort_by = "距离"
        elif any(k in text for k in ["评分高", "评分最高", "最好吃", "高分"]): s.sort_by = "评分"

        # --- 3.10 冲突检测（公共函数：规则版 / LLM 版共用，防单条分支绕过） ---
        _detect_conflicts(s, text)

        s.parser = "rule"
        return s

    def _parse_negatives(self, text: str) -> tuple[list[str], list[str]]:
        """解析否定词：不要/别/忌/过敏/不吃 后面的食材。

        两种落点（数据字典里反复强调的三分语义）：
          - 过敏原词 -> exclude_allergens：店【含有】就排除（must_not）
          - 忌口食材 -> require_avoid：要求店【明确标注不使用】（must）
        例："不要香菜和花生"
            -> require_avoid=["香菜"], exclude_allergens=["花生"]
        """
        exclude, require = [], []
        neg_pattern = r"(?:不要|别要|别放|不加|不吃|忌|过敏|避开|不能有|不要有)\s*([\u4e00-\u9fa5/、和,，]{1,20})"
        for m in re.finditer(neg_pattern, text):
            chunk = m.group(1)
            # 在片段里找命中的食材词
            for w in ALLERGEN_WORDS:
                if w in chunk and ALLERGEN_CANON.get(w, w) not in exclude:
                    exclude.append(ALLERGEN_CANON.get(w, w))
            for w in AVOID_WORDS:
                if w in chunk and AVOID_CANON.get(w, w) not in require:
                    require.append(AVOID_CANON.get(w, w))
        # "XX过敏" 单独兜底（可能没落在否定词片段里）
        for m in re.finditer(r"([\u4e00-\u9fa5]{1,4})\s*过敏", text):
            w = m.group(1)
            if w in ALLERGEN_CANON and ALLERGEN_CANON[w] not in exclude:
                exclude.append(ALLERGEN_CANON[w])
        return exclude, require


# ---------------------------------------------------------------------------
# 4. LLM 解析器（可选，function calling）——展示"可插拔"的另一半
# ---------------------------------------------------------------------------

LLM_TOOL_SCHEMA = {
    "name": "parse_search_slots",
    "description": "把用户的美食查询拆成结构化槽位。硬约束不确定就留空，禁止猜测。",
    "parameters": {
        "type": "object",
        "properties": {
            "district": {"type": "string", "description": "行政区，如 越秀区"},
            "business_area": {"type": "string", "description": "商圈，如 西华路"},
            "price_max": {"type": "integer", "description": "人均上限"},
            "price_min": {"type": "integer"},
            "cuisines": {"type": "array", "items": {"type": "string"}},
            "exclude_allergens": {"type": "array", "items": {"type": "string"},
                                  "description": "过敏原黑名单：店含有则排除"},
            "require_avoid": {"type": "array", "items": {"type": "string"},
                              "description": "忌口：要求店明确标注不使用"},
            "dietary_flags": {"type": "array", "items": {"type": "string"}},
            "max_walk_min": {"type": "integer"},
            "metro_lines": {"type": "array", "items": {"type": "string"}},
            "noise_level": {"type": "string"},
            "private_room": {"type": "boolean"},
            "soft": {"type": "array", "items": {"type": "string"}, "description": "软偏好"},
            "sort_by": {"type": "string", "enum": ["综合", "评分", "人均", "距离"]},
            "conflicts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["soft"],
    },
}


def _normalize_llm_slots(s: SearchSlots) -> None:
    """LLM 解析结果的白名单归一化（原地修改）—— 一道必须在入口做的安全网。

    为什么必须有（实测踩坑）：
        LLM 语义理解强，但【不受约束词表限制】。实测同一句"西华路人均30以下的肠粉"，
        它有时输出 soft=['肠粉']（正确），有时却输出 cuisines=['肠粉']（错误）——
        "肠粉"根本不是数据里的 cuisine 枚举值（ cuisine 只有粤菜/粥粉面/广式点心等 14 类）。
        一旦这种幻觉值进到 cuisines 硬约束，Qdrant pre-filter 会 100% 过滤掉所有店，
        用户看到"0 家" —— 硬约束错一次的代价是事故。

    处理策略（宁可漏，不可错）：
        - 受约束字段（cuisines / district / dietary_flags / metro_lines）逐个对白名单校验；
        - 白名单内的保留；不在的【降级为软偏好】—— 只影响排序，不会误杀任何店；
        - 这样既吃到 LLM 的语义理解（"肠粉"仍作为偏好参与语义召回），又杜绝硬约束污染。

    面试讲点：这就是"LLM 输出必须过 schema 校验 + 枚举归一化"的落地。
        prompt 里写"cuisines 只能填枚举值"只是劝，这里的代码校验才是查。
    """
    # 1) cuisine：数据里的 14 个枚举值（与 config.CUISINES 一致）
    valid_cuisines = set(config.CUISINES)
    bad_cuisines = [c for c in s.cuisines if c not in valid_cuisines]
    s.cuisines = [c for c in s.cuisines if c in valid_cuisines]
    # 2) district：必须是已知行政区
    valid_districts = set(config.DISTRICTS)
    bad_districts = [d for d in [s.district] if d and d not in valid_districts]
    if bad_districts:
        s.district = None
    # 3) dietary_flags：必须在白名单
    valid_diet = set(config.DIETARY_FLAGS.values())
    bad_diet = [d for d in s.dietary_flags if d not in valid_diet]
    s.dietary_flags = [d for d in s.dietary_flags if d in valid_diet]
    # 4) metro_lines：必须在白名单
    valid_metro = set(METRO_LINES)
    bad_metro = [m for m in s.metro_lines if m not in valid_metro]
    s.metro_lines = [m for m in s.metro_lines if m in valid_metro]

    # 所有被踢出的值降级为软偏好（去重，仍参与语义召回/排序，但不做硬过滤）
    for v in bad_cuisines + bad_districts + bad_diet + bad_metro:
        if v not in s.soft:
            s.soft.append(v)


def llm_parse(query: str) -> SearchSlots | None:
    """走 OpenAI 兼容的 function calling。没配 key / 出错时返回 None，由上层降级到规则版。

    这就是方案里的 LLMClient 抽象的最小实现：base_url / model / key 全走环境变量，
    不绑死任何厂商（DeepSeek / Qwen / GLM 都兼容 OpenAI 格式）。
    """
    api_key = os.environ.get("RAG_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("RAG_LLM_BASE_URL")
    model = os.environ.get("RAG_LLM_MODEL", "deepseek-chat")
    if not api_key:
        return None
    try:
        from openai import OpenAI  # 延迟导入：没用 LLM 时不要求装这个包
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "你是美食检索的槽位解析器。只输出函数调用，不要解释。"
                    "硬约束（价格/过敏/行政区）不确定就留空，禁止猜测。"
                    "'不要香菜'->require_avoid=['香菜']；'花生过敏'->exclude_allergens=['花生']；"
                    "'人均80以内'->price_max=80；'别太吵'->soft=['安静']不要设noise_level。"
                )},
                {"role": "user", "content": query},
            ],
            tools=[{"type": "function", "function": LLM_TOOL_SCHEMA}],
            # 用 auto 而非强制指定 function：
            # 很多"OpenAI 兼容"网关不接受 {"type":"function",...} 这种强制 tool_choice（会 400）。
            # auto 通用性最好；代价是模型可能不调用，下面做校验兜底。
            tool_choice="auto",
            temperature=0,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:  # 模型选择直接回答而非调用工具 -> 视为解析失败
            raise ValueError("模型未返回 tool_calls")
        call = next((c for c in msg.tool_calls
                     if c.function.name == "parse_search_slots"), msg.tool_calls[0])
        args = call.function.arguments
        import json
        data = json.loads(args)
        allowed = {f for f in SearchSlots.__dataclass_fields__}
        data = {k: v for k, v in data.items() if k in allowed}
        s = SearchSlots(**data)
        _normalize_llm_slots(s)  # 白名单归一化：LLM 的枚举幻觉绝不进硬约束
        # 冲突检测必须和规则版共用！否则 LLM 把"米其林"归到 soft 后，
        # "人均30米其林"就不会触发二次确认（实测踩过的坑）。
        _detect_conflicts(s, query.strip())
        s.parser = "llm"
        return s
    except Exception as e:  # noqa: BLE001 - 降级路径要吞掉所有异常
        print(f"[slot] LLM 解析失败，降级规则版：{type(e).__name__}: {e}")
        return None


def parse_slots(query: str, prefer_llm: bool = True) -> SearchSlots:
    """统一入口：优先 LLM，失败/未配置则规则版。"""
    if prefer_llm:
        s = llm_parse(query)
        if s is not None:
            return s
    return RuleSlotParser().parse(query)


if __name__ == "__main__":
    # 自测：跑几条典型 query，看槽位拆得对不对
    tests = [
        "西华路附近人均30以下的肠粉店",
        "不要香菜和花生，人均80以内的粤菜",
        "必须安静有包间，珠江新城评分高的",
        "地铁3号线步行10分钟内，别太吵",
        "越秀区人均30米其林",
        "花生过敏，找个便宜的",
    ]
    p = RuleSlotParser()
    for q in tests:
        s = p.parse(q)
        print(f"\nQ: {q}")
        print(f"   {s.summary()}")
        if s.need_clarify:
            print(f"   [需追问] {s.clarify_question}")
