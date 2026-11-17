"""FastAPI 应用：RAG 服务层。

架构要点（面试重点）：
    1. lifespan 里加载 RetrievalPipeline —— 进程启动时加载一次 4.4GB 模型，
       所有请求复用。对比 CLI 每跑一次命令重载一次（15~38s），这是服务层的根本价值。
    2. 全局单例通过 app.state 挂载，端点里用 Request 取，避免 import 时初始化
       （import 阶段初始化会在 reload/worker 多进程下重复加载）。
    3. 同步的检索/生成放到线程池（run_in_threadpool），避免阻塞事件循环——
       BGE-M3 encode / rerank / Qdrant 都是 CPU 密集同步调用，直接写在 async 端点里
       会卡住整个事件循环，导致并发请求排队。
    4. SSE 流式端点单独处理流式矛盾：guard 校验需要完整答案，
       提供 buffer_guard 两种策略（详见 stream_answer 注释）。

端点一览：
    GET  /                 服务信息
    GET  /health           健康检查（含索引点数）
    POST /api/search       只检索，返回结构化店铺 + trace
    POST /api/answer       端到端（非流式，完整 guard）
    POST /api/answer/stream SSE 流式答案（支持两种 guard 策略）
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# 让本文件既可被 uvicorn "service.app:app" 导入，也可直接 python 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request, HTTPException  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from retrieve.pipeline import RetrievalPipeline  # noqa: E402
from retrieve.config import COLLECTION  # noqa: E402
from generate.llm_client import LLMClient  # noqa: E402
from generate.answer import AnswerGenerator  # noqa: E402
from generate.prompt import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from generate.guard import validate  # noqa: E402
from generate.rewrite import rewrite_query  # noqa: E402
from . import session_store  # noqa: E402
from .schemas import (  # noqa: E402
    SearchRequest, AnswerRequest, ShopOut, TraceOut, SearchResponse,
    AnswerResponse, HealthResponse, RewriteOut, SessionCreateOut,
    SessionOut, MessageOut, RenameSessionRequest, BeginTurnRequest,
)

START_TIME = time.time()


# ---------------- lifespan：重资源只加载一次 ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程启动 -> yield -> 进程关闭。

    面试讲点：为什么用 lifespan 而不是模块级全局变量？
        - 模块级初始化发生在 import 时，uvicorn --reload / 多 worker 下会重复加载模型；
        - lifespan 是 FastAPI 官方推荐的"应用级资源"钩子，启动/清理成对，语义清晰；
        - 加载失败时应用直接起不来（fail fast），不会带着半初始化状态接请求。
    """
    print("[startup] 加载检索管线（模型约 4.4GB，一次性）...", flush=True)
    t0 = time.time()
    state = app.state
    state.status = "loading"
    state.detail = "loading models"
    try:
        session_store.init_db()  # 建会话表（幂等，SQLite 毫秒级）
        state.pipe = RetrievalPipeline()
        state.llm = LLMClient()
        state.generator = AnswerGenerator(client=state.llm)
        state.status = "ok"
        state.detail = "ready"
        print(f"[startup] 就绪，耗时 {time.time() - t0:.1f}s", flush=True)
        if not state.llm.config.available:
            print("[startup] 未检测到 LLM key，将走模板降级（设 RAG_LLM_API_KEY 启用）", flush=True)
    except Exception as e:  # noqa: BLE001
        state.status = "error"
        state.detail = f"{type(e).__name__}: {e}"
        print(f"[startup] 加载失败：{state.detail}", flush=True)
        raise
    yield
    print("[shutdown] 服务关闭", flush=True)


app = FastAPI(
    title="广州美食 RAG 服务",
    version="1.0.0",
    description="父子块切分 + BGE-M3 三路召回 + RRF + Rerank + 硬约束复核 + LLM 生成 + SSE 流式",
    lifespan=lifespan,
)

# CORS：让前端页面（含本地 html）能跨域调用。学习项目放行所有，生产应收紧 allow_origins。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 工具函数 ----------------

