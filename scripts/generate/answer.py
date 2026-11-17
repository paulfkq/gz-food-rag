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
from .guard import validate, repair_citations


@dataclass
class GeneratedAnswer:
    text: str                 # 最终给用户的答案
    mode: str                 # "llm" / "template"（可观测：答案是谁生成的）
    guard_reason: str = ""    # 校验结果说明
    llm_ms: float = 0.0


class AnswerGenerator:
    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    def generate(self, query: str, hits: list[FinalHit], slots_summary: str = "",
                 system_extra: str = "", user_extra: str = "") -> GeneratedAnswer:
        """生成答案。system_extra/user_extra 是可选追加块（交通富化用），默认空串完全向后兼容。

        交通块只追加到 prompt，不进入 validate() 的引用校验口径 —— guard 仍按餐厅资料
        校验编号/店名，交通数字由 TRAFFIC_RULES 约束 LLM 照抄，不参与"是否照抄资料"判定。
        """
        if not hits:
            return GeneratedAnswer(
                text="根据现有资料，没有找到符合条件的店。可以试试放宽价格、区域或口味要求。",
                mode="template",
            )

        import time
        t0 = time.time()
        system_prompt = SYSTEM_PROMPT + (system_extra or "")
        user_prompt = build_user_prompt(query, slots_summary, hits) + (user_extra or "")
        llm_text = self.client.chat(system_prompt, user_prompt)
        llm_ms = (time.time() - t0) * 1000

        if llm_text:
            g = validate(llm_text, hits)  # 引用校验：LLM 答案也未必可信
            if g.ok:
                return GeneratedAnswer(llm_text, mode="llm", guard_reason="pass", llm_ms=llm_ms)

            # 引用错位可精准修复：正文事实是好的，只是店名 token 串了。
            # 修完【必须重新 validate】——复验不过照样降级，绝不带着脏答案出门。
            reason = g.reason
            if "引用错位" in reason:
                fixed, repairs = repair_citations(llm_text, hits)
                if repairs:
                    g2 = validate(fixed, hits)
                    if g2.ok:
                        detail = "；".join(repairs)
                        return GeneratedAnswer(
                            fixed, mode="llm", guard_reason=f"auto_repaired:{detail}", llm_ms=llm_ms
                        )
                    reason = g2.reason  # 修完还有别的问题 -> 用新原因降级
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
