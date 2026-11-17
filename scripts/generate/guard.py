"""引用校验器：答案生成后的最后一道防线。

为什么必须有（面试高频）：
    LLM 会说得很像真的 —— "据记载这家店 1985 年创立，人均 68 元"，
    但资料里根本没写 1985，68 也是它编的。prompt 约束只是"劝"，校验器才是"查"。
    这一层做的事情：把答案里的引用编号、店名、关键数字，和召回集逐一核对。

校验三种问题：
    1. 幻觉引用：答案里出现 [5]，但只召回 3 家 -> 非法引用。
    2. 幻觉店名：答案提到一个不在召回集里的店名。
    3. 幻觉数字：答案里出现的价格/评分，和资料对不上（这里做轻量检查：
       只验证答案里写的 ¥数字 是否在候选资料里出现过，避免误伤）。

校验不通过时的策略：不是丢弃整份答案（那样太浪费），而是【降级到模板答案】——
模板答案 100% 来自结构化字段，零幻觉。这是"优雅降级"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from retrieve.verify import FinalHit


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""


def check_citations(answer: str, hits: list[FinalHit]) -> GuardResult:
    """校验引用编号与店名是否都在召回集内。"""
    n = len(hits)
    # 1. 引用编号合法性
    cited = [int(x) for x in re.findall(r"\[(\d+)\]", answer)]
    illegal = [c for c in cited if c < 1 or c > n]
    if illegal:
        return GuardResult(False, f"非法引用编号 {illegal}，召回集只有 {n} 家")

    # 2. 至少有一个有效引用（除非答案是"没有符合条件的店"这种拒答）
    refusal_markers = ["没有符合", "没有完全符合", "没有找到", "无法满足"]
    is_refusal = any(m in answer for m in refusal_markers)
    if not cited and not is_refusal:
        return GuardResult(False, "答案没有引用任何证据编号")

    # 3. 【新增】元叙述泄漏：reasoning 模型偶尔把"编号对应错了"这类思考写进正文
    leak_markers = ["编号对应错", "对应错了", "这里是 [", "这里应该是", "重新编号", "修正："]
    for mk in leak_markers:
        if mk in answer:
            return GuardResult(False, f"答案泄漏了推理/元叙述（含'{mk}'）")

    # 4. 【新增】引用对齐：店名[N] 里的 N 必须真的指向这家店（防引用错位）
    #    例：召回[1]=明记轩、[2]=榕荫轩，但答案写"榕荫轩[2]"-> 错位。
    #    用"店名核心前缀"做宽松匹配，容忍 LLM 用简称（明记轩·林和西面家 -> 明记轩）。
    for name, num in re.findall(r"([\u4e00-\u9fa5A-Za-z0-9·]{2,12})\s*\[(\d+)\]", answer):
        idx = int(num)
        if 1 <= idx <= n:
            truth = hits[idx - 1].shop_name
            core = name.split("·")[0]
            if core not in truth and truth.split("·")[0] not in name:
                return GuardResult(False, f"引用错位：'{name}[{num}]'，但编号{num}是'{truth}'")

    # 5. 幻觉店名：完全不提任何已知店名且非拒答 -> 可疑
    #    用核心前缀匹配，容忍 LLM 用简称（"明记轩·林和西面家" -> "明记轩"也算提及）
    cores = {h.shop_name.split("·")[0] for h in hits}
    if not is_refusal and not any(name in answer for name in cores):
        return GuardResult(False, "答案未提及任何召回集中的店名")
    return GuardResult(True)


def check_numbers(answer: str, hits: list[FinalHit]) -> GuardResult:
    """轻量数字核验：答案里显式写的 ¥数字 必须出现在资料中。"""
    cited_nums = set(re.findall(r"[¥￥]\s*(\d+)", answer))
    if not cited_nums:
        return GuardResult(True)  # 没写价格数字就不检查
    corpus = " ".join(h.parent_text for h in hits)
    corpus_nums = set(re.findall(r"[¥￥]\s*(\d+)", corpus)) | set(
        re.findall(r"人均\s*(\d+)", corpus)
    )
    bad = cited_nums - corpus_nums
    if bad:
        return GuardResult(False, f"答案出现资料中没有的价格数字：{sorted(bad)}")
    return GuardResult(True)


def validate(answer: str, hits: list[FinalHit]) -> GuardResult:
    for fn in (check_citations, check_numbers):
        r = fn(answer, hits)
        if not r.ok:
            return r
    return GuardResult(True)
