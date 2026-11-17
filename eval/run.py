#!/usr/bin/env python3
"""评估运行器：一次加载模型，遍历 Gold Set × 消融配置，逐条落盘结果。

设计要点（面试讲点）：
    1. 【一次加载，多次查询】4.4GB 模型只加载一次（约 23s），否则 40 样本 ×
       5 配置 × 23s 光加载就要 76 分钟。
    2. 【断点续跑】每条结果立即 append 到 JSONL，中断后重跑自动跳过已完成项；
       长任务（纯 CPU）不可能一口气跑完，这是必备工程属性。
    3. 【分层成本】消融配置只跑检索（确定性指标），只有 full 配置才调 LLM
       生成答案 + RAGAS judge —— RAGAS 每条要 4~5 次 LLM 调用，全配置跑
       属于拿 GPU 当柴烧。

用法（先设 LLM 环境变量，否则生成层降级为模板、RAGAS 不可用）：
    python eval/run.py --limit 6 --configs full,dense_only --ragas-limit 4
    python eval/run.py --configs full,no_rerank,no_hard_filter,dense_only,rule_slots --ragas
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "eval"))

from retrieve import config as RCFG  # noqa: E402
from retrieve.pipeline import RetrievalPipeline  # noqa: E402
from generate.answer import AnswerGenerator  # noqa: E402
import metrics as M  # noqa: E402
from judge import RagasJudge  # noqa: E402

# 消融配置：defaults 全开；每个配置只改一个变量，保证对比可归因
ABLATIONS: dict[str, dict] = {
    "full":           {"prefer_llm": True,  "ablation": {}},
    "no_rerank":      {"prefer_llm": True,  "ablation": {"use_rerank": False}},
    "no_hard_filter": {"prefer_llm": True,  "ablation": {"use_hard_filter": False}},
    "dense_only":     {"prefer_llm": True,  "ablation": {"recall_routes": ["dense"]}},
    "rule_slots":     {"prefer_llm": False, "ablation": {}},
}


def load_samples(path: Path, limit: int | None) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    return rows[:limit] if limit else rows


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    r = json.loads(line)
                    done.add(f"{r['sample_id']}::{r['config']}")
                except Exception:  # noqa: BLE001
                    pass
    return done


def evaluate_one(pipe, generator, judge, sample: dict, cfg_name: str,
                 cfg: dict, topn: int, do_ragas: bool) -> dict:
    t0 = time.time()
    query = sample["query"]
    hits, trace = pipe.search(
        query, topn=topn,
        prefer_llm=cfg["prefer_llm"],
        ablation=cfg["ablation"],
    )
    latency_ms = (time.time() - t0) * 1000

    row: dict = {
        "sample_id": sample["id"],
        "config": cfg_name,
        "query": query,
        "type": sample["type"],
        "expect_clarify": sample["expect_clarify"],
        "clarify_hit": bool(trace.need_clarify),
        "retrieved_ids": [h.shop_id for h in hits],
        "retrieved_names": [h.shop_name for h in hits],
        "hits": [{"shop_id": h.shop_id, "payload": h.payload,
                  "score": round(h.score, 4)} for h in hits],
        "latency_ms": round(latency_ms, 1),
        "recall_counts": trace.recall_counts,
    }

    if sample["expect_clarify"]:
        return row

    sha, sta = M.slot_field_accuracy(sample)
    row["metrics"] = {
        "recall": M.recall_at_k(row["retrieved_ids"], sample["gold_shop_ids"], topn),
        "precision": M.precision_at_k(row["retrieved_ids"], sample["gold_shop_ids"], topn),
        "mrr": M.mrr(row["retrieved_ids"], sample["gold_shop_ids"]),
        "hit_rate": M.hit_rate_at_k(row["retrieved_ids"], sample["gold_shop_ids"], topn),
        "ndcg": M.ndcg_at_k(row["retrieved_ids"], sample["gold_shop_ids"], topn),
        "hard_compliance": M.hard_compliance(row["hits"], sample["constraints"]),
        "slot_acc_hit": sha,
        "slot_acc_total": sta,
    }

    # 只有 full 配置才生成答案 + 跑 RAGAS（成本控制，见模块 docstring）
    if cfg_name == "full" and generator is not None:
        contexts = [h.parent_text for h in hits]
        ans = generator.generate(query, hits, trace.slots.summary() if trace.slots else "")
        row["answer"] = ans.text
        row["answer_mode"] = ans.mode
        row["guard_reason"] = ans.guard_reason
        row["metrics"]["entity_recall"] = M.entity_recall(ans.text, sample["gold_shop_names"])
        if do_ragas and judge is not None and hits:
            row["ragas"] = judge.evaluate(sample, ans.text, contexts)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="广州美食 RAG 评估运行器")
    ap.add_argument("--goldset", default=str(ROOT / "eval" / "goldset.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "out" / "results.jsonl"))
    ap.add_argument("--configs", default="full", help="逗号分隔的消融配置名")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条样本（调试用）")
    ap.add_argument("--topn", type=int, default=8)
    ap.add_argument("--ragas", action="store_true", help="对 full 配置跑 RAGAS 四指标")
    ap.add_argument("--ragas-limit", type=int, default=12, help="RAGAS 只跑前 N 条（默认12，成本考虑）")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存，全部重跑")
    args = ap.parse_args()

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    bad = [c for c in configs if c not in ABLATIONS]
    if bad:
        raise SystemExit(f"未知配置 {bad}，可选: {list(ABLATIONS)}")

    samples = load_samples(Path(args.goldset), args.limit)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and out_path.exists():
        out_path.unlink()  # --fresh 必须真清空，否则新结果 append 到旧结果后造成重复计数
    done = set() if args.fresh else load_done(out_path)
    ragas_ids = set(s["id"] for s in samples[:args.ragas_limit])

    print(f"[run] 样本={len(samples)} 配置={configs} 断点已完成={len(done)}", flush=True)
    print("[run] 加载模型（约 20s）...", flush=True)
    pipe = RetrievalPipeline(collection=RCFG.COLLECTION)
    import os
    has_llm = bool(os.environ.get("RAG_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    generator = AnswerGenerator() if has_llm else None
    judge = RagasJudge(encoder=pipe.encoder) if (args.ragas and has_llm) else None
    if args.ragas and not has_llm:
        print("[run] 未设 LLM key -> 跳过 RAGAS", flush=True)

    total = len(samples) * len(configs)
    cur = 0
    for cfg_name in configs:
        cfg = ABLATIONS[cfg_name]
        for s in samples:
            cur += 1
            key = f"{s['id']}::{cfg_name}"
            if key in done:
                print(f"[{cur}/{total}] {key} 已存在，跳过", flush=True)
                continue
            do_ragas = (args.ragas and cfg_name == "full"
                        and s["id"] in ragas_ids and judge is not None)
            row = evaluate_one(pipe, generator, judge, s, cfg_name, cfg, args.topn, do_ragas)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            m = row.get("metrics", {})
            print(f"[{cur}/{total}] {key}  hit={row.get('clarify_hit') if s['expect_clarify'] else m.get('recall')} "
                  f"lat={row['latency_ms']:.0f}ms" + (" +RAGAS" if do_ragas else ""), flush=True)

    print(f"[run] 完成，结果 -> {out_path.resolve()}", flush=True)


if __name__ == "__main__":
    main()
