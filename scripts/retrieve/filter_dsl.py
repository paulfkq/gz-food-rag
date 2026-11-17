"""检索层第 2 环：Filter DSL 编译（硬约束的 pre-filter）。

一句话职责：把 SearchSlots 里的【硬约束】翻译成 Qdrant 的 Filter 对象，
让向量检索"在候选阶段就把不合条件的店排除掉"，而不是检索完再人工挑。

为什么必须前置（新手必懂的反例）：
    假设用户要"人均30以内"，你先向量检索出 Top-50，再过滤掉人均>30的。
    如果库里大部分店人均都在 50 以上，Top-50 里可能只剩 3 家符合——
    召回被浪费了，真正符合的平价店因为向量分稍低根本没进 Top-50。
    pre-filter = "先限定候选池，再在里面按相似度排序"，从根上避免这个问题。

Qdrant Filter 的三个逻辑块（对应 SQL 的 WHERE）：
    must    : 同时满足（AND）
    should  : 满足其一（OR）
    must_not: 不能满足（NOT）—— 过敏原黑名单用它

本文件同时提供 compile_filter()（给 Qdrant 用）和 verify_hard()（后置复核用），
二者检查的是同一批字段，构成"前置 + 后置"双保险。
"""
from __future__ import annotations

from qdrant_client import models

from .slots import SearchSlots


def compile_filter(slots: SearchSlots, now_hour: int | None = None) -> models.Filter | None:
    """把硬约束编译成 Qdrant Filter。没有任何硬约束时返回 None（不过滤）。

    字段与 chunker.py 冗余进 payload 的字段一一对应，这是入库时就设计好的。
    """
    must: list[models.FieldCondition] = []
    must_not: list[models.FieldCondition] = []

    # --- 行政区 / 商圈（精确匹配，KEYWORD 索引） ---
    if slots.district:
        must.append(models.FieldCondition(key="district", match=models.MatchValue(value=slots.district)))
    if slots.business_area:
        must.append(models.FieldCondition(key="business_area", match=models.MatchValue(value=slots.business_area)))

    # --- 价格区间（INTEGER 索引，Range） ---
    if slots.price_max is not None or slots.price_min is not None:
        must.append(models.FieldCondition(
            key="price_per_person",
            range=models.Range(gte=slots.price_min, lte=slots.price_max),
        ))

    # --- 菜系（OR：指定多个菜系满足其一） ---
    if slots.cuisines:
        must.append(models.FieldCondition(key="cuisine", match=models.MatchAny(any=slots.cuisines)))

    # --- 饮食规范（清真/全素，OR） ---
    if slots.dietary_flags:
        must.append(models.FieldCondition(key="dietary_flags", match=models.MatchAny(any=slots.dietary_flags)))

    # --- 过敏原黑名单（核心红线）：店【含有】这些 => 排除 ---
    # allergens 是"含有"语义，所以用 must_not + MatchAny
    if slots.exclude_allergens:
        must_not.append(models.FieldCondition(key="allergens", match=models.MatchAny(any=slots.exclude_allergens)))

    # --- 忌口白名单：要求店【明确标注不使用】=> must + MatchAny ---
    # 注意：data 里只有 46.7% 的店标注了 avoid_ingredients，未标注视为"无法保证"，
    # 所以这里只召回明确标注的店（保守策略，宁可少推不可错推）。
    if slots.require_avoid:
        must.append(models.FieldCondition(key="avoid_ingredients", match=models.MatchAny(any=slots.require_avoid)))

    # --- 地铁线与步行时长 ---
    if slots.metro_lines:
        must.append(models.FieldCondition(key="metro_lines", match=models.MatchAny(any=slots.metro_lines)))
    if slots.max_walk_min is not None:
        must.append(models.FieldCondition(
            key="metro_walk_min", range=models.Range(lte=slots.max_walk_min)))

    # --- 噪声硬要求（"必须安静"）---
    if slots.noise_level:
        must.append(models.FieldCondition(key="noise_level", match=models.MatchValue(value=slots.noise_level)))

    # --- 布尔设施 / 时段 ---
    if slots.private_room:
        must.append(models.FieldCondition(key="private_room", match=models.MatchValue(value=True)))
    if slots.outdoor_seating:
        must.append(models.FieldCondition(key="outdoor_seating", match=models.MatchValue(value=True)))
    if slots.is_late_night:
        must.append(models.FieldCondition(key="is_late_night", match=models.MatchValue(value=True)))
    if slots.is_dim_sum:
        must.append(models.FieldCondition(key="is_dim_sum", match=models.MatchValue(value=True)))

    # --- 现在营业（粗判：数据里 opening_hours 是 [{"open","close","days"}]）---
    # 注：Qdrant 无法直接对嵌套数组做时段比较，这里留给后置复核 verify_hard 处理，
    #     pre-filter 阶段不设条件（否则会把本来能营业的店误杀）。
    if not must and not must_not:
        return None
    return models.Filter(must=must or None, must_not=must_not or None)


