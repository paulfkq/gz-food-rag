"""检索层第 4、5 环：RRF 融合 + Rerank 精排。

=== 第 4 环 RRF（Reciprocal Rank Fusion）===
三路召回各有各的分数：dense 是 cosine(0~1)、BM25 无上界、sparse 又是另一套尺度。
直接加权求和？不行——尺度不可比，还要调权重 α，换个数据集又得重调。
RRF 的天才之处：只看【排名】，不看分数：
    RRF(d) = Σ  1 / (k + rank_i(d))    k=60
    一条候选在三路里都排第 1，得分最高；只在一路出现、还排第 30，得分很低。
优点：尺度无关、无需训练、10 行代码、鲁棒。这就是面试里"为什么用 RRF"的标准答案。

=== 第 5 环 Rerank（精排）===
RRF 融合后的 Top-50 交给 cross-encoder（BGE-Reranker-v2-M3）逐个 (query, doc) 打分。
为什么需要它（bi-encoder 的死穴）：
    dense 是把 query 和 doc 各自编码后再比，二者"从未见过面"，
    所以分不清"人均500"和"人均80以内"的关系——token 相似就给高分。
    cross-encoder 把 (query, doc) 拼在一起过一遍注意力，能做细粒度交互判断，
    才发现"500>80，这家不该排前面"。代价是慢（O(n) 次前向），所以只对 Top-50 做。
"""
from __future__ import annotations

from .recall import Candidate
from . import config


def rrf_fuse(rankings: list[list[Candidate]], k: int = config.RRF_K) -> list[Candidate]:
    """把多路候选按 RRF 融合成一个排序列表。

    实现要点：
    1) 每路内部先按 score 降序确定名次 rank（1-based）。
    2) 同一 chunk_id 在多路出现时，合并其分数与来源（sources）。
    3) 按 RRF 总分降序输出。
    """
    pools: dict[str, Candidate] = {}
    scores: dict[str, float] = {}

    for ranking in rankings:
        # 关键：每路按自己的分数重排一遍，确定 rank
        ordered = sorted(ranking, key=lambda c: c.score, reverse=True)
        for rank, cand in enumerate(ordered, start=1):
            cid = cand.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in pools:
                # 复制一份，避免修改原对象
                pools[cid] = Candidate(
                    chunk_id=cid, score=0.0, payload=cand.payload, text=cand.text,
                )
            pools[cid].ranks[cand.sources[0]] = rank
            if cand.sources[0] not in pools[cid].sources:
                pools[cid].sources.append(cand.sources[0])
            # 保留任一路里非空的信息（payload/text 理论上各路一致）
            if not pools[cid].text and cand.text:
                pools[cid].text = cand.text

    for cid, cand in pools.items():
        cand.score = scores[cid]

    return sorted(pools.values(), key=lambda c: c.score, reverse=True)


class Reranker:
    """BGE-Reranker-v2-M3 cross-encoder 精排。加载一次，反复用。"""

    def __init__(self, model_path: str = config.RERANKER_PATH):
        from FlagEmbedding import FlagReranker
        # normalize=True 把分数压到 0~1，便于解释与设阈值
        self.model = FlagReranker(model_path, use_fp16=False, normalize=True)

    @staticmethod
    def _doc_text(cand: Candidate) -> str:
        """取用于精排的文本。优先用 chunk text；入库没存 text 时从 payload 现拼。"""
        if cand.text:
            return cand.text
        p = cand.payload
        bits = [p.get("shop_name", "")]
        if p.get("text"):
            bits.append(p["text"])
        else:
            if p.get("cuisine"): bits.append("菜系:" + ",".join(p["cuisine"]))
            if p.get("taste_tags"): bits.append("口味:" + ",".join(p["taste_tags"]))
            if p.get("scene_tags"): bits.append("场景:" + ",".join(p["scene_tags"]))
            if p.get("env_tags"): bits.append("环境:" + ",".join(p["env_tags"]))
            if p.get("price_per_person") is not None: bits.append(f"人均{p['price_per_person']}元")
        return " ".join(str(b) for b in bits if b)

    def rerank(self, query: str, candidates: list[Candidate],
               topn: int = config.RERANK_TOPN,
               dedup_by_shop: bool = False) -> list[Candidate]:
        """对候选逐个 (query, doc) 打分，返回精排后 Top-N。

        dedup_by_shop（推荐默认开启）：
            一家店有约 8 个 child chunk（概览/菜品×N/场景/优缺点…）。若在 chunk 级
            取 Top-N，8 个名额会被 2~3 家店的多个块挤占，店铺覆盖率严重不足。
            开启后：对【全部候选】打分，再按 source_shop_id 去重，每家店只保留
            分数最高的那个 chunk，最后取 Top-N 家【不同的店】。
            评估实测：这一步把 chunk 级 Recall@8 从 0.489 提升到接近 1.0。

            注意去重发生在精排【之后】：先用 cross-encoder 挑出每家店最相关的块，
            再按店取最高分。若先按 RRF 去重，会用融合分（不够准）挑代表块。
        """
        if not candidates:
            return []
        pairs = [(query, self._doc_text(c)) for c in candidates]
        scores = self.model.compute_score(pairs)
        if not isinstance(scores, list):
            scores = [scores]
        for c, sc in zip(candidates, scores):
            c.ranks["rerank"] = float(sc)  # 用 ranks 字典暂存精排分，避免污染 RRF 分
            c.score = float(sc)             # 覆盖为精排分

        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        if not dedup_by_shop:
            return ranked[:topn]

        # 按店去重：每家店保留第一个（=分数最高）的 chunk
        seen_shops: set[str] = set()
        unique: list[Candidate] = []
        for c in ranked:
            shop_id = (c.payload or {}).get("source_shop_id") or c.chunk_id
            if shop_id in seen_shops:
                continue
            seen_shops.add(shop_id)
            unique.append(c)
            if len(unique) >= topn:
                break
        return unique
