#!/usr/bin/env python3
"""补跑 Faithfulness 指标（修复 max_tokens 截断后）。

背景：首轮评估时 judge 的 max_tokens=900，faithfulness 要对 7~8 个长上下文
逐条抽 claim（实测输出 2600+ 字符），被截断导致 JSON 解析失败、记为 None。
judge.py 已把 max_tokens 提到 2400。本脚本：
    1. 清除 judge 缓存里 deepseek 命名空间下值为 None 的 faithfulness 项；
    2. 对结果文件里每条已有 ragas 的样本，重算 faithfulness 并回写。
其他三个 RAGAS 指标（本就正常）不触碰。

用法：
    python eval/refill_faithfulness.py --results eval/out/results_v2.jsonl
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from report import load_jsonl
import judge as J

# 强制 judge 用独立模型 + 本地端点，且每条样本新建 client。
# 实测坑：不设 BASE_URL 会走 OpenAI 默认端点导致 Connection error；
# 复用连接的长输出请求会超时；新建 client + 限制 claim 数后稳定在 2~3s/条。
os.environ["RAG_JUDGE_MODEL"] = "deepseek-v4.1-flash"
os.environ.setdefault("RAG_LLM_BASE_URL", "http://127.0.0.1:8600/v1")
os.environ.setdefault("RAG_JUDGE_BASE_URL", "http://127.0.0.1:8600/v1")

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval" / "goldset.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser(description="补跑 Faithfulness")
    ap.add_argument("--results", default=str(ROOT / "eval" / "out" / "results_v2.jsonl"))
    ap.add_argument("--cache", default=str(ROOT / "eval" / "out" / "judge_cache.jsonl"))
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    args = ap.parse_args()

    results_path = Path(args.results)
    cache_path = Path(args.cache)

    # 1) 清理该模型下值为 None 的 faithfulness 缓存
    kept = []
    removed = 0
    if cache_path.exists():
        for line in cache_path.open(encoding="utf-8"):
            if not line.strip():
                continue
            r = __import__("json").loads(line)
            if (r.get("model") == args.model
                    and "::faithfulness::" in r.get("key", "")
                    and r.get("value") is None):
                removed += 1
                continue
            kept.append(line.rstrip("\n"))
    with cache_path.open("w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")
    print(f"[refill] 清除失效 faithfulness 缓存 {removed} 条")

    # 2) 重算：每条新建 judge（新 client），避免复用连接导致的长输出超时
    gold = {g["id"]: g for g in load_jsonl(GOLD)}
    rows = load_jsonl(results_path)
    fixed = 0
    for r in rows:
        rag = r.get("ragas")
        if not rag:
            continue
        j = J.RagasJudge(encoder=None, cache_path=cache_path)
        # 上下文来源：results 里没存 parent_text，改用 goldset 的 gold_contexts
        # （与检索用的 parent_text 同为完整店铺主档、同源格式）。faithfulness 问的是
        # "答案里的陈述能否被店铺资料支持"，用 gold 主档判定是合理近似，且每条
        # gold_contexts 就是该样本 gold 店铺的主档集合，与答案讨论的店铺高度重合。
        g = gold.get(r["sample_id"], {})
        contexts = g.get("gold_contexts") or []
        # 用检索生成的答案文本作为被评估对象
        v = j.faithfulness(r["query"], r.get("answer", ""), contexts)
        if v is not None:
            rag["faithfulness"] = v
            fixed += 1
        print(f"[refill] {r['sample_id']} faithfulness={v}")

    with results_path.open("w", encoding="utf-8") as f:
        import json
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[refill] 回写完成，修复 {fixed}/{sum(1 for r in rows if r.get('ragas'))} 条")


if __name__ == "__main__":
    main()
