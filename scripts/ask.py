"""检索层命令行入口：一条命令跑通六步流水线，打印结果 + trace。

用法：
    # 交互模式（推荐，可连续问）
    python scripts/ask.py

    # 单次查询
    python scripts/ask.py "西华路人均30以下的肠粉"

    # 看完整 trace
    python scripts/ask.py "不要香菜和花生" --trace
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieve.pipeline import RetrievalPipeline  # noqa: E402


def show(hits, trace, verbose: bool):
    if trace.need_clarify:
        print(f"\n[需追问] {trace.clarify_question}")
        return
    print(f"\n槽位: {trace.slots.summary()}")
    print(f"召回: dense={trace.recall_counts.get('dense')} sparse={trace.recall_counts.get('sparse')} "
          f"bm25={trace.recall_counts.get('bm25')}")
    if trace.rejected:
        print(f"复核剔除 {len(trace.rejected)} 条")
    print("-" * 70)
    if not hits:
        print("没有符合条件的店。")
        return
    for i, h in enumerate(hits, 1):
        print(f"{i}. {h.shop_name}  [{h.shop_id}]  精排分={h.score:.3f}")
        soft = ("命中软偏好:" + ",".join(h.matched_soft)) if h.matched_soft else ""
        print(f"   {soft}")
        if verbose:
            first = h.parent_text.split("\n")[0]
            print(f"   {first[:80]}")
    if verbose:
        print("\n===== TRACE =====")
        import json
        print(json.dumps({
            "slots": trace.slots.summary(),
            "filter": trace.filter_dump,
            "recall_counts": trace.recall_counts,
            "rrf_top": trace.rrf_top,
            "rerank_top": trace.rerank_top,
            "rejected": trace.rejected,
            "timings_ms": {k: round(v, 1) for k, v in trace.timings_ms.items()},
        }, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*", help="查询，留空进入交互模式")
    ap.add_argument("--trace", action="store_true", help="打印完整 trace")
    ap.add_argument("--no-llm", action="store_true", help="强制只用规则槽位解析")
    args = ap.parse_args()

    pipe = RetrievalPipeline(prefer_llm=not args.no_llm)
    if args.query:
        q = " ".join(args.query)
        hits, trace = pipe.search(q)
        show(hits, trace, args.trace)
    else:
        print("进入交互模式，输入 q 退出。")
        while True:
            q = input("\n> ").strip()
            if q.lower() in ("q", "quit", "exit"):
                break
            if not q:
                continue
            hits, trace = pipe.search(q)
            show(hits, trace, args.trace)


if __name__ == "__main__":
    main()
