#!/usr/bin/env python3
"""RAGAS 四指标（按论文定义自实现，用本地 LLM 做 judge）。

为什么自实现而不 pip install ragas（面试讲点）：
    1. 本环境 openai==3.51（旧版），ragas 新版本依赖 openai>=1.x 与一整套
       langchain 生态，强装会破坏已跑通的Retrieval/Generation链路；
    2. RAGAS 的价值在于「指标定义」而非库本身 —— 四个指标的核心算法都很短，
       自己实现才能讲清 Faithfulness 是 claim 级校验、Context Precision 是
       rank-weighted、Answer Relevancy 是反向生成+相似度，而不是黑盒调 API；
    3. 自带缓存与 JSON 健壮解析，judge 结果可按样本复用，避免重复烧 CPU。

四指标（Es et al., RAGAS 2023）：
    Faithfulness      = 答案中能被上下文支持的 claim 数 / 总 claim 数
    Context Precision = Σ(precision@k × rel@k) / Σrel@k    （按排序位置加权）
    Context Recall    = 参考答案中能归因到上下文的 claim 数 / 总 claim 数
    Answer Relevancy  = mean(cosine(query, 由答案反向生成的 N 个问题))
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from generate.llm_client import LLMClient, LLMConfig  # noqa: E402

SYS = "你是一个严谨的 RAG 系统评估专家。只输出合法 JSON，不要输出解释、Markdown 围栏或多余文字。"


def parse_json(text: str | None) -> dict | None:
    """LLM 输出健壮解析：去围栏、截取首尾花括号。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:  # noqa: BLE001
        return None


