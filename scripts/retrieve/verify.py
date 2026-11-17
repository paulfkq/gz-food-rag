"""检索层第 6 环：硬约束后置复核 + Parent 回溯（small-to-big）。

一句话职责：Rerank 之后的 Top-N，逐家再验一次硬约束，然后按 parent_id
把 Child 换回完整的 Parent 主档文本，作为最终交给 LLM 的上下文。

两件事为什么都要做：

1) 硬约束后置复核（双保险的第二道）
   pre-filter 已经挡过一道，为什么还要复核？因为：
   - BM25 那一路在内存里跑，根本没经过 Qdrant 的 filter；
   - RRF 融合、Rerank 是代码逻辑，可能有 bug 让不合条件的候选混进来；
   - 宁可现在多一次 O(n) 的 Python 判断（n≤50），也不能让过敏店进答案。
   这是"关键约束不满足就是事故"的必然选择。

2) Parent 回溯（small-to-big 检索）
   检索用的是 Child（粒度细、语义纯、命中准），但送给 LLM 的应该是 Parent
   （全字段主档，信息完整）。如果直接把命中的"菜品块"丢给 LLM，它可能只看到
   "流沙包好吃"，却看不到"人均78、含花生"——答案就会缺关键信息甚至张冠李戴。
   所以命中 Child -> 记下 parent_id -> 从 parent store 取回完整主档。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .filter_dsl import verify_hard
from .recall import Candidate
from .slots import SearchSlots
from . import config


@dataclass
class FinalHit:
    """最终交付给上层的结构化结果。"""
    shop_id: str
    shop_name: str
    score: float
    parent_text: str
    payload: dict
    matched_hard: str          # 命中了哪些硬约束（用于答案解释）
    matched_soft: list[str]    # 命中了哪些软偏好
    matched_child_ids: list[str]


class ParentStore:
    """Parent 主档存储：从 chunks.jsonl 读出所有 parent，按 parent_id 索引。"""

    def __init__(self, chunks_path=None):
        self.path = chunks_path or config.CHUNKS_PATH
        self.parents: dict[str, dict] = {}
        self._load()

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                c = json.loads(line)
                if c.get("chunk_type") == "parent":
                    self.parents[c["chunk_id"]] = c
                    # 也用 source_shop_id 建一份，方便兜底
                    self.parents.setdefault(c.get("source_shop_id", ""), c)

    def get(self, parent_id: str) -> dict | None:
        return self.parents.get(parent_id)


def _soft_hits(payload: dict, soft: list[str]) -> list[str]:
    """统计命中了哪些软偏好（只用于答案里解释，不影响准入）。

    软偏好词可能落在 taste_tags / scene_tags / env_tags / noise_level 里。
    """
    if not soft:
        return []
    hay = set()
    for key in ["taste_tags", "scene_tags", "env_tags", "health_tags"]:
        hay.update(payload.get(key) or [])
    if payload.get("noise_level"):
        hay.add(payload["noise_level"])
    return [w for w in soft if w in hay]


def verify_and_expand(candidates: list[Candidate], slots: SearchSlots,
                      parent_store: ParentStore,
                      now_hour: int | None = None) -> tuple[list[FinalHit], list[dict]]:
    """复核 + 回溯。返回 (通过的最终结果, 被剔除的候选及原因)。

    注意去重：多路召回可能让同一家店的多个 Child 都进候选（概览块、菜品块…）。
    这里按 shop_id 去重，一家店只保留分数最高的那个 Child 对应的 Parent，
    并记下这家店所有命中的 child_id（用于答案引用与可观测性）。
    """
    passed: dict[str, FinalHit] = {}
    rejected: list[dict] = []

    for cand in candidates:
        ok, reason = verify_hard(cand.payload, slots, now_hour=now_hour)
        if not ok:
            rejected.append({"chunk_id": cand.chunk_id, "shop_name": cand.payload.get("shop_name"),
                             "reason": reason, "score": round(cand.score, 4)})
            continue

        pid = cand.payload.get("parent_id")
        parent = parent_store.get(pid) if pid else None
        if parent is None:
            rejected.append({"chunk_id": cand.chunk_id, "shop_name": cand.payload.get("shop_name"),
                             "reason": "Parent回溯失败", "score": round(cand.score, 4)})
            continue

        shop_id = parent.get("source_shop_id", "")
        if shop_id in passed:
            # 同店已有更高分的候选：只补 child_id，不覆盖
            passed[shop_id].matched_child_ids.append(cand.chunk_id)
            continue

        passed[shop_id] = FinalHit(
            shop_id=shop_id,
            shop_name=parent.get("payload", {}).get("shop_name", cand.payload.get("shop_name", "")),
            score=cand.score,
            parent_text=parent["text"],
            payload=cand.payload,
            matched_hard=slots.summary(),
            matched_soft=_soft_hits(cand.payload, slots.soft),
            matched_child_ids=[cand.chunk_id],
        )

    # 按分数降序输出
    results = sorted(passed.values(), key=lambda h: h.score, reverse=True)
    return results, rejected
