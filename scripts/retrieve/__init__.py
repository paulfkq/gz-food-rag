"""广州美食 RAG 检索层。

模块顺序（也是学习顺序）：
    slots.py       ① 槽位解析：自然语言 -> 结构化条件
    filter_dsl.py  ② Filter 编译 + 后置复核函数
    recall.py      ③ 三路召回：dense / sparse / BM25
    fusion.py      ④ RRF 融合  ⑤ Rerank 精排
    verify.py      ⑥ 硬约束复核 + Parent 回溯
    pipeline.py    总控：串起六步 + trace
"""
from .slots import SearchSlots, RuleSlotParser, parse_slots
from .recall import Candidate
from .verify import FinalHit
from .pipeline import RetrievalPipeline, Trace

__all__ = ["SearchSlots", "RuleSlotParser", "parse_slots", "Candidate",
           "FinalHit", "RetrievalPipeline", "Trace"]
