"""生成层总控：LLM 组织答案 -> 引用校验 -> 不过则模板降级。

“答案可降级，系统不能挂” —— 这是生成层的设计底线。
    LLM 可用且校验通过 -> 用 LLM 的自然语言答案（体验好）
    LLM 不可用 / 校验不过 -> 用结构化字段拼的模板答案（零幻觉，可解释）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from retrieve.verify import FinalHit
from .llm_client import LLMClient, LLMConfig
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .guard import validate


@dataclass
class GeneratedAnswer:
    text: str                 # 最终给用户的答案
    mode: str                 # "llm" / "template"（可观测：答案是谁生成的）
    guard_reason: str = ""    # 校验结果说明
    llm_ms: float = 0.0


class AnswerGenerator:
    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    def generate(self, query: str, hits: list[FinalHit], slots_summary: str = "") -> GeneratedAnswer:
        if not hits:
            return GeneratedAnswer(
                text="根据现有资料，没有找到符合条件的店。可以试试放宽价格、区域或口味要求。",
                mode="template",
            )

        import time
        t0 = time.time()
        llm_text = self.client.chat(SYSTEM_PROMPT, build_user_prompt(query, slots_summary, hits))
        llm_ms = (time.time() - t0) * 1000

        if llm_text:
            g = validate(llm_text, hits)  # 引用校验：LLM 答案也未必可信
            if g.ok:
                return GeneratedAnswer(llm_text, mode="llm", guard_reason="pass", llm_ms=llm_ms)
            reason = g.reason
        else:
            reason = "LLM 不可用或返回空"

        # 降级：模板答案（100% 来自结构化字段，零幻觉）
        return GeneratedAnswer(
            self._template(hits), mode="template", guard_reason=reason, llm_ms=llm_ms
        )

    @staticmethod
    def _template(hits: list[FinalHit]) -> str:
        lines = [f"为你找到 {len(hits)} 家符合条件的店：\n"]
        for i, h in enumerate(hits, 1):
            p = h.payload
            facts = []
            if p.get("price_per_person"):
                facts.append(f"人均 ¥{p['price_per_person']}")
            if p.get("rating"):
                facts.append(f"评分 {p['rating']}")
            areas = " / ".join(filter(None, [p.get("district"), p.get("business_area")]))
            if areas:
                facts.append(areas)
            if p.get("allergens"):
                facts.append(f"含过敏原：{'、'.join(p['allergens'])}")
            lines.append(f"{i}. {h.shop_name}[{i}] —— {'｜'.join(facts)}")
        lines.append("\n（以上信息来自检索到的餐厅资料。）")
        return "\n".join(lines)
