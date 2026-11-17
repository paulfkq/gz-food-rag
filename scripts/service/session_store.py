"""SQLite 会话存储层：给多轮对话提供持久化。

为什么用 SQLite（面试讲点）：
    - 零运维：标准库 sqlite3 自带，单机演示/学习项目不需要额外起服务；
    - 事务可靠：WAL 模式下读写并发友好，进程重启数据不丢；
    - 接口清晰：会话（sessions）和消息（messages）一对多，是聊天系统的标准建模。
  生产环境换成 PostgreSQL/Redis 时，只要保持同样的方法签名，上层端点不用改
  —— 这就是"存储层抽象"的价值。

线程模型：
    FastAPI 的请求跑在不同线程（CPU 密集调用走 run_in_threadpool），
    sqlite3 连接不能跨线程共享。这里采用【每次操作新建连接】的策略：
    连接创建开销极小（微秒级），换来天然的线程安全，避免共享连接的锁竞争。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# scripts/service/session_store.py -> parents[2] 是项目根目录 gz-food-rag/
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sessions.db"


@contextmanager
def _db():
    """连接级上下文：自动提交/回滚 + 关闭，保证不泄漏句柄。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 读写并发：写不阻塞读
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表（幂等）。在 lifespan 里调用一次。"""
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT '新会话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,           -- user / assistant
                content         TEXT NOT NULL,           -- 展示给用户的文本
                query_original  TEXT,                    -- 用户原始输入（仅 user）
                query_rewritten TEXT,                    -- 改写后的检索 query（仅 user）
                meta_json       TEXT,                    -- mode/guard/count 等结构化数据
                created_at      TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, id)
        """)
        # 老库兼容：早期版本可能没有 query_original/query_rewritten 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for col in ("query_original", "query_rewritten", "meta_json"):
            if col not in cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_session(title: str = "新会话") -> dict:
    sid = uuid.uuid4().hex[:12]
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (sid, title, now, now),
        )
    return {"id": sid, "title": title, "created_at": now, "updated_at": now}


