#!/usr/bin/env python3
"""确定性指标：不依赖 LLM，快且可复现，是消融实验的主力指标。

面试讲点：
    RAGAS 这类 LLM-as-judge 指标虽有语义质感，但慢且带 judge 偏差；
    检索层的核心质量（该召回的召回到没有、硬约束守没守住）完全可以用
    ID 集合 + 结构化字段精确计算 —— 这部分应当作为回归测试的主指标，
    RAGAS 只作为补充的语义维度。分层评估才是工程上可持续的做法。
"""
from __future__ import annotations


def recall_at_k(retrieved: list[str], gold: list[str], k: int = 8) -> float | None:
    if not gold:
        return None
    return len(set(retrieved[:k]) & set(gold)) / len(gold)


def precision_at_k(retrieved: list[str], gold: list[str], k: int = 8) -> float | None:
    if not gold:
        return None
    top = retrieved[:k]
    return len(set(top) & set(gold)) / len(top) if top else 0.0


def mrr(retrieved: list[str], gold: list[str]) -> float | None:
    if not gold:
        return None
    for i, r in enumerate(retrieved):
        if r in set(gold):
            return 1.0 / (i + 1)
    return 0.0


def hit_rate_at_k(retrieved: list[str], gold: list[str], k: int = 8) -> float | None:
    if not gold:
        return None
    return 1.0 if set(retrieved[:k]) & set(gold) else 0.0


def ndcg_at_k(retrieved: list[str], gold: list[str], k: int = 8) -> float | None:
    """二值相关性的 nDCG@K（所有 gold 相关度视为 1）。"""
    if not gold:
        return None
    import math
    dcg = sum(1.0 / math.log2(i + 2) for i, r in enumerate(retrieved[:k]) if r in set(gold))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / ideal if ideal else 0.0


def hard_ok(payload: dict, constraints: dict) -> bool:
    """逐条检查硬约束（独立于 pipeline 的 filter 实现，直接对 payload 判断）。"""
    if constraints.get("district") and payload.get("district") != constraints["district"]:
        return False
    if constraints.get("business_area") and payload.get("business_area") != constraints["business_area"]:
        return False
    p = payload.get("price_per_person")
    if constraints.get("price_max") is not None and (p if p is not None else 9999) > constraints["price_max"]:
        return False
    if constraints.get("price_min") is not None and (p if p is not None else 0) < constraints["price_min"]:
        return False
    if constraints.get("cuisines") and not set(constraints["cuisines"]) & set(payload.get("cuisine") or []):
        return False
    if constraints.get("exclude_allergens") and set(constraints["exclude_allergens"]) & set(payload.get("allergens") or []):
        return False
    if constraints.get("scene") and constraints["scene"] not in (payload.get("scene_tags") or []):
        return False
    if constraints.get("is_late_night") and not payload.get("is_late_night"):
        return False
    if constraints.get("is_dim_sum") and not payload.get("is_dim_sum"):
        return False
    if constraints.get("metro_line") and not any(
        m.get("line") == constraints["metro_line"] for m in (payload.get("nearest_metro") or [])
    ):
        return False
    return True


def hard_compliance(hits: list[dict], constraints: dict) -> float | None:
    """返回结果中满足全部硬约束的比例。无硬约束的样本返回 None。"""
    if not constraints:
        return None
    if not hits:
        return 1.0  # 空结果没有"违反约束"的项
    return sum(hard_ok(h.get("payload", {}), constraints) for h in hits) / len(hits)


def slot_field_accuracy(sample: dict) -> tuple[int, int]:
    """槽位解析准确率：gold 约束里被规则解析器正确还原的字段数 / 总字段数。

    这是 RAG 入口（Query Understanding）的质量指标 —— 入口错一个价格上限，
    后面召回再准也没用。把它单独量化，能定位"检索差"到底是入口的锅还是召回的锅。
    """
    c = sample.get("constraints") or {}
    p = sample.get("parsed_slots") or {}
    if not c:
        return (0, 0)
    pairs = [
        ("district", lambda: c["district"] == p.get("district")),
        ("business_area", lambda: c["business_area"] == p.get("business_area")),
        ("price_max", lambda: c["price_max"] == p.get("price_max")),
        ("cuisines", lambda: set(c["cuisines"]) == set(p.get("cuisines") or [])),
        ("exclude_allergens", lambda: set(c["exclude_allergens"]) == set(p.get("exclude_allergens") or [])),
    ]
    hit = total = 0
    for key, check in pairs:
        if key not in c:
            continue
        total += 1
        hit += int(bool(check()))
    return hit, total


def entity_recall(answer: str, gold_names: list[str]) -> float | None:
    """答案中提到的 gold 店铺名比例（生成层的事实覆盖度，确定性替代指标）。"""
    if not gold_names:
        return None
    return sum(1 for n in gold_names if n in answer) / len(gold_names)


def summarize(values: list[float | None]) -> dict:
    """聚合并去掉 None（无 gold 的样本不参与该类指标）。"""
    nums = [v for v in values if v is not None]
    return {
        "mean": round(sum(nums) / len(nums), 4) if nums else None,
        "n": len(nums),
    }
