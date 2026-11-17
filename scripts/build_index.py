"""把 Parent-Child chunk 用 BGE-M3 编码后写入 Qdrant（方案 §3.4 / §4.1 / §4.4）。

流程：
1. 读 data/chunks/chunks.jsonl
2. 只把 Child 向量化入库（Parent 作为 parent store 留在 jsonl，命中后回溯）
3. BGE-M3 一次编码同时产出 dense(1024, L2 归一化) + sparse(词级权重)
4. Qdrant embedded 模式建 collection：named dense + sparse、payload index、geo index
5. 分批 upsert，写入 model_version 便于后续换模型对照

运行（用装好依赖的 venv）：
    python scripts/build_index.py \
      --chunks data/chunks/chunks.jsonl \
      --qdrant data/qdrant \
      --batch-size 16 --limit 0

关键实现细节（面试讲点）：
- dense 必须 L2 归一化，否则 cosine 距离 ≠ 内积，检索分数失真。
- sparse 是 BGE-M3 学出的词级权重，与 jieba BM25 的错误模式不同，二者互补。
- pre-filter 能生效的前提：每个 Child payload 都冗余了硬约束字段（chunker 已做）。
- Qdrant embedded 模式下 payload index 不生效（走精确扫描），但过滤结果正确；
  生产换 Docker 版 Qdrant 即可无缝享受索引加速。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import numpy as np


def point_id(chunk_id: str) -> int:
    """字符串 chunk_id -> Qdrant 无符号 64 位整数 id（稳定可复现）。"""
    return int.from_bytes(hashlib.sha256(chunk_id.encode("utf-8")).digest()[:8], "big")


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


# Qdrant payload 索引字段：会被 pre-filter 用到的都要建索引
PAYLOAD_INDEXES: list[tuple[str, Any]] = []


def _index_specs(models):
    return [
        ("district", models.PayloadSchemaType.KEYWORD),
        ("business_area", models.PayloadSchemaType.KEYWORD),
        ("cuisine", models.PayloadSchemaType.KEYWORD),
        ("allergens", models.PayloadSchemaType.KEYWORD),
        ("avoid_ingredients", models.PayloadSchemaType.KEYWORD),
        ("dietary_flags", models.PayloadSchemaType.KEYWORD),
        ("noise_level", models.PayloadSchemaType.KEYWORD),
        ("metro_lines", models.PayloadSchemaType.KEYWORD),
        ("scene_tags", models.PayloadSchemaType.KEYWORD),
        ("env_tags", models.PayloadSchemaType.KEYWORD),
        ("price_per_person", models.PayloadSchemaType.INTEGER),
        ("spiciness", models.PayloadSchemaType.INTEGER),
        ("rating", models.PayloadSchemaType.FLOAT),
        ("queue_min", models.PayloadSchemaType.INTEGER),
        ("metro_walk_min", models.PayloadSchemaType.INTEGER),
        ("private_room", models.PayloadSchemaType.BOOL),
        ("parking", models.PayloadSchemaType.BOOL),
        ("reservation", models.PayloadSchemaType.BOOL),
        ("takeaway", models.PayloadSchemaType.BOOL),
        ("is_late_night", models.PayloadSchemaType.BOOL),
        ("is_dim_sum", models.PayloadSchemaType.BOOL),
        ("chunk_type", models.PayloadSchemaType.KEYWORD),
        ("source_shop_id", models.PayloadSchemaType.KEYWORD),
        ("geo", models.PayloadSchemaType.GEO),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="data/chunks/chunks.jsonl")
    ap.add_argument("--qdrant", default="data/qdrant")
    ap.add_argument("--collection", default="gz_food_chunks")
    ap.add_argument("--model", default="D:/向量模型下载与部署/models/BAAI/bge-m3",
                    help="BGE-M3 模型名或本地目录")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="只索引前 N 个 child，0=全部；冒烟用")
    ap.add_argument("--recreate", action="store_true", help="删除并重建 collection")
    args = ap.parse_args()

    from FlagEmbedding import BGEM3FlagModel
    from qdrant_client import QdrantClient, models

    # --model 既可为 HF repo id，也可为本地目录（本机权重已下载到本地）。
    # 本地目录直接传给 FlagEmbedding，不触发任何联网检查。
    model_path = Path(args.model)
    if model_path.exists():
        print(f"[加载] BGE-M3 本地权重：{model_path}", flush=True)
    else:
        print(f"[加载] BGE-M3 模型名（在线/缓存）：{args.model}", flush=True)

    t0 = time.time()
    model = BGEM3FlagModel(str(model_path if model_path.exists() else args.model), use_fp16=False)
    print(f"[加载] 完成，耗时 {time.time()-t0:.0f}s", flush=True)

    chunks = [json.loads(l) for l in Path(args.chunks).read_text(encoding="utf-8").splitlines() if l.strip()]
    children = [c for c in chunks if c["chunk_type"] != "parent"]
    if args.limit:
        children = children[: args.limit]
    print(f"[数据] 总 chunk {len(chunks)}，待索引 Child {len(children)}", flush=True)

    qpath = Path(args.qdrant)
    qpath.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(qpath))

    if args.recreate and client.collection_exists(args.collection):
        client.delete_collection(args.collection)
    if not client.collection_exists(args.collection):
        client.create_collection(
            collection_name=args.collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=1024, distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=200),
                )
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(index=models.SparseIndexParams(on_disk=False))
            },
        )
        for key, schema in _index_specs(models):
            client.create_payload_index(collection_name=args.collection, field_name=key, field_schema=schema)
        print("[Qdrant] collection 与 payload/geo 索引已创建", flush=True)

    t1 = time.time()
    done = 0
    for start in range(0, len(children), args.batch_size):
        batch = children[start : start + args.batch_size]
        texts = [c["text"] for c in batch]
        out = model.encode(texts, return_dense=True, return_sparse=True, return_colbert_vecs=False,
                           max_length=512)
        # FlagEmbedding 1.4.2 的键名是 dense_vecs / lexical_weights
        dense_vecs = out["dense_vecs"]
        sparse_vecs = out["lexical_weights"]
        points = []
        for c, dvec, svec in zip(batch, dense_vecs, sparse_vecs):
            dv = l2_normalize(np.asarray(dvec, dtype=np.float32)).tolist()
            sv = {int(k): float(v) for k, v in svec.items() if float(v) > 0}
            points.append(models.PointStruct(
                id=point_id(c["chunk_id"]),
                vector={"dense": dv, "sparse": models.SparseVector(
                    indices=list(sv.keys()), values=list(sv.values()))},
                payload=c["payload"],
            ))
        client.upsert(collection_name=args.collection, points=points, wait=True)
        done += len(points)
        if done % 200 < args.batch_size or done == len(children):
            print(f"[索引] {done}/{len(children)}  用时 {time.time()-t1:.0f}s", flush=True)

    info = client.get_collection(args.collection)
    print(f"[完成] 索引 {done} 个 Child，collection 现有点数 {info.points_count}", flush=True)
    print(f"[提示] Parent 保留在 {args.chunks}（chunk_type=parent），命中 Child 后按 parent_id 回溯", flush=True)


if __name__ == "__main__":
    main()