def list_sessions(limit: int = 50) -> list[dict]:
    """会话列表，附带消息数，按最后更新时间倒序。"""
    with _db() as conn:
        rows = conn.execute("""
            SELECT s.*, COUNT(m.id) AS message_count
            FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str, include_messages: bool = True) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        if include_messages:
            msgs = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
            out["messages"] = [_message_out(m) for m in msgs]
    return out


def _message_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["meta"] = json.loads(d.get("meta_json") or "{}")
    except (ValueError, TypeError):
        d["meta"] = {}
    d.pop("meta_json", None)
    return d


def delete_session(session_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))  # 兼容无外键的旧库
        return cur.rowcount > 0


def rename_session(session_id: str, title: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (title[:40], _now(), session_id),
        )


def add_message(
    session_id: str,
    role: str,
    content: str,
    query_original: str | None = None,
    query_rewritten: str | None = None,
    meta: dict | None = None,
) -> dict:
    """追加一条消息，并同步刷新会话的 updated_at（列表排序依赖它）。"""
    now = _now()
    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO messages
                (session_id, role, content, query_original, query_rewritten, meta_json, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            session_id, role, content,
            query_original, query_rewritten,
            json.dumps(meta or {}, ensure_ascii=False), now,
        ))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        # 首轮 user 消息自动把会话标题改成问题前 24 字（演示时一眼能认出）
        if role == "user":
            conn.execute("""
                UPDATE sessions SET title=?
                WHERE id=? AND title='新会话'
            """, (content.strip()[:24], session_id))
        mid = cur.lastrowid
    return {
        "id": mid, "session_id": session_id, "role": role, "content": content,
        "query_original": query_original, "query_rewritten": query_rewritten,
        "meta": meta or {}, "created_at": now,
    }


# ---------------- 两阶段落库（输入即锁标题 / 回答完成再收尾）----------------
# 背景：旧版只在【回答生成完成】时才落 user 消息并设标题。回答中途用户切走 ->
#       前端 abort -> 后端 SSE 被切断 -> persist() 没执行 -> user 消息根本没落库，
#       于是标题停在"新会话"、刷新后内容全丢。
# 解法：把"落 user + 锁标题"提前到用户按下发送的瞬间（阶段1），与回答是否完成解耦；
#       回答完成后只做收尾回填（阶段2）。两阶段用 meta.phase 标记衔接，保证幂等。

def add_user_turn(session_id: str, query: str) -> dict:
    """阶段1：用户发送瞬间落一条 user 消息，并锁定会话标题。

    meta.phase='pending' 表示"这轮已开始、答案还没生成完"，两个作用：
      1. get_history 会排除 pending，避免把【当前正在问的这句】当成历史去改写自己；
      2. finalize_turn 靠它幂等回填，不会重复插入 user。
    复用 add_message 里"首条 user 自动把'新会话'改成问题前 24 字"的逻辑锁标题。
    """
    with _db() as conn:
        # 中断重发去重：若最后一条 user 内容相同且仍是 pending（上轮被 abort 没 finalize），
        # 直接复用它，避免库里堆两条相同 user；否则新建。
        last = conn.execute("""
            SELECT id, content, meta_json FROM messages
            WHERE session_id=? AND role='user'
            ORDER BY id DESC LIMIT 1
        """, (session_id,)).fetchone()
        if last is not None and (last["content"] or "").strip() == query.strip():
            try:
                m = json.loads(last["meta_json"] or "{}")
            except (ValueError, TypeError):
                m = {}
            if m.get("phase") == "pending":
                return _message_out(last)
    return add_message(
        session_id, "user", query,
        query_original=query,
        meta={"phase": "pending"},
    )


def finalize_turn(session_id: str, original: str, rewritten: str | None,
                  answer_text: str, meta: dict | None = None) -> None:
    """阶段2：回答完成 -> 回填 pending user 的改写结果 + 落 assistant。

    幂等 & 向后兼容：
      - 找到 pending user（新前端走了阶段1）：只回填 query_rewritten、把 phase 置 done，
        不再插入 user，避免历史里出现两条相同 user 把多轮改写搅乱；
      - 找不到（老客户端/直接调 API，没走阶段1）：补落一条 user，行为与旧版完全一致。
    """
    now = _now()
    with _db() as conn:
        row = conn.execute("""
            SELECT id, meta_json FROM messages
            WHERE session_id=? AND role='user'
            ORDER BY id DESC LIMIT 1
        """, (session_id,)).fetchone()
        uid = None
        if row is not None:
            try:
                m = json.loads(row["meta_json"] or "{}")
            except (ValueError, TypeError):
                m = {}
            if m.get("phase") == "pending":
                uid = row["id"]
                m["phase"] = "done"
                conn.execute(
                    "UPDATE messages SET query_rewritten=?, meta_json=? WHERE id=?",
                    (rewritten, json.dumps(m, ensure_ascii=False), uid),
                )
        if uid is None:
            # 兼容旧路径：阶段1没调用过，这里补落 user（add_message 内已含锁标题逻辑）
            conn.execute("""
                INSERT INTO messages
                    (session_id, role, content, query_original, query_rewritten, meta_json, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (session_id, "user", original, original, rewritten,
                  json.dumps({}, ensure_ascii=False), now))
            conn.execute("""
                UPDATE sessions SET title=? WHERE id=? AND title='新会话'
            """, (original.strip()[:24], session_id))
        conn.execute("""
            INSERT INTO messages
                (session_id, role, content, query_original, query_rewritten, meta_json, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (session_id, "assistant", answer_text, None, None,
              json.dumps(meta or {}, ensure_ascii=False), now))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))


def get_history(session_id: str, max_turns: int = 4) -> list[dict]:
    """取最近 N 轮 user/assistant 消息，供 query 改写使用。

    只取最后 max_turns*2 条，避免历史过长把 LLM prompt 撑爆、也避免远期
    对话干扰当前意图。返回按时间正序，格式 [{"role","content"}, ...]。
    """
    with _db() as conn:
        # 多取几条做余量：下面会剔除 pending 的 user（当前正在问、还没生成完的那句）
        rows = conn.execute("""
            SELECT role, content, meta_json FROM messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY id DESC LIMIT ?
        """, (session_id, max_turns * 2 + 6)).fetchall()
    out = []
    for r in rows:
        if r["role"] == "user":
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            # 当前正在处理的这句不能进历史，否则 query 改写会"改写自己"
            if meta.get("phase") == "pending":
                continue
        out.append({"role": r["role"], "content": r["content"]})
    out = out[:max_turns * 2]   # DESC 顺序下前 N*2 条即最近 N 轮
    out.reverse()               # 翻成正序供 prompt 使用
    return out


if __name__ == "__main__":
    # 自测：建库 -> 建会话 -> 存两轮 -> 读历史
    init_db()
    s = create_session("自测会话")
    add_message(s["id"], "user", "西华路肠粉", "西华路肠粉", "西华路肠粉")
    add_message(s["id"], "assistant", "找到 2 家", meta={"count": 2, "mode": "llm"})
    add_message(s["id"], "user", "那便宜点的呢", "那便宜点的呢", "西华路人均20以下的肠粉")
    print("会话:", get_session(s["id"]))
    print("历史:", get_history(s["id"]))
    print("列表:", list_sessions())