def _shop_out(rank: int, h) -> ShopOut:
    """FinalHit -> 对外精简视图（不暴露 parent_text 大段文本和内部 id 细节）。"""
    p = h.payload
    return ShopOut(
        rank=rank,
        shop_id=h.shop_id,
        shop_name=h.shop_name,
        score=round(h.score, 4),
        price_per_person=p.get("price_per_person"),
        rating=p.get("rating"),
        district=p.get("district"),
        business_area=p.get("business_area"),
        cuisines=p.get("cuisine") or [],
        noise_level=p.get("noise_level"),
        allergens=p.get("allergens") or [],
        avoid_ingredients=p.get("avoid_ingredients") or [],
        scene_tags=p.get("scene_tags") or [],
        taste_tags=p.get("taste_tags") or [],
        nearest_metro=p.get("nearest_metro") or [],
        metro_walk_min=p.get("metro_walk_min"),
        matched_soft=h.matched_soft,
    )


def _trace_out(trace, include: bool) -> TraceOut | None:
    if not include:
        return None
    return TraceOut(
        slots=trace.slots.summary() if trace.slots else "",
        filter=trace.filter_dump,
        recall_counts=trace.recall_counts,
        rejected_count=len(trace.rejected),
        timings_ms={k: round(v, 1) for k, v in trace.timings_ms.items()},
    )


def _ready_or_404(request: Request):
    """端点前置检查：模型没加载好就返回 503，不要接请求跑空。"""
    if request.app.state.status != "ok":
        raise HTTPException(
            status_code=503,
            detail=f"服务未就绪（status={request.app.state.status}）：{request.app.state.detail}",
        )