def verify_hard(payload: dict, slots: SearchSlots, now_hour: int | None = None) -> tuple[bool, str]:
    """后置复核：对单条候选 payload 逐字段检查硬约束是否满足。

    返回 (是否通过, 不通过原因)。与 compile_filter 互为双保险：
      - compile_filter 在 Qdrant 查询时挡一道（召回层面）
      - verify_hard 在 Rerank 之后再挡一道（防止融合/精排过程混入不合条件的候选）
    为什么两道都要：Qdrant filter 依赖 payload index 配置正确；BM25 那一路在内存里
    检索根本不过 Qdrant filter；LLM/融合代码也可能出 bug。后置复核是最后防线。
    """
    # 行政区 / 商圈
    if slots.district and payload.get("district") != slots.district:
        return False, f"行政区不符:{payload.get('district')}!={slots.district}"
    if slots.business_area and payload.get("business_area") != slots.business_area:
        return False, f"商圈不符:{payload.get('business_area')}!={slots.business_area}"

    # 价格
    price = payload.get("price_per_person")
    if price is not None:
        if slots.price_max is not None and price > slots.price_max:
            return False, f"超预算:¥{price}>¥{slots.price_max}"
        if slots.price_min is not None and price < slots.price_min:
            return False, f"低于下限:¥{price}<¥{slots.price_min}"

    # 菜系（OR）
    if slots.cuisines:
        cuisines = set(payload.get("cuisine") or [])
        if not cuisines.intersection(slots.cuisines):
            return False, f"菜系不符:{cuisines}"

    # 饮食规范（OR）
    if slots.dietary_flags:
        flags = set(payload.get("dietary_flags") or [])
        if not flags.intersection(slots.dietary_flags):
            return False, f"饮食规范不符:{flags}"

    # 过敏原黑名单：含有即排除
    if slots.exclude_allergens:
        allergens = set(payload.get("allergens") or [])
        hit = allergens.intersection(slots.exclude_allergens)
        if hit:
            return False, f"含过敏原:{hit}"

    # 忌口白名单：必须明确标注不使用
    if slots.require_avoid:
        avoid = set(payload.get("avoid_ingredients") or [])
        if not avoid.issuperset(slots.require_avoid):
            return False, f"未标注不含:{set(slots.require_avoid)-avoid}"

    # 地铁
    if slots.metro_lines:
        lines = set(payload.get("metro_lines") or [])
        if not lines.intersection(slots.metro_lines):
            return False, f"地铁线不符:{lines}"
    if slots.max_walk_min is not None:
        walk = payload.get("metro_walk_min")
        if walk is not None and walk > slots.max_walk_min:
            return False, f"步行过远:{walk}min"

    # 噪声
    if slots.noise_level and payload.get("noise_level") != slots.noise_level:
        return False, f"噪声不符:{payload.get('noise_level')}"

    # 布尔设施
    for slot_name in ["private_room", "outdoor_seating", "is_late_night", "is_dim_sum"]:
        if getattr(slots, slot_name) and not payload.get(slot_name, False):
            return False, f"缺设施/属性:{slot_name}"

    # 营业时段（只有明确要求"现在营业"时检查；跨午夜的 close<open 要特殊处理）
    if slots.open_now and now_hour is not None:
        if not _is_open_at(payload.get("opening_hours") or [], now_hour):
            return False, f"当前{now_hour}:00不营业"

    return True, ""


def _is_open_at(hours: list[dict], hour: int) -> bool:
    """粗判某个小时是否营业。跨零点（如 17:00-02:00）要拆成两段判断。"""
    for seg in hours:
        try:
            o = int(seg["open"].split(":")[0])
            c = int(seg["close"].split(":")[0])
        except (ValueError, KeyError):
            continue
        if o <= c:  # 普通时段 11:00-22:00
            if o <= hour < c:
                return True
        else:       # 跨零点 17:00-02:00 => hour>=17 或 hour<2
            if hour >= o or hour < c:
                return True
    return False


if __name__ == "__main__":
    # 用一个假 payload 自测
    from .slots import RuleSlotParser
    q = "不要香菜和花生，人均80以内的粤菜"
    slots = RuleSlotParser().parse(q)
    print("槽位:", slots.summary())
    f = compile_filter(slots)
    print("Qdrant Filter:", f.model_dump() if f else None)
    ok, why = verify_hard(
        {"cuisine": ["粤菜"], "price_per_person": 60,
         "allergens": ["麸质"], "avoid_ingredients": ["香菜"]}, slots)
    print("复核(不含花生+标注不含香菜):", ok, why)
    ok2, why2 = verify_hard(
        {"cuisine": ["粤菜"], "price_per_person": 60,
         "allergens": ["花生"], "avoid_ingredients": []}, slots)
    print("复核(含花生+未标注不含香菜):", ok2, why2)
