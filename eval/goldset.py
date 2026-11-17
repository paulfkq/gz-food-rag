#!/usr/bin/env python3
"""Gold Set 生成器：从 300 家种子数据程序化构造可读的评估问答对。

设计原则（面试讲点）：
    1. Ground truth 不靠人工拍脑袋 —— 全部由结构化字段【确定性过滤】得出：
       给定查询约束，遍历 300 家店，满足全部硬约束的就是 gold_shop_ids。
       这样 gold 与 pipeline 的 filter 逻辑相互独立（一个用原始 JSON，一个用
       Qdrant payload），避免"用被测代码自己造答案"的循环论证。
    2. 固定 seed 可复现；约束组合从真实店铺字段采样，保证 gold 集大小适中
       （1~8 家），否则 Recall@8 天然很低、指标失去区分度。
    3. 每条样本同时存 gold_answer / gold_contexts，供 RAGAS 的 Context Recall、
       Faithfulness 等指标使用。

输出：eval/goldset.jsonl，每行一条样本：
    {
      "id", "type", "query", "constraints", "gold_shop_ids",
      "gold_answer", "gold_contexts", "expect_clarify"
    }

用法：
    python eval/goldset.py --size 40 --out eval/goldset.jsonl --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from retrieve.slots import parse_slots  # noqa: E402  用于记录"规则解析器实际解析出的槽位"


def load_shops(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_parents(path: Path) -> dict[str, str]:
    """shop_id -> parent 主档文本（作为 RAGAS 的 gold contexts）。"""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            c = json.loads(line)
            if c.get("chunk_type") == "parent":
                out[c["source_shop_id"]] = c["text"]
    return out


# ---------------------------------------------------------------------------
# 约束匹配：独立于 pipeline 的 filter 实现，直接对原始 shop JSON 判断
# ---------------------------------------------------------------------------
def matches(shop: dict, c: dict) -> bool:
    if c.get("district") and shop.get("district") != c["district"]:
        return False
    if c.get("business_area") and shop.get("business_area") != c["business_area"]:
        return False
    if c.get("price_max") is not None and (shop.get("price_per_person") or 9999) > c["price_max"]:
        return False
    if c.get("price_min") is not None and (shop.get("price_per_person") or 0) < c["price_min"]:
        return False
    if c.get("cuisines") and not set(c["cuisines"]) & set(shop.get("cuisine") or []):
        return False
    if c.get("exclude_allergens") and set(c["exclude_allergens"]) & set(shop.get("allergens") or []):
        return False
    if c.get("require_avoid") and not set(c["require_avoid"]).issubset(set(shop.get("avoid_ingredients") or [])):
        return False
    if c.get("scene") and c["scene"] not in (shop.get("scene_tags") or []):
        return False
    if c.get("is_late_night") and not shop.get("is_late_night"):
        return False
    if c.get("is_dim_sum") and not shop.get("is_dim_sum"):
        return False
    if c.get("metro_line") and not any(
        m.get("line") == c["metro_line"] for m in (shop.get("nearest_metro") or [])
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# 查询模板：每个模板定义「怎么从锚点店生成约束」和「怎么把约束说成人话」
# ---------------------------------------------------------------------------
def tpl_area_price(a: dict, rng: random.Random) -> dict:
    return {"business_area": a["business_area"], "price_max": a["price_per_person"] + rng.choice([0, 5, 10])}


def q_area_price(c: dict) -> str:
    return f"{c['business_area']}附近人均{c['price_max']}以内有什么好吃的？"


def tpl_district_cuisine(a: dict, rng: random.Random) -> dict | None:
    if not a.get("cuisine"):
        return None
    return {"district": a["district"], "cuisines": [rng.choice(a["cuisine"])],
            "price_max": a["price_per_person"] + rng.choice([5, 10, 15])}


def q_district_cuisine(c: dict) -> str:
    return f"{c['district']}人均{c['price_max']}以下的{c['cuisines'][0]}，有推荐吗？"


def tpl_allergen_cuisine(a: dict, rng: random.Random) -> dict | None:
    if not a.get("allergens") or not a.get("cuisine"):
        return None
    return {"exclude_allergens": [rng.choice(a["allergens"])],
            "cuisines": [rng.choice(a["cuisine"])],
            "price_max": a["price_per_person"] + rng.choice([5, 10, 15])}


def q_allergen_cuisine(c: dict) -> str:
    return f"不吃{c['exclude_allergens'][0]}，人均{c['price_max']}以下的{c['cuisines'][0]}店有哪些？"


def tpl_scene_area(a: dict, rng: random.Random) -> dict | None:
    if not a.get("scene_tags") or not a.get("business_area"):
        return None
    return {"business_area": a["business_area"], "scene": rng.choice(a["scene_tags"]),
            "price_max": a["price_per_person"] + rng.choice([5, 10, 15])}


def q_scene_area(c: dict) -> str:
    return f"想在{c['business_area']}找适合{c['scene']}的店，人均{c['price_max']}左右"


def tpl_late_night(a: dict, rng: random.Random) -> dict | None:
    if not a.get("is_late_night"):
        return None
    return {"district": a["district"], "is_late_night": True,
            "price_max": a["price_per_person"] + rng.choice([5, 10])}


def q_late_night(c: dict) -> str:
    return f"{c['district']}有吃夜宵的地方吗？人均不超过{c['price_max']}"


def tpl_dimsum(a: dict, rng: random.Random) -> dict | None:
    if not a.get("is_dim_sum"):
        return None
    return {"district": a["district"], "is_dim_sum": True,
            "price_max": a["price_per_person"] + rng.choice([10, 20])}


def q_dimsum(c: dict) -> str:
    return f"{c['district']}哪里喝早茶比较好？人均{c['price_max']}以内"


def tpl_metro(a: dict, rng: random.Random) -> dict | None:
    if not a.get("nearest_metro") or not a.get("cuisine"):
        return None
    return {"metro_line": rng.choice(a["nearest_metro"])["line"],
            "cuisines": [rng.choice(a["cuisine"])],
            "price_max": a["price_per_person"] + rng.choice([5, 10, 15])}


def q_metro(c: dict) -> str:
    return f"地铁{c['metro_line']}沿线人均{c['price_max']}以下的{c['cuisines'][0]}店"


TEMPLATES = [
    ("area_price", tpl_area_price, q_area_price),
    ("district_cuisine", tpl_district_cuisine, q_district_cuisine),
    ("allergen", tpl_allergen_cuisine, q_allergen_cuisine),
    ("scene", tpl_scene_area, q_scene_area),
    ("late_night", tpl_late_night, q_late_night),
    ("dimsum", tpl_dimsum, q_dimsum),
    ("metro", tpl_metro, q_metro),
]


def make_gold_answer(gold: list[dict]) -> str:
    if not gold:
        return "根据现有资料，没有找到符合条件的店。"
    lines = [f"根据资料，符合条件的有 {len(gold)} 家："]
    for i, s in enumerate(gold, 1):
        cuisine = "、".join(s.get("cuisine") or [])
        area = " / ".join(filter(None, [s.get("district"), s.get("business_area")]))
        lines.append(
            f"{i}. {s['name']}——人均 ¥{s.get('price_per_person')}，{area}，"
            f"{cuisine}，评分 {s.get('rating')}。"
        )
    return "\n".join(lines)


def build_sample(typ: str, query: str, constraints: dict, gold: list[dict],
                 parents: dict[str, str], sid: str,
                 expect_clarify: bool = False) -> dict:
    parsed = parse_slots(query, prefer_llm=False)  # 记录规则解析器实际产出，评估解析准确率
    return {
        "id": sid,
        "type": typ,
        "query": query,
        "constraints": constraints,
        "gold_shop_ids": [s["shop_id"] for s in gold],
        "gold_shop_names": [s["name"] for s in gold],
        "gold_answer": make_gold_answer(gold),
        "gold_contexts": [parents[s["shop_id"]] for s in gold if s["shop_id"] in parents],
        "expect_clarify": expect_clarify,
        "parsed_slots": {
            "district": parsed.district,
            "business_area": parsed.business_area,
            "price_max": parsed.price_max,
            "cuisines": parsed.cuisines,
            "exclude_allergens": parsed.exclude_allergens,
            "scene_soft": [w for w in parsed.soft if w in (constraints.get("scene") or "")],
            "need_clarify": parsed.need_clarify,
        },
    }


def generate(size: int, seed: int, shops: list[dict], parents: dict[str, str]) -> list[dict]:
    rng = random.Random(seed)
    samples: list[dict] = []
    seen_queries: set[str] = set()

    # 正常样本：按模板轮询，每轮每模板尝试一次
    import math
    per_type = max(1, math.ceil((size - 2) / len(TEMPLATES)))
    for typ, tpl_fn, q_fn in TEMPLATES:
        made = 0
        for _ in range(per_type * 40):  # 足够多的尝试次数
            if made >= per_type:
                break
            anchor = rng.choice(shops)
            c = tpl_fn(anchor, rng)
            if not c:
                continue
            query = q_fn(c)
            if query in seen_queries:
                continue
            gold = [s for s in shops if matches(s, c)]
            if not (1 <= len(gold) <= 8):  # gold 太少无意义，太多则 Recall@8 天然低
                continue
            seen_queries.add(query)
            samples.append(build_sample(typ, query, c, gold, parents, f"q{len(samples)+1:03d}"))
            made += 1

    # 先截正常样本到 size-2，保证下面的冲突样本一定在最终集合里
    samples = samples[:max(0, size - 2)]

    # 冲突样本：验证 need_clarify（这类不参与 Recall 类指标，单独统计追问命中率）
    for query in ["人均30米其林", "人均20块的高档海鲜私房菜"]:
        if len(samples) >= size:
            break
        samples.append(build_sample("clarify", query, {}, [], parents,
                                    f"q{len(samples)+1:03d}", expect_clarify=True))
    return samples


def main() -> None:
    ap = argparse.ArgumentParser(description="生成广州美食 RAG 评估 Gold Set")
    ap.add_argument("--shops", default=str(ROOT / "data" / "raw" / "shops.jsonl"))
    ap.add_argument("--chunks", default=str(ROOT / "data" / "chunks" / "chunks.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "goldset.jsonl"))
    ap.add_argument("--size", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    shops = load_shops(Path(args.shops))
    parents = load_parents(Path(args.chunks))
    samples = generate(args.size, args.seed, shops, parents)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    from collections import Counter
    print(f"[goldset] 写入 {out.resolve()}  样本数={len(samples)}  seed={args.seed}")
    print("[goldset] 类型分布:", dict(Counter(s["type"] for s in samples)))
    print("[goldset] gold 店铺数分布:", dict(Counter(len(s["gold_shop_ids"]) for s in samples)))
    print("[goldset] 样例:")
    for s in samples[:3]:
        print(f"  {s['id']} [{s['type']}] {s['query']}  -> gold={len(s['gold_shop_ids'])}家 "
              f"({', '.join(s['gold_shop_ids'][:3])})")


if __name__ == "__main__":
    main()
