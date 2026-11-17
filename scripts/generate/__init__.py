"""生成层：把检索 Top-N 交给 LLM 组织成自然语言答案，并做引用校验。

模块：
    llm_client.py  OpenAI 兼容客户端（本地/云端可切换 + 降级）
    prompt.py      system/user prompt 构造（严格约束只用证据）
    answer.py      生成 + 引用校验 + 模板兜底
    guard.py       引用校验器（防编造店名/价格/评分）
"""
from .answer import AnswerGenerator, GeneratedAnswer

__all__ = ["AnswerGenerator", "GeneratedAnswer"]
