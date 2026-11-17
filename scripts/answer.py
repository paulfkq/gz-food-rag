"""端到端入口：检索 -> 生成 -> 输出。一条命令看到 RAG 的完整产出。

用法：
    python scripts/answer.py "西华路人均30以下的肠粉"
    python scripts/answer.py "不要香菜和花生" --compare   # 同时打印模板版和 LLM 版做对比
    python scripts/answer.py "..." --no-llm                # 强制模板版（零 key 也能跑）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieve.pipeline import RetrievalPipeline  # noqa: E402
from generate import AnswerGenerator  # noqa: E402
from generate.llm_client import LLMClient  # noqa: E402


def _warn_if_llm_missing():
    """启动自检：没配 key 时明确提示，避免'为什么没调 LLM'的困惑。"""
    has_key = os.environ.get("RAG_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not has_key:
        print("[提示] 未检测到 RAG_LLM_API_KEY，本次将走【规则解析 + 模板答案】降级。")
        print("       Windows PowerShell 启用 LLM：")
        print('         $env:RAG_LLM_API_KEY="sk-wb-local-8600"')
        print('         $env:RAG_LLM_BASE_URL="http://127.0.0.1:8600/v1"')
        print('         $env:RAG_LLM_MODEL="glm-5.3-flash"')
        print("       （如刻意只要模板版，请忽略本提示 / 使用 --no-llm）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--compare", action="store_true", help="打印模板版 + LLM 版对比")
    args = ap.parse_args()
    q = " ".join(args.query) or "西华路人均30以下的肠粉"

    if not args.no_llm:
        _warn_if_llm_missing()

    pipe = RetrievalPipeline(prefer_llm=not args.no_llm)
    hits, trace = pipe.search(q)
    if trace.need_clarify:
        print(f"[需追问] {trace.clarify_question}")
        return
    gen = AnswerGenerator(client=LLMClient()) if not args.no_llm else AnswerGenerator(
        client=LLMClient.__new__(LLMClient)  # 空客户端 -> 直接模板
    )
    if args.no_llm:
        gen.client._client = None

    print("=" * 74)
    print(f"Q: {q}")
    print(f"检索命中 {len(hits)} 家，槽位: {trace.slots.summary()}")
    print("=" * 74)
    ans = gen.generate(q, hits, trace.slots.summary())
    print(f"[生成模式: {ans.mode} | 校验: {ans.guard_reason} | LLM {ans.llm_ms:.0f}ms]")
    print(ans.text)


if __name__ == "__main__":
    main()
