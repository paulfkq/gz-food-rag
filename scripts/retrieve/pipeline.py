"""检索层总控：把 6 个环节串成一条流水线，并记录完整 trace。

六步流水线（对应方案 §2 在线链路 B1~B10）：
    Query
      -> ① 槽位解析        slots.parse_slots
      -> ② 编译 Filter      filter_dsl.compile_filter
      -> ③ 三路召回         recall.dense/sparse/bm25
      -> ④ RRF 融合         fusion.rrf_fuse
      -> ⑤ Rerank 精排      fusion.Reranker
      -> ⑥ 硬约束复核+回溯  verify.verify_and_expand
      -> 最终 Top-N Parent

设计成"重资源只加载一次"：Encoder / Reranker / BM25 / ParentStore 都是启动时加载，
之后每次查询复用。否则每条 query 都加载 2.2GB 权重会慢到不能用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from qdrant_client import QdrantClient

from . import config
from .slots import parse_slots, SearchSlots
from .filter_dsl import compile_filter, verify_hard
from .recall import Encoder, BM25Index, dense_recall, sparse_recall
from .fusion import rrf_fuse, Reranker
from .verify import ParentStore, verify_and_expand, FinalHit


@dataclass
class Trace:
    """可观测性：记录每一步的中间结果，出问题能定位是哪一环丢了召回。"""
    query: str
    slots: SearchSlots | None = None
    filter_dump: dict | None = None
    recall_counts: dict[str, int] = field(default_factory=dict)
    rrf_top: list[dict] = field(default_factory=list)
    rerank_top: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    need_clarify: bool = False
    clarify_question: str | None = None


class RetrievalPipeline:
    def __init__(self, build_bm25: bool = True, prefer_llm: bool = True,
                 collection: str = config.COLLECTION):
        t0 = time.time()
        print("[init] 加载 Qdrant ...", flush=True)
        self.client = QdrantClient(path=str(config.QDRANT_PATH))
        self.collection = collection
        print("[init] 加载 BGE-M3 encoder ...", flush=True)
        self.encoder = Encoder()
        print("[init] 加载 BGE-Reranker ...", flush=True)
        self.reranker = Reranker()
        print("[init] 加载 BM25 索引 ...", flush=True)
        self.bm25 = BM25Index()
        if build_bm25:
            self.bm25.build()
        print("[init] 加载 Parent store ...", flush=True)
        self.parents = ParentStore()
        self.prefer_llm = prefer_llm
        print(f"[init] 就绪，耗时 {time.time()-t0:.0f}s", flush=True)

    def search(self, query: str, topn: int = config.RERANK_TOPN,
               now_hour: int | None = None,
               prefer_llm: bool | None = None,
               ablation: dict | None = None) -> tuple[list[FinalHit], Trace]:
        """执行一次检索。

        prefer_llm 是【并发安全的 per-request 参数】：
          - None（默认）：沿用构造时的 self.prefer_llm —— CLI 单次调用无需改动；
          - True/False：仅本次请求生效，不写回实例状态。

        为什么重点做这个改造：服务层 lifespan 里 pipe 是单例，4.4GB 模型只有一份。
        早版用 `old = pipe.prefer_llm; pipe.prefer_llm = X; finally: 恢复` 的写法，
        两个并发请求会互相覆盖（A 设 False、B 设 True，A 结束时恢复的值污染 B），
        是真实竞态。改成参数传递后，每次调用自带配置，共享的只有只读模型/索引，
        并发天然安全 —— 这是允许多会话并行的前置条件。

        ablation 是【评估专用】的 per-request 消融开关（默认全开=线上行为）：
          - use_rerank: False 跳过 BGE-Rerank，直接用 RRF 顺序
          - use_hard_filter: False 关闭 pre-filter + 后置复核（观察硬约束的价值）
          - recall_routes: 只启用指定召回路，如 ["dense"]（观察多路召回的价值）
          - shop_dedup: False 关闭店铺级去重，退回 chunk 级 Top-N（观察该优化的价值）
        关硬约束时用空 SearchSlots 编译 filter/复核，但 trace.slots 仍保留原解析
        结果，便于分析"解析对了但没执行"造成的指标差异。
        """
        trace = Trace(query=query)
        ab = ablation or {}
        use_rerank = ab.get("use_rerank", True)
        use_hard_filter = ab.get("use_hard_filter", True)
        shop_dedup = ab.get("shop_dedup", True)  # 优化默认开启；消融可关掉对比
        routes = ab.get("recall_routes", ["dense", "sparse", "bm25"])
        t = time.time()

        # ① 槽位解析：本次请求的偏好优先，未指定才回退到实例默认值
        use_llm = self.prefer_llm if prefer_llm is None else prefer_llm
        slots = parse_slots(query, prefer_llm=use_llm)
        trace.slots = slots
        filter_slots = slots if use_hard_filter else SearchSlots()
        trace.need_clarify = slots.need_clarify
        trace.clarify_question = slots.clarify_question
        trace.timings_ms["1_slots"] = (time.time() - t) * 1000
        if slots.need_clarify:
            return [], trace  # 条件冲突时不硬凑答案，先追问（产品成熟度体现）

        # ② 编译 Filter
        t = time.time()
        qfilter = compile_filter(filter_slots, now_hour=now_hour)
        trace.filter_dump = qfilter.model_dump() if qfilter else None
        trace.timings_ms["2_filter"] = (time.time() - t) * 1000

        # ③ 三路召回
        t = time.time()
        dense_vec, sparse_vec = self.encoder.encode_query(query)
        dense = dense_recall(self.client, dense_vec, qfilter)
        sparse = sparse_recall(self.client, sparse_vec, qfilter)
        bm25_raw = self.bm25.search(query)
        # 【关键坑】BM25 在内存里检索，没有经过 Qdrant 的 pre-filter！
        # 不过滤的话，含花生的店会混进融合，Rerank 后又可能被复核剔光 -> 结果为空。
        # 所以 BM25 召回后必须立刻用同一套 verify_hard 过滤（与后置复核同源）。
        bm25 = []
        for c in bm25_raw:
            ok, _ = verify_hard(c.payload, filter_slots, now_hour=now_hour)
            if ok:
                bm25.append(c)
        trace.recall_counts = {"dense": len(dense), "sparse": len(sparse),
                               "bm25": len(bm25), "bm25_raw": len(bm25_raw)}
        trace.timings_ms["3_recall"] = (time.time() - t) * 1000

        # ④ RRF 融合
        t = time.time()
        _routes: dict[str, list] = {"dense": dense, "sparse": sparse, "bm25": bm25}
        fused = rrf_fuse([_routes[r] for r in routes if r in _routes])
        trace.rrf_top = [
            {"shop": c.payload.get("shop_name"), "rrf": round(c.score, 4), "sources": c.sources}
            for c in fused[:10]
        ]
        trace.timings_ms["4_rrf"] = (time.time() - t) * 1000

        # ⑤ Rerank 精排（对全部 RRF 候选打分，再按店去重取 Top-N 家不同的店）
        t = time.time()
        if use_rerank:
            reranked = self.reranker.rerank(
                query, fused[:config.RRF_TOPN], topn=topn, dedup_by_shop=shop_dedup)
        elif shop_dedup:
            # 无精排但保留店铺去重：按 RRF 顺序取每家店最高分块（观察"仅去重"的价值）
            seen, uniq = set(), []
            for c in fused:
                sid = (c.payload or {}).get("source_shop_id") or c.chunk_id
                if sid in seen:
                    continue
                seen.add(sid)
                uniq.append(c)
                if len(uniq) >= topn:
                    break
            reranked = uniq
        else:
            reranked = fused[:topn]  # 消融：既无精排也无去重，纯 RRF chunk 顺序
        trace.rerank_top = [
            {"shop": c.payload.get("shop_name"), "score": round(c.score, 4),
             "sources": c.sources, "ranks": c.ranks}
            for c in reranked
        ]
        trace.timings_ms["5_rerank"] = (time.time() - t) * 1000

        # ⑥ 硬约束复核 + Parent 回溯
        t = time.time()
        final, rejected = verify_and_expand(reranked, filter_slots, self.parents, now_hour=now_hour)
        trace.rejected = rejected
        trace.timings_ms["6_verify"] = (time.time() - t) * 1000

        return final, trace
