"""Pydantic 数据模型：请求校验 + 响应结构 + 自动 OpenAPI 文档。

为什么用 Pydantic（面试讲点）：
    1. 参数自动校验（query 非空、topn 范围），不合法直接 422，不用手写 if。
    2. 响应结构即文档：FastAPI 据此自动生成 /docs 的 Swagger 页面。
    3. FinalHit 是内部 dataclass，含 parent_text 这种大段文本，不能直接外泄给前端——
       这里定义精简的 ShopOut，只暴露必要字段（信息最小暴露原则）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------- 请求 ----------------

class SearchRequest(BaseModel):
    """只走检索层，不做 LLM 生成。适合前端自己做展示/二次加工。"""
    query: str = Field(..., min_length=1, max_length=200, description="用户自然语言问题")
    topn: int = Field(8, ge=1, le=20, description="最终返回店数")
    prefer_llm_slots: bool = Field(True, description="槽位解析是否优先用 LLM")
    include_trace: bool = Field(False, description="是否返回各阶段 trace 明细")
    session_id: str | None = Field(None, description="多轮会话 ID；传入则先做 query 改写")
    enable_rewrite: bool = Field(True, description="有多轮历史时是否做 query 改写")


class AnswerRequest(BaseModel):
    """端到端：检索 + 生成 + 引用校验。"""
    query: str = Field(..., min_length=1, max_length=200)
    topn: int = Field(8, ge=1, le=20)
    prefer_llm_slots: bool = Field(True)
    use_llm: bool = Field(True, description="False 则强制模板答案（即使配了 key）")
    buffer_guard: bool = Field(
        True,
        description=(
            "流式专用：True=先缓冲全文→校验通过再一次性输出（安全，无打字机效果）；"
            "False=逐字流式输出，校验在流结束后进行，失败则推送 fallback 事件让前端覆盖（有打字机效果）"
        ),
    )
    include_trace: bool = Field(False)
    session_id: str | None = Field(None, description="多轮会话 ID；传入则读历史改写 query 并落库")
    enable_rewrite: bool = Field(True, description="有多轮历史时是否做 query 改写")


# ---------------- 多轮会话 ----------------

class RewriteOut(BaseModel):
    """query 改写结果：让前端能展示"原始 → 改写"，可解释、可排错。"""
    query: str
    changed: bool
    reason: str


class SessionCreateOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    query_original: str | None = None
    query_rewritten: str | None = None
    meta: dict = Field(default_factory=dict)
    created_at: str


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    messages: list[MessageOut] = Field(default_factory=list)


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=40)


class BeginTurnRequest(BaseModel):
    """用户按下发送的瞬间调用：先落 user 消息并锁定会话标题。

    这样标题与"回答是否生成完成"解耦：即使之后 abort/刷新，会话名也已固定。
    """
    query: str = Field(..., min_length=1, max_length=200)


# ---------------- 响应 ----------------

class ShopOut(BaseModel):
    """一家店的对外精简视图。"""
    rank: int
    shop_id: str
    shop_name: str
    score: float = Field(..., description="精排分（rerank 输出，越大越靠前）")
    price_per_person: int | None = None
    rating: float | None = None
    district: str | None = None
    business_area: str | None = None
    cuisines: list[str] = Field(default_factory=list)
    noise_level: str | None = None
    allergens: list[str] = Field(default_factory=list)
    avoid_ingredients: list[str] = Field(default_factory=list)
    scene_tags: list[str] = Field(default_factory=list)
    taste_tags: list[str] = Field(default_factory=list)
    nearest_metro: list[dict] = Field(default_factory=list, description="最近地铁站列表[{line,station,walk_min}]")
    metro_walk_min: int | None = None
    matched_soft: list[str] = Field(default_factory=list)


class TraceOut(BaseModel):
    """可观测性明细：各阶段耗时、召回数、被剔除的候选。"""
    slots: str = ""
    filter: dict | None = None
    recall_counts: dict = Field(default_factory=dict)
    rejected_count: int = 0
    timings_ms: dict[str, float] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str                      # 实际用于检索的 query（可能被改写）
    original_query: str = ""        # 用户原始输入（前端展示用）
    rewrite: RewriteOut | None = None
    count: int
    slots: str
    need_clarify: bool = False
    clarify_question: str | None = None
    shops: list[ShopOut] = Field(default_factory=list)
    trace: TraceOut | None = None


class AnswerResponse(BaseModel):
    query: str                      # 实际用于检索的 query（可能被改写）
    original_query: str = ""        # 用户原始输入
    rewrite: RewriteOut | None = None
    session_id: str | None = None
    answer: str
    mode: str = Field(..., description="llm / template —— 答案是谁生成的")
    guard_reason: str = ""
    llm_ms: float = 0.0
    total_ms: float = 0.0
    count: int
    slots: str
    need_clarify: bool = False
    clarify_question: str | None = None
    shops: list[ShopOut] = Field(default_factory=list)
    trace: TraceOut | None = None


class HealthResponse(BaseModel):
    status: str = Field(..., description="ok / loading / error")
    detail: str = ""
    llm_configured: bool = Field(False, description="是否检测到 LLM key")
    collection_points: int | None = None
    uptime_s: float = 0.0