def _sse(event: str, data: dict) -> str:
    """拼一条 SSE 帧。格式：event: 名字 / data: JSON / 空行分隔。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _prepare_query(req, llm) -> tuple[str, RewriteOut | None, list[dict]]:
    """多轮入口：读历史 -> query 改写 -> 返回实际用于检索的 query。

    返回 (实际query, 改写结果 or None, 历史)。无 session_id 时原样返回，
    保证旧客户端（不传会话）行为完全不变 —— 向后兼容是加功能时的底线。
    """
    if not getattr(req, "session_id", None):
        return req.query, None, []

    history = session_store.get_history(req.session_id)
    if not history or not getattr(req, "enable_rewrite", True):
        return req.query, None, history

    r = rewrite_query(req.query, history, llm=llm)
    return r.query, RewriteOut(**r.to_dict()), history


def _save_turn(session_id: str | None, original: str, rewritten: str | None,
               answer_text: str, meta: dict) -> None:
    """回答完成时收尾落库。无 session_id 时是单轮查询，不落库。

    新链路里 user 消息已在"按下发送"时由 /api/sessions/{id}/begin-turn 落好（阶段1），
    这里走 finalize：只回填改写结果 + 落 assistant，不会重复插 user。
    老客户端没调 begin-turn 时，finalize 会自动补落 user，行为与旧版一致（向后兼容）。
    """
    if not session_id:
        return
    session_store.finalize_turn(session_id, original, rewritten, answer_text, meta=meta)


def _build_persist_meta(*, req, trace, hits, rewrite=None,
                        mode="", guard_reason="", need_clarify=False,
                        clarify_question="") -> dict:
    """构造落库 meta：除运行状态外，带齐"刷新后回显"所需的展示态。

    背景：早期 meta 只存 mode/count，前端历史消息不回填卡片（怕响应体膨胀）。
    用户实测刷新后店铺卡片 / trace / 条件摘要 / 改写标签全丢，只剩基本元素。
    故改为把精简的 ShopOut / TraceOut / slots / rewrite 一并落库：
      - ShopOut 已在 schemas 里砍掉 parent_text 等大字段，单家仅十几个短字段；
      - TraceOut 只有几个计数与耗时 dict；
      - 一轮 8 家店约几 KB，SQLite 与 getSession 响应都可承受。
    这样"怕膨胀"的旧取舍让位于用户明确要求的回显体验（面试可讲这个权衡取舍）。
    """
    trace_out = _trace_out(trace, True) if trace else None
    meta = {
        "mode": mode,
        "guard_reason": guard_reason,
        "count": len(hits),
        "slots": (trace.slots.summary() if trace and trace.slots else ""),
        "shops": [_shop_out(i, h).model_dump() for i, h in enumerate(hits, 1)],
        "trace": trace_out.model_dump() if trace_out else None,
    }
    if need_clarify:
        meta["need_clarify"] = True
        meta["clarify_question"] = clarify_question or ""
    if rewrite is not None and getattr(rewrite, "changed", False):
        meta["rewrite"] = {
            "original": req.query,
            "rewritten": rewrite.query,
            "reason": rewrite.reason,
        }
    return meta


# ---------------- 端点 ----------------

@app.get("/", tags=["meta"])
def root():
    return {
        "service": "广州美食 RAG",
        "docs": "/docs",
        "endpoints": [
            "/health", "/api/search", "/api/answer", "/api/answer/stream",
            "/api/sessions", "/docs",
        ],
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(request: Request):
    """健康检查：K8s / 负载均衡探针用。带索引点数，能发现"服务活着但索引空了"。"""
    st = request.app.state
    points = None
    try:
        if st.status == "ok":
            points = st.pipe.client.get_collection(COLLECTION).points_count
    except Exception:  # noqa: BLE001
        pass
    return HealthResponse(
        status=st.status,
        detail=st.detail,
        llm_configured=bool(st.llm.config.available) if hasattr(st, "llm") else False,
        collection_points=points,
        uptime_s=round(time.time() - START_TIME, 1),
    )


# ---------------- 会话管理端点 ----------------

@app.post("/api/sessions", response_model=SessionCreateOut, tags=["session"])
def create_session():
    """新建会话，返回 session_id。前端把它带上做多轮对话。"""
    return session_store.create_session()


@app.get("/api/sessions", response_model=list[SessionOut], tags=["session"])
def list_sessions():
    """会话列表（按最后更新时间倒序，带消息数），不含消息正文，列表轻量。"""
    rows = session_store.list_sessions()
    # 列表不需要消息正文，显式置空避免响应体膨胀
    return [SessionOut(messages=[], **r) for r in rows]


@app.get("/api/sessions/{session_id}", response_model=SessionOut, tags=["session"])
def get_session(session_id: str):
    """取单个会话详情（含全部消息）。前端切会话时调用。"""
    s = session_store.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return s


@app.post("/api/sessions/{session_id}/begin-turn", response_model=SessionOut, tags=["session"])
def begin_turn(session_id: str, req: BeginTurnRequest):
    """用户按下发送的瞬间调用（阶段1）：立即落 user 消息 + 锁定会话标题。

    为什么单独成一个端点而不是等回答完再存：
        回答要跑 10~25s，中途用户可能切会话/刷新/中断。若那时才落库，
        标题会一直是"新会话"，刷新后这条消息也丢失。把"落 user + 锁定标题"
        提到输入瞬间，就与回答的成败/快慢彻底解耦。返回整条会话给前端同步标题。
    """
    s = session_store.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session_store.add_user_turn(session_id, req.query)
    s = session_store.get_session(session_id)
    # 补上 message_count（get_session 不带该字段，列表查询才统计），让返回结构自洽
    counts = {x["id"]: x["message_count"] for x in session_store.list_sessions()}
    s["message_count"] = counts.get(session_id, 0)
    return s


@app.patch("/api/sessions/{session_id}", response_model=SessionCreateOut, tags=["session"])
def rename_session(session_id: str, req: RenameSessionRequest):
    s = session_store.get_session(session_id, include_messages=False)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session_store.rename_session(session_id, req.title)
    return {**s, "title": req.title}


@app.delete("/api/sessions/{session_id}", tags=["session"])
def delete_session(session_id: str):
    if not session_store.delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "id": session_id}


@app.post("/api/search", response_model=SearchResponse, tags=["rag"])
async def search(req: SearchRequest, request: Request):
    """只走检索层（槽位→三路召回→RRF→Rerank→复核→回溯），不做 LLM 生成。"""
    _ready_or_404(request)
    st = request.app.state
    pipe = st.pipe

    # 多轮：先用历史把指代句改写成自包含 query
    actual_query, rewrite, _ = await run_in_threadpool(_prepare_query, req, st.llm)

    # CPU 密集型同步调用丢线程池，避免阻塞事件循环。
    # prefer_llm 以参数传入（per-request），不再改全局单例状态 —— 并发安全。
    hits, trace = await run_in_threadpool(
        pipe.search, actual_query, topn=req.topn, prefer_llm=req.prefer_llm_slots,
    )

    shops_out = [_shop_out(i, h) for i, h in enumerate(hits, 1)]
    # 纯检索模式也落库：否则用 search 做首轮后，下一轮读不到历史、改写失效。
    # 没有 LLM 答案，就把店铺摘要作为 assistant 内容存下来（够做指代线索）。
    if req.session_id:
        digest = "；".join(f"{s.rank}.{s.shop_name}" for s in shops_out) or "未找到匹配的店"
        meta = _build_persist_meta(
            req=req, trace=trace, hits=hits, rewrite=rewrite,
            mode="search", need_clarify=trace.need_clarify,
            clarify_question=trace.clarify_question,
        )
        _save_turn(req.session_id, req.query, rewrite.query if rewrite else None, digest, meta)

    return SearchResponse(
        query=actual_query,
        original_query=req.query,
        rewrite=rewrite,
        count=len(hits),
        slots=trace.slots.summary() if trace.slots else "",
        need_clarify=trace.need_clarify,
        clarify_question=trace.clarify_question,
        shops=shops_out,
        trace=_trace_out(trace, req.include_trace),
    )


@app.post("/api/answer", response_model=AnswerResponse, tags=["rag"])
async def answer(req: AnswerRequest, request: Request):
    """端到端：检索 + LLM 生成 + 引用校验 + 降级。非流式，一次返回完整答案。"""
    _ready_or_404(request)
    st = request.app.state
    t0 = time.time()

    # 多轮：历史改写 query
    actual_query, rewrite, _ = await run_in_threadpool(_prepare_query, req, st.llm)

    # per-request 传参，避免并发请求互相污染单例的 prefer_llm
    hits, trace = await run_in_threadpool(
        st.pipe.search, actual_query, topn=req.topn, prefer_llm=req.prefer_llm_slots,
    )

    if trace.need_clarify:
        out = AnswerResponse(
            query=actual_query, original_query=req.query, rewrite=rewrite,
            session_id=req.session_id,
            answer="", mode="template", count=0,
            slots=trace.slots.summary(), need_clarify=True,
            clarify_question=trace.clarify_question,
        )
        _save_turn(req.session_id, req.query, rewrite.query if rewrite else None,
                   out.clarify_question or "",
                   _build_persist_meta(req=req, trace=trace, hits=[], rewrite=rewrite,
                                       need_clarify=True, clarify_question=trace.clarify_question))
        return out

    if req.use_llm:
        ans = await run_in_threadpool(
            st.generator.generate, actual_query, hits, trace.slots.summary(),
        )
    else:
        ans = await run_in_threadpool(_template_only, st.generator, hits)

    out = AnswerResponse(
        query=actual_query,
        original_query=req.query,
        rewrite=rewrite,
        session_id=req.session_id,
        answer=ans.text,
        mode=ans.mode,
        guard_reason=ans.guard_reason,
        llm_ms=round(ans.llm_ms, 1),
        total_ms=round((time.time() - t0) * 1000, 1),
        count=len(hits),
        slots=trace.slots.summary(),
        shops=[_shop_out(i, h) for i, h in enumerate(hits, 1)],
        trace=_trace_out(trace, req.include_trace),
    )
    # 落库：把这一轮 user + assistant 存起来，下一轮就能做上下文改写。
    # meta 里带齐展示态，刷新后店铺卡片/trace/条件摘要/改写标签能还原。
    _save_turn(req.session_id, req.query, rewrite.query if rewrite else None, ans.text,
               _build_persist_meta(req=req, trace=trace, hits=hits, rewrite=rewrite,
                                   mode=ans.mode, guard_reason=ans.guard_reason))
    return out


def _template_only(generator, hits):
    """强制模板：复用生成层的 _template，但不调用 LLM。"""
    from generate.answer import GeneratedAnswer
    return GeneratedAnswer(text=generator._template(hits), mode="template", guard_reason="user_disabled")


@app.post("/api/answer/stream", tags=["rag"])
async def stream_answer(req: AnswerRequest, request: Request):
    """SSE 流式答案。

    ===== 流式 vs guard 的固有矛盾（本实现的核心教学点）=====
        guard 校验（引用编号/店名对齐/数字溯源）必须拿到【完整答案】才能做；
        但流式是【边生成边吐】，吐出去的字收不回来。所以有两种策略：

        策略 A（buffer_guard=True，默认，安全优先）：
            后台流式接收 LLM 全文 -> 缓冲完后 guard 校验 -> 通过才以流的形式吐出。
            代价：首字延迟 = LLM 生成全程（约 6s），没有真正的打字机效果；
            收益：吐出的每个字都经过校验，零幻觉泄漏。

        策略 B（buffer_guard=False，体验优先）：
            逐块边生成边吐 -> 流结束后做 guard -> 追加一条事件：
              · 通过：guard 事件 ok=true
              · 失败：guard 事件 ok=false + fallback 事件给出模板答案全文，
                前端收到 fallback 应【覆盖】已显示的流式文本（事件里带 replace=true）。
            代价：用户可能先看到一段有问题的文本再被覆盖；
            收益：首字延迟低，打字机体验好。

        SSE 事件类型（前端按 event 名分支处理）：
            meta      - 检索完成：命中数、槽位
            clarify   - 条件冲突需追问（无后续 token）
            token     - 正文增量片段（策略 B 才有）
            answer    - 完整答案（策略 A，或模板降级）
            guard     - 校验结果 {ok, reason}
            fallback  - 校验失败时的模板答案（策略 B，replace=true）
            done      - 结束，带耗时统计
            error     - 异常终止

    为什么用 SSE 不用 WebSocket（面试可讲）：
        本场景是【单向推送】（服务端吐 token，客户端只发一次请求），
        SSE 基于普通 HTTP，自动重连、穿透代理友好、实现简单；
        WebSocket 适合双向高频交互（聊天室/协同编辑），这里属于杀鸡用牛刀。
    """
    _ready_or_404(request)
    st = request.app.state
    t0 = time.time()

    async def gen():
        # 落库用的收尾变量：记录最终展示给用户的答案文本 + 元信息
        final_text = ""
        final_meta: dict = {}
        rewritten_for_save = None

        def persist():
            if req.session_id:
                _save_turn(req.session_id, req.query, rewritten_for_save, final_text, final_meta)

        try:
            # 多轮：历史改写 query（在线程池里跑，不阻塞事件循环）
            actual_query, rewrite, _ = await run_in_threadpool(_prepare_query, req, st.llm)
            rewritten_for_save = rewrite.query if rewrite else None

            # per-request 传参，并发请求互不污染单例状态
            hits, trace = await run_in_threadpool(
                st.pipe.search, actual_query, topn=req.topn, prefer_llm=req.prefer_llm_slots,
            )

            slots_summary = trace.slots.summary() if trace.slots else ""

            # 各终止分支共用：用本轮的 req/trace/hits/rewrite 构造落库 meta。
            # 把展示态一起落库，刷新后店铺卡片/trace/条件摘要/改写标签能还原。
            def mk_meta(**kw):
                return _build_persist_meta(req=req, trace=trace, hits=hits,
                                           rewrite=rewrite, **kw)

            # meta 里带上结构化店铺、trace、改写结果：前端首屏立即渲染，
            # 并把"原始 → 改写"展示给用户（可解释性）。
            yield _sse("meta", {
                "count": len(hits),
                "slots": slots_summary,
                "query": actual_query,
                "original_query": req.query,
                "rewrite": rewrite.model_dump() if rewrite else None,
                "shops": [_shop_out(i, h).model_dump() for i, h in enumerate(hits, 1)],
                "trace": _trace_out(trace, req.include_trace).model_dump()
                if req.include_trace and _trace_out(trace, True) else None,
            })

            if trace.need_clarify:
                final_text = trace.clarify_question or ""
                final_meta = mk_meta(need_clarify=True, clarify_question=trace.clarify_question)
                yield _sse("clarify", {"question": trace.clarify_question})
                persist()
                yield _sse("done", {"total_ms": round((time.time() - t0) * 1000, 1)})
                return

            if not hits:
                final_text = "根据现有资料，没有找到符合条件的店。可以试试放宽价格、区域或口味要求。"
                final_meta = mk_meta(mode="template", guard_reason="no_hits")
                yield _sse("answer", {"text": final_text, "mode": "template", "guard_reason": "no_hits"})
                persist()
                yield _sse("done", {"total_ms": round((time.time() - t0) * 1000, 1)})
                return

            # 强制模板 / 没配 key：直接一次性给模板，无需流式
            if not req.use_llm or not st.llm.config.available:
                final_text = await run_in_threadpool(st.generator._template, hits)
                reason = "user_disabled" if not req.use_llm else "llm_not_configured"
                final_meta = mk_meta(mode="template", guard_reason=reason)
                yield _sse("answer", {"text": final_text, "mode": "template", "guard_reason": reason})
                persist()
                yield _sse("done", {"total_ms": round((time.time() - t0) * 1000, 1)})
                return

            user_prompt = build_user_prompt(actual_query, slots_summary, hits)

            if req.buffer_guard:
                # ---- 策略 A：缓冲全文 -> 校验 -> 一次性吐 ----
                full = await _collect_stream(st.llm, SYSTEM_PROMPT, user_prompt)
                if not full:
                    final_text = await run_in_threadpool(st.generator._template, hits)
                    final_meta = mk_meta(mode="template", guard_reason="llm_empty")
                    yield _sse("answer", {"text": final_text, "mode": "template", "guard_reason": "llm_empty"})
                    yield _sse("guard", {"ok": False, "reason": "llm_empty"})
                else:
                    g = await run_in_threadpool(validate, full, hits)
                    if g.ok:
                        final_text = full
                        final_meta = mk_meta(mode="llm", guard_reason="pass")
                        yield _sse("answer", {"text": final_text, "mode": "llm", "guard_reason": "pass"})
                        yield _sse("guard", {"ok": True, "reason": "pass"})
                    else:
                        final_text = await run_in_threadpool(st.generator._template, hits)
                        final_meta = mk_meta(mode="template", guard_reason=g.reason)
                        yield _sse("answer", {"text": final_text, "mode": "template", "guard_reason": g.reason})
                        yield _sse("guard", {"ok": False, "reason": g.reason})
            else:
                # ---- 策略 B：逐字流式 -> 事后校验 -> 失败给 fallback 覆盖 ----
                buf = []
                async for piece in _iter_stream(st.llm, SYSTEM_PROMPT, user_prompt):
                    buf.append(piece)
                    yield _sse("token", {"text": piece})
                full = "".join(buf)
                if not full:
                    yield _sse("guard", {"ok": False, "reason": "llm_empty"})
                    final_text = await run_in_threadpool(st.generator._template, hits)
                    final_meta = mk_meta(mode="template", guard_reason="llm_empty")
                    yield _sse("fallback", {"text": final_text, "replace": True, "reason": "llm_empty"})
                else:
                    g = await run_in_threadpool(validate, full, hits)
                    yield _sse("guard", {"ok": g.ok, "reason": g.reason or "pass"})
                    if g.ok:
                        final_text = full
                        final_meta = mk_meta(mode="llm", guard_reason="pass")
                    else:
                        final_text = await run_in_threadpool(st.generator._template, hits)
                        final_meta = mk_meta(mode="template", guard_reason=g.reason)
                        yield _sse("fallback", {"text": final_text, "replace": True, "reason": g.reason})

            persist()
            yield _sse("done", {
                "total_ms": round((time.time() - t0) * 1000, 1),
                "count": len(hits),
            })
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"type": type(e).__name__, "message": str(e)[:200]})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲，否则流式会被攒着不发
        },
    )


# ---- 流式辅助：把同步 generator 桥接到 async ----

async def _collect_stream(llm, system: str, user: str) -> str:
    """在线程池里把流式全文收齐（策略 A 用）。"""
    def _run():
        return "".join(llm.chat_stream(system, user))
    return await run_in_threadpool(_run)


async def _iter_stream(llm, system: str, user: str):
    """把同步 generator 逐块转成 async 迭代器（策略 B 用）。

    这里用 queue + 后台线程做桥接。为什么不用 async generator 直接包？
        LLMClient.chat_stream 是同步阻塞迭代器（OpenAI SDK 的 stream），
        直接在 async 里 next() 会阻塞事件循环；丢到线程池逐块 next() 可行，
        但每块一次线程切换开销大。用一个专用后台线程 + asyncio.Queue 更稳。
    """
    import asyncio
    import threading
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    SENTINEL = object()

    def producer():
        try:
            for piece in llm.chat_stream(system, user):
                asyncio.run_coroutine_threadsafe(q.put(piece), loop)
            asyncio.run_coroutine_threadsafe(q.put(SENTINEL), loop)
        except Exception as e:  # noqa: BLE001
            asyncio.run_coroutine_threadsafe(q.put(SENTINEL), loop)
            print(f"[stream] producer 异常：{type(e).__name__}: {e}")

    loop = asyncio.get_running_loop()
    threading.Thread(target=producer, daemon=True, name="llm-stream").start()
    while True:
        item = await q.get()
        if item is SENTINEL:
            break
        yield item