def _as_bool(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


class RagasJudge:
    def __init__(self, encoder=None, cache_path: Path | None = None):
        # 【独立 judge 模型】默认 deepseek-v4.1-flash，与生成端 glm 分开，
        # 避免"同一模型既当运动员又当裁判"的自评偏差（生成答案的模型和打分
        # 的模型若是同一个，会系统性偏袒自己的输出）。
        # 可用 RAG_JUDGE_* 覆盖；不设时回退到 RAG_LLM_*（向后兼容旧行为）。
        import os
        cfg = LLMConfig(
            api_key=(os.environ.get("RAG_JUDGE_API_KEY")
                     or os.environ.get("RAG_LLM_API_KEY")
                     or os.environ.get("OPENAI_API_KEY", "")),
            base_url=(os.environ.get("RAG_JUDGE_BASE_URL")
                      or os.environ.get("RAG_LLM_BASE_URL")),
            model=(os.environ.get("RAG_JUDGE_MODEL")
                   or os.environ.get("RAG_LLM_MODEL", "glm-5.3-flash")),
        )
        # faithfulness 限制抽最多 8 个关键 claim（见该方法 prompt），输出很短；
        # 1200 tokens 足够。之前不限制 claim 数时输出 2600+ 字符，长输出在
        # 本地 LLM 服务上会超时（实测 96s 后 APIConnectionError）导致 None。
        cfg.max_tokens = 1200
        cfg.timeout = 120.0
        cfg.retries = 1
        self.model = cfg.model
        self.client = LLMClient(cfg)
        self.encoder = encoder  # 复用 pipeline 已加载的 BGE-M3，避免重复占 2GB 内存
        self.cache_path = Path(cache_path) if cache_path else ROOT / "eval" / "out" / "judge_cache.jsonl"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 【缓存 key 带模型名】换 judge 模型后自动失效旧缓存，否则换了模型
        # 仍命中 glm 时代的打分，等于没换。不同模型的判断分开存、互不污染。
        self.cache_ns = f"{self.model}::"
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            with self.cache_path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            r = json.loads(line)
                            self.cache[r["key"]] = r
                        except Exception:  # noqa: BLE001
                            pass

    def _ask_json(self, system: str, user: str) -> dict | None:
        return parse_json(self.client.chat(system, user))

    def _cached(self, key: str, fn):
        full_key = self.cache_ns + key
        if full_key in self.cache:
            return self.cache[full_key]["value"]
        value = fn()
        with self.cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": full_key, "value": value,
                                "model": self.model}, ensure_ascii=False) + "\n")
        self.cache[full_key] = {"key": full_key, "value": value, "model": self.model}
        return value

    @staticmethod
    def _ctx_block(contexts: list[str]) -> str:
        return "\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts)) or "（无参考资料）"

    def faithfulness(self, query: str, answer: str, contexts: list[str]) -> float | None:
        """答案里的事实陈述，有多少能被参考资料支持。"""
        def calc() -> float | None:
            d = self._ask_json(SYS, f"""请从【答案】中抽取最多 8 个原子事实陈述
（每条只表达一个事实，优先店名/价格/评分/菜系等关键信息），
逐条判断该陈述是否能由【参考资料】直接支持或合理推断。

【问题】{query}
【答案】{answer}
【参考资料】
{self._ctx_block(contexts)}

输出格式：{{"claims":[{{"text":"陈述原文","supported":true或false}}]}}""")
            if not d or not isinstance(d.get("claims"), list) or not d["claims"]:
                return None
            return sum(_as_bool(x.get("supported")) for x in d["claims"]) / len(d["claims"])
        return self._cached(f"faithfulness::{query}::{answer}", calc)

    def context_precision(self, query: str, contexts: list[str]) -> float | None:
        """检索到的上下文按排序位置加权的相关性（RAGAS 原公式）。"""
        def calc() -> float | None:
            d = self._ask_json(SYS, f"""判断下面每一条【参考资料】是否包含回答【问题】所需的信息
（哪怕只是部分相关信息，如店名、区域、价格、菜系等都算相关）。

【问题】{query}
【参考资料】
{self._ctx_block(contexts)}

输出格式：{{"relevance":[1,0,1]}}，数组长度和顺序必须与参考资料编号一致，1=相关，0=无关。""")
            if not d or not isinstance(d.get("relevance"), list) or not d["relevance"]:
                return None
            rel = [1 if _as_bool(x) or x == 1 else 0 for x in d["relevance"]]
            denom = sum(rel)
            if not denom:
                return 0.0
            running = 0
            acc = 0.0
            for k, r in enumerate(rel, 1):
                running += r
                acc += (running / k) * r
            return acc / denom
        return self._cached(f"context_precision::{query}::{len(contexts)}::{'|'.join(contexts)[:300]}", calc)

    def context_recall(self, gold_answer: str, contexts: list[str]) -> float | None:
        """参考答案里的 claim，有多少能归因到检索上下文。"""
        def calc() -> float | None:
            d = self._ask_json(SYS, f"""从【参考答案】中抽取所有原子事实陈述（每条只表达一个事实），
逐条判断该陈述是否能从【参考资料】中找到明确依据。

【参考答案】{gold_answer}
【参考资料】
{self._ctx_block(contexts)}

输出格式：{{"claims":[{{"text":"陈述原文","attributable":true或false}}]}}""")
            if not d or not isinstance(d.get("claims"), list) or not d["claims"]:
                return None
            return sum(_as_bool(x.get("attributable")) for x in d["claims"]) / len(d["claims"])
        return self._cached(f"context_recall::{gold_answer}::{'|'.join(contexts)[:300]}", calc)

    def _cosine(self, a: str, b: str) -> float:
        va, _ = self.encoder.encode_query(a)
        vb, _ = self.encoder.encode_query(b)
        return sum(x * y for x, y in zip(va, vb))

    def answer_relevancy(self, query: str, answer: str) -> float | None:
        """由答案反向生成 N 个问题，算它们与原问题的平均余弦相似度。"""
        def calc() -> float | None:
            d = self._ask_json(SYS, f"""根据下面的【答案】，生成 3 个不同的、该答案能够回答的问题。
问题要贴合答案实际提供的内容，不要臆造答案里没有的信息。

【答案】{answer}

输出格式：{{"questions":["问题1","问题2","问题3"]}}""")
            if not d or not isinstance(d.get("questions"), list) or not d["questions"]:
                return None
            if self.encoder is None:
                return None  # 无 encoder 时该项不可算（报告中记为缺失）
            sims = [self._cosine(query, q) for q in d["questions"][:3]]
            return sum(sims) / len(sims)
        return self._cached(f"answer_relevancy::{query}::{answer}", calc)

    def evaluate(self, sample: dict, answer: str, contexts: list[str]) -> dict:
        """返回四指标分数；任一指标 judge 失败记 None，不中断整批评估。"""
        return {
            "faithfulness": self.faithfulness(sample["query"], answer, contexts),
            "answer_relevancy": self.answer_relevancy(sample["query"], answer),
            "context_precision": self.context_precision(sample["query"], contexts),
            "context_recall": self.context_recall(sample["gold_answer"], contexts),
        }
