"""Parent-Child 父子块切分器（方案 §3.3 / §3.4）。

输入：data/raw/shops.jsonl（300 家，45 字段）
输出：data/chunks/chunks.jsonl（Parent + Child）

设计要点（面试讲点）：
1. 餐厅是实体型数据，字段强关联。固定 512 滑窗会把「人均 18」和店名切开，
   导致硬过滤失效 + LLM 张冠李戴。所以按「语义角色」切，不按 token 数切。
2. Parent：全字段渲染成半结构化文本，作为命中 Child 后回溯的 LLM 上下文。
   采用经典 small-to-big 形态——Parent 不向量化、不进 Qdrant，只存成本地的
   parent store；检索只面向 Child，命中后按 parent_id 取回 Parent 全文。
   （若要让 Parent 也参与检索，只需把它同样向量化入库并用 chunk_type 区分。）
3. Child 分 5 类：overview / dish / scene / pros / cons。
   - pros（好评）与 cons（争议）必须分开：「环境嘈杂」若和好评混在一个向量里，
     语义被稀释，查「安静」反而可能命中这家店。
4. 每个 Child 的 payload 冗余复制父级硬约束字段。这样 pre-filter 能在 Child 层
   一次完成，命中后无需再 join 店铺表——这是硬约束 100% 满足的前提。

运行：
    python scripts/chunker.py \
      --in data/raw/shops.jsonl \
      --out data/chunks/chunks.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MODEL_VERSION = "bge-m3@2026-11"

# 需要冗余到每个 Child payload 的硬约束 / 高频过滤字段。
# 判断标准：会进入 SearchSlots.hard 或常常被当作筛选条件的字段。
HARD_FIELDS = (
    "district", "business_area", "cuisine", "price_per_person", "price_band",
    "spiciness", "sweetness", "oily_level", "noise_level", "geo",
    "nearest_metro", "opening_hours", "is_late_night", "is_dim_sum",
    "queue_min", "rating", "review_count", "allergens", "avoid_ingredients",
    "dietary_flags", "health_tags", "season_tags", "private_room",
    "outdoor_seating", "parking", "reservation", "takeaway",
    "scene_tags", "env_tags", "taste_tags", "since_year", "awards",
)


def _yes_no(v: bool) -> str:
    return "有" if v else "无"


def _join(v: Any, sep: str = "、") -> str:
    if isinstance(v, (list, tuple)):
        return sep.join(str(x) for x in v) if v else "无"
    return str(v) if v is not None else "无"


def _hours_text(hours: list[dict]) -> str:
    if not hours:
        return "时间未标注"
    parts = []
    for h in hours:
        parts.append(f"{h.get('open', '?')}-{h.get('close', '?')}")
    return "/".join(parts)


def _metro_text(metro: list[dict]) -> str:
    if not metro:
        return "未标注地铁"
    segs = [f"{m['line']}{m['station']}站步行{m['walk_min']}分钟" for m in metro]
    return "；".join(segs)


def render_parent(s: dict) -> str:
    """Parent 主档：字段标题 + 值的半结构化文本，既适合人读也保留字段边界。"""
    first_metro = s["nearest_metro"][0] if s["nearest_metro"] else None
    metro_short = (
        f"地铁 {first_metro['line']}{first_metro['station']}站步行{first_metro['walk_min']}分钟"
        if first_metro else "地铁未标注"
    )
    alias = _join(s["aliases"], "，")
    awards = _join(s["awards"])
    avoid = _join(s["avoid_ingredients"])
    lines = [
        f"【店铺】{s['name']}（别名：{alias}）｜ID {s['shop_id']}",
        (f"【位置】{s['district']} / {s['business_area']}｜{s['address']}｜"
         f"{metro_short}｜{_yes_no(s['parking'])}停车位"),
        f"【菜系】{_join(s['cuisine'])}｜招牌：{_join(s['dishes'])}",
        (f"【口味】{_join(s['taste_tags'])}；辣度 {s['spiciness']}/5，"
         f"甜度 {s['sweetness']}/5，油 {s['oily_level']}/5"),
        (f"【价格】人均 ¥{s['price_per_person']}（区间 {s['price_band']}）｜"
         f"性价比 {s['value_for_money']}"),
        (f"【环境】{_join(s['env_tags'])}、{s['noise_level']}、"
         f"{_yes_no(s['private_room'])}包间、{_yes_no(s['outdoor_seating'])}户外位"
         f"｜适合 {_join(s['scene_tags'])}"),
        (f"【时间】{_hours_text(s['opening_hours'])}｜"
         f"早茶{'有' if s['is_dim_sum'] else '无'}｜夜宵{'有' if s['is_late_night'] else '无'}"
         f"｜排队约 {s['queue_min']} 分钟"),
        (f"【忌口与过敏】含过敏原：{_join(s['allergens'])}；"
         f"饮食规范：{_join(s['dietary_flags'])}；不使用：{avoid}"),
        (f"【口碑】评分 {s['rating']}（{s['review_count']} 条）｜"
         f"{s['since_year']} 年开业｜{awards}"),
        f"【好评】{_join(s['pros'], '；')}",
        f"【争议】{_join(s['cons'], '；')}",
    ]
    return "\n".join(lines)


def render_child(kind: str, s: dict, dish: str | None = None) -> str:
    """Child：按语义角色切成 5 类，每类文本只承载一个意图，语义更纯。"""
    name = s["name"]
    where = f"{s['district']}{s['business_area']}"
    price = f"人均 ¥{s['price_per_person']}"
    if kind == "child_overview":
        return (
            f"{name} 是{where}的一家{_join(s['cuisine'])}，{price}。"
            f"招牌菜：{_join(s['dishes'])}。口味{_join(s['taste_tags'])}，"
            f"{_join(s['env_tags'])}，适合{_join(s['scene_tags'])}。"
        )
    if kind == "child_dish":
        return (
            f"{name}（{where}，{price}）的招牌菜「{dish}」。"
            f"口味{_join(s['taste_tags'])}，{_join(s['pros'], '；')}。"
        )
    if kind == "child_scene":
        return (
            f"{name} 的环境与场景：{_join(s['env_tags'])}，{s['noise_level']}，"
            f"{_yes_no(s['private_room'])}包间，{_yes_no(s['outdoor_seating'])}户外座位。"
            f"适合{_join(s['scene_tags'])}。{_join(s['pros'], '；')}。"
        )
    if kind == "child_pros":
        return f"{name} 的好评：{_join(s['pros'], '；')}。招牌：{_join(s['dishes'])}。"
    if kind == "child_cons":
        return (
            f"{name} 的争议与不足：{_join(s['cons'], '；')}。"
            f"（{where}，{price}，评分 {s['rating']}）"
        )
    raise ValueError(f"未知 chunk 类型：{kind}")


def _payload(s: dict, chunk_type: str, parent_id: str | None) -> dict:
    """Child 冗余父级硬约束字段；Parent 也带一份，保持结构一致。"""
    p = {k: s[k] for k in HARD_FIELDS if k in s}
    p.update({
        "shop_name": s["name"],
        "chunk_type": chunk_type,
        "source_shop_id": s["shop_id"],
        "parent_id": parent_id,
        # 便于 Qdrant filter 的派生字段
        "metro_lines": [m["line"] for m in s["nearest_metro"]],
        "metro_walk_min": min((m["walk_min"] for m in s["nearest_metro"]), default=999),
        "model_version": MODEL_VERSION,
    })
    return p


def split_shop(s: dict) -> list[dict]:
    """把一家店切成 1 Parent + 5 类 Child。"""
    sid = s["shop_id"]
    out: list[dict] = []

    # Parent：回溯上下文，不进向量库
    out.append({
        "chunk_id": f"{sid}::parent",
        "chunk_type": "parent",
        "source_shop_id": sid,
        "parent_id": None,
        "text": render_parent(s),
        "payload": _payload(s, "parent", None),
        "model_version": MODEL_VERSION,
    })

    # Child：真正进向量库、被检索的单元
    specs: list[tuple[str, str | None]] = [("child_overview", None)]
    specs += [("child_dish", d) for d in s["dishes"]]
    specs += [
        ("child_scene", None),
        ("child_pros", None),
        ("child_cons", None),
    ]
    for i, (kind, dish) in enumerate(specs):
        out.append({
            "chunk_id": f"{sid}::{kind}::{i}",
            "chunk_type": kind,
            "source_shop_id": sid,
            "parent_id": f"{sid}::parent",
            "text": render_child(kind, s, dish),
            "payload": _payload(s, kind, f"{sid}::parent"),
            "model_version": MODEL_VERSION,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/raw/shops.jsonl")
    ap.add_argument("--out", default="data/chunks/chunks.jsonl")
    args = ap.parse_args()

    src = Path(args.inp)
    shops = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]

    chunks: list[dict] = []
    for s in shops:
        chunks.extend(split_shop(s))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    by_type: dict[str, int] = {}
    for c in chunks:
        by_type[c["chunk_type"]] = by_type.get(c["chunk_type"], 0) + 1
    print(f"[切分] 店铺 {len(shops)} 家 -> chunk {len(chunks)} 个")
    print(f"[分布] {json.dumps(by_type, ensure_ascii=False)}")
    print(f"[输出] {out.resolve()}")

    # 自检：chunk_id 唯一、child 都带 parent_id、parent 无 parent_id、文本非空
    assert len({c["chunk_id"] for c in chunks}) == len(chunks), "chunk_id 不唯一"
    assert all(c["text"].strip() for c in chunks), "存在空文本 chunk"
    assert all(c["parent_id"] is None for c in chunks if c["chunk_type"] == "parent")
    assert all(c["parent_id"] for c in chunks if c["chunk_type"] != "parent")
    print("[自检] chunk_id 唯一 / 文本非空 / 父子指针完整：通过")

    # 打印 1 家店的完整切分结果，便于人工核对模板
    sample_sid = shops[0]["shop_id"]
    print(f"\n===== 样例：{sample_sid} 的切分结果 =====")
    for c in chunks:
        if c["source_shop_id"] == sample_sid:
            print(f"\n--- {c['chunk_type']} | {c['chunk_id']} ---")
            print(c["text"])


if __name__ == "__main__":
    main()
