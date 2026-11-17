"""检索层第 3 环：三路召回（dense + sparse + BM25）。

一句话职责：用三种"不同的看法"各自找出一批候选店，然后交给下一环融合。

三路分别是什么、为什么都要（新手必读）：
    1) dense（稠密向量）：
       把 query 和文档各自编码成一个 1024 维向量，比"方向接近程度"（cosine）。
       擅长：语义相似。"安静适合约会的地方" 能召回没写过"约会"但写过"灯光柔和"的店。
       弱点：对专名、数值、否定不敏感（"人均30"的 30 在向量里几乎没意义）。

    2) sparse（BGE-M3 lexical_weights）：
       模型学出的"词 -> 权重"稀疏向量，本质是"这个词对这段文本有多重要"。
       擅长：关键词级匹配，且比传统 BM25 多一点语义扩展（模型词表）。
       和 dense 来自同一个模型，一次编码同时产出，几乎零额外成本。

    3) BM25（jieba 分词 + rank_bm25）：
       传统倒排索引的统计打分（词频 TF × 逆文档频率 IDF）。
       擅长：粤语专名/新词/店名的精确命中，如"啫啫煲""泮塘马蹄糕"。
       为什么说它和 sparse 不重复：sparse 是模型词表上的权重，遇到没登录的
       新词/方言词可能失效；jieba + 自定义词典是确定性的，能兜住这些长尾。
       三者错误模式不同 -> 融合后更稳（这是面试能讲出深度的点）。

关于硬过滤：dense 和 sparse 两路把 compile_filter 的结果传给 Qdrant 做 pre-filter；
BM25 是内存索引不过 Qdrant，所以召回后用 verify_hard 补一道复核（见 pipeline）。
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field

import numpy as np
from qdrant_client import QdrantClient, models

from . import config


@dataclass
class Candidate:
    """一路召回的一个结果（统一结构，方便后面融合）。"""
    chunk_id: str
    score: float
    payload: dict
    text: str = ""
    ranks: dict[str, int] = field(default_factory=dict)  # 各路里的名次，RRF 用
    sources: list[str] = field(default_factory=list)      # 被哪几路命中


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


class Encoder:
    """把 BGE-M3 包一层，统一 query 的 dense/sparse 编码。加载一次，反复用。"""

    def __init__(self, model_path: str = config.BGE_M3_PATH):
        from FlagEmbedding import BGEM3FlagModel
        self.model = BGEM3FlagModel(model_path, use_fp16=False)

    def encode_query(self, text: str) -> tuple[list[float], models.SparseVector]:
        out = self.model.encode(
            [text], return_dense=True, return_sparse=True, return_colbert_vecs=False)
        dv = l2_normalize(np.asarray(out["dense_vecs"][0], dtype=np.float32)).tolist()
        raw = out["lexical_weights"][0]  # dict: token_id(str) -> weight
        sv = {int(k): float(v) for k, v in raw.items() if float(v) > 0}
        sparse = models.SparseVector(indices=list(sv.keys()), values=list(sv.values()))
        return dv, sparse


def dense_recall(client: QdrantClient, dense_vec: list[float],
                 qfilter: models.Filter | None, topk: int = config.DENSE_TOPK,
                 collection: str = config.COLLECTION) -> list[Candidate]:
    """路 1：dense 向量检索，带 pre-filter。"""
    resp = client.query_points(
        collection_name=collection,
        query=models.NearestQuery(nearest=dense_vec),
        using="dense",
        query_filter=qfilter,
        limit=topk,
        with_payload=True,
    )
    return [_to_candidate(p, "dense") for p in resp.points]


def sparse_recall(client: QdrantClient, sparse_vec: models.SparseVector,
                  qfilter: models.Filter | None, topk: int = config.SPARSE_TOPK,
                  collection: str = config.COLLECTION) -> list[Candidate]:
    """路 2：BGE-M3 sparse（lexical）检索，带 pre-filter。"""
    resp = client.query_points(
        collection_name=collection,
        query=models.NearestQuery(nearest=sparse_vec),
        using="sparse",
        query_filter=qfilter,
        limit=topk,
        with_payload=True,
    )
    return [_to_candidate(p, "sparse") for p in resp.points]


def _to_candidate(point, source: str) -> Candidate:
    p = point.payload or {}
    return Candidate(
        chunk_id=p.get("chunk_id", str(point.id)),
        score=float(point.score),
        payload=p,
        text=p.get("text", ""),  # 若入库未放 text，这里为空，Rerank 会用 payload 现拼
        ranks={source: 0},
        sources=[source],
    )


# ---------------------------------------------------------------------------
# BM25 路：内存倒排索引（Qdrant 不负责 BM25）
# ---------------------------------------------------------------------------

class BM25Index:
    """用 jieba 分词 + rank_bm25 建的内存 BM25 索引。

    为什么不在 Qdrant 里做：Qdrant 的 sparse 是模型权重，不是 BM25 的 TF-IDF。
    学习项目里 2388 条在内存建索引只要几十毫秒，还能 pickle 缓存，简单可控。
    数据量上千万时才会换成 Elasticsearch / Qdrant 的 sparse 直接做 BM25。

    注意：BM25 的检索文本我们用 child 的 text，和向量用同一份，保证可比。
    """

    def __init__(self, chunks_path=None, cache_path=None):
        self.chunks_path = chunks_path or config.CHUNKS_PATH
        self.cache_path = cache_path or config.BM25_CACHE_PATH
        self.bm25 = None
        self.chunks: list[dict] = []
        self.doc_tokens: list[list[str]] = []
        self._setup_jieba()

    @staticmethod
    def _setup_jieba():
        """加载自定义词典：广州地名/菜名，避免 jieba 把"啫啫煲"切碎。"""
        import jieba
        words = ["啫啫煲", "泮塘", "马蹄糕", "艇仔粥", "肠粉", "早茶", "烧鹅",
                 "叉烧包", "流沙包", "云吞面", "牛杂", "西华路", "上下九",
                 "珠江新城", "江南西", "东山口", "沙面", "镬气"]
        for w in words:
            jieba.add_word(w)
        # 关掉日志，避免刷屏
        jieba.setLogLevel(60)

    def build(self, force: bool = False) -> None:
        """建索引或读缓存。"""
        if not force and self.cache_path.exists():
            try:
                self._load_cache()
                return
            except Exception:
                pass  # 缓存坏了就重建
        import json
        import jieba
        from rank_bm25 import BM25Okapi

        self.chunks = []
        with open(self.chunks_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    c = json.loads(line)
                    if c.get("chunk_type") != "parent":
                        self.chunks.append(c)
        self.doc_tokens = [list(jieba.cut(c["text"])) for c in self.chunks]
        self.bm25 = BM25Okapi(self.doc_tokens, k1=1.5, b=0.75)
        self._save_cache()

    def _save_cache(self):
        with open(self.cache_path, "wb") as f:
            pickle.dump({"chunks": self.chunks, "doc_tokens": self.doc_tokens}, f)

    def _load_cache(self):
        with open(self.cache_path, "rb") as f:
            d = pickle.load(f)
        self.chunks, self.doc_tokens = d["chunks"], d["doc_tokens"]
        from rank_bm25 import BM25Okapi
        self.bm25 = BM25Okapi(self.doc_tokens, k1=1.5, b=0.75)

    def search(self, query: str, topk: int = config.BM25_TOPK) -> list[Candidate]:
        """BM25 检索。BM25 分数无上界，这里只保留 >0 的。"""
        import jieba
        tokens = list(jieba.cut(query))
        scores = self.bm25.get_scores(tokens)
        idx = np.argsort(-scores)[:topk]
        out = []
        for i in idx:
            if scores[i] <= 0:
                continue
            c = self.chunks[i]
            p = dict(c.get("payload") or {})
            # chunk_id / text / parent_id 在 chunk 顶层，BM25 路要手动塞回 payload，
            # 保证和 dense/sparse 两路的 Candidate.payload 结构一致（融合时按 chunk_id 对齐）
            p.setdefault("chunk_id", c.get("chunk_id"))
            p.setdefault("parent_id", c.get("parent_id"))
            p.setdefault("shop_name", p.get("shop_name", ""))
            out.append(Candidate(
                chunk_id=c["chunk_id"], score=float(scores[i]), payload=p,
                text=c["text"], ranks={"bm25": 0}, sources=["bm25"],
            ))
        return out
