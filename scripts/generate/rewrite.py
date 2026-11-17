"""多轮对话的 query 改写（指代消解 / contextual query rewrite）。

这是多轮 RAG 的关键环节，面试高频考点。

为什么需要（新手向）：
    用户第二轮常说的是【省略句/指代句】，比如"那便宜点的呢""有没有安静的"。
    这种句子单独拿去检索，槽位解析器提取不到区域/价格/品类，必然召回失败。
    但结合历史看，"那"指向上一轮的商圈和品类，"便宜点"是对价格的修改。
    所以要在检索【之前】加一步：把「历史 + 当前输入」改写成一条自包含 query。

设计选择（延续本项目的"哑铃结构"）：
    - 入口（改写 + 槽位）和出口（答案生成）用 LLM；中间检索链路保持确定性；
    - 改写结果必须【可观测】：返回 changed/reason，前端展示"原始 → 改写"，
      用户能看懂系统理解了什么，出错也能一眼发现（可解释性 > 黑盒）。

降级策略（答案可降级，系统不能挂）：
    - 首轮 / 无历史 -> 直接返回原 query（省一次 LLM 调用）
    - 没配 key / 调用失败 / 返回空 -> 返回原 query，reason 标注降级
    - 历史只取最近 N 轮，避免 prompt 过长 + 远期对话干扰
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .llm_client import LLMClient


@dataclass
class RewriteResult:
    query: str            # 改写后的 query（用于检索）
    changed: bool         # 是否真的改写了
    reason: str           # 说明：no_history / ok / fallback 等
    raw: str = ""         # LLM 原始返回（调试用）

    def to_dict(self) -> dict:
        return {"query": self.query, "changed": self.changed, "reason": self.reason}


# 指代/省略信号词：命中才值得调 LLM（纯关键词触发，省调用）
FOLLOWUP_HINTS = (
    "那", "这个", "那个", "它", "同样", "一样", "再", "还", "别的", "其他",
    "便宜", "贵", "安静", "近", "远", "附近", "附近有没有", "换成", "改",
    "去掉", "不要了", "加上", "有没有", "呢", "？", "?", "的话", "左右",
)

SYSTEM_PROMPT = """你是美食检索系统的"多轮查询改写器"。

任务：根据对话历史，把用户【最新一句输入】改写成一条【自包含的检索 query】。
改写后的 query 会被丢进餐厅检索系统，所以必须包含检索需要的全部条件。

规则（严格遵守）：
1. 继承历史中仍然有效的条件：城市/区域/商圈、品类、价格上限、口味、忌口、环境偏好等。
2. 用户在最新一句里【明确修改或取消】的条件，以最新一句为准：
   - "便宜点/别那么贵" -> 把价格上限下调（如 30 -> 20）
   - "贵一点/预算放宽" -> 上调价格
   - "换成粤菜" -> 替换菜系；"不要香菜" -> 追加忌口
3. 解析指代词："那里/那边" = 上一轮提到的商圈；"第二个" = 上一轮的第二家店。
4. 只输出改写后的 query 本身，不要解释、不要加引号、不要加前缀（如"改写后："）。
5. 保持简短（30 字以内），只保留检索相关的条件，删掉礼貌用语和语气词。

示例：
历史：
  用户：西华路人均30以下的肠粉
  助手：为你找到2家西华路肠粉店
最新输入：那便宜点的呢
改写输出：西华路人均20以下的肠粉

历史：
  用户：珠江新城适合约会的安静粤菜
  助手：为你找到3家
最新输入：有没有包间
改写输出：珠江新城安静有包间的粤菜
"""


def _build_user_prompt(history: list[dict], query: str) -> str:
    lines = []
    for h in history[-6:]:  # 最近 3 轮（user/assistant 各一条）
        role = "用户" if h["role"] == "user" else "助手"
        content = h.get("content", "")
        # 助手答案可能很长，截断避免 prompt 膨胀（历史只需要意图线索，不需要全文）
        if len(content) > 160:
            content = content[:160] + "…"
        lines.append(f"{role}：{content}")
    lines.append(f"最新输入：{query}")
    lines.append("改写输出：")
    return "\n".join(lines)


def _clean(raw: str) -> str:
    """清洗 LLM 输出：去引号、去多余前缀、去换行。"""
    s = (raw or "").strip()
    # 常见包装：改写后："xxx" / "xxx" / ```xxx```
    s = re.sub(r"^改写后[：:]\s*", "", s)
    s = re.sub(r"^```(?:text)?\s*|\s*```$", "", s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”‘’":
        s = s[1:-1].strip()
    s = s.split("\n")[0].strip()  # 只取第一行，防止模型啰嗦解释
    # 去掉过长的输出（超过 60 字基本是模型跑偏了，原 query 更安全）
    return s if 0 < len(s) <= 60 else ""


def rewrite_query(
    query: str,
    history: list[dict],
    llm: LLMClient | None = None,
    max_turns: int = 3,
) -> RewriteResult:
    """把当前 query 结合历史改写成自包含 query。

    Args:
        query:   用户本轮原始输入
        history: [{"role": "user"/"assistant", "content": "..."}, ...] 按时间正序
        llm:     LLMClient 实例；None 时从环境变量新建一个
        max_turns: 最多参考最近几轮

    Returns:
        RewriteResult（永远有值，失败时 query 就是原输入）
    """
    # 首轮 / 空历史：无需改写，直接返回（省一次 LLM 调用 + 2s 延迟）
    if not history:
        return RewriteResult(query=query, changed=False, reason="no_history")

    client = llm if llm is not None else LLMClient()
    if not client.config.available or client._client is None:
        return RewriteResult(query=query, changed=False, reason="llm_not_configured")

    # 快速判断：如果当前输入已经自包含（长句 + 无指代信号），跳过改写省调用。
    # 注意这只是"省调用"的优化，判断错了（该改没改）顶多退化成单轮查询，不会出错。
    recent_user = [h["content"] for h in history if h["role"] == "user"]
    is_followup = (
        len(query) <= 20
        and any(h in query for h in FOLLOWUP_HINTS)
        and bool(recent_user)
    )
    if not is_followup:
        return RewriteResult(query=query, changed=False, reason="self_contained")

    user_prompt = _build_user_prompt(history[-max_turns * 2:], query)
    raw = client.chat(SYSTEM_PROMPT, user_prompt)
    cleaned = _clean(raw or "")
    if not cleaned:
        return RewriteResult(query=query, changed=False, reason="llm_empty", raw=raw or "")

    # 改写结果和原句一样 -> 没必要标 changed（前端不用显示改写标签）
    changed = cleaned != query.strip()
    return RewriteResult(
        query=cleaned, changed=changed,
        reason="ok" if changed else "no_change", raw=cleaned,
    )


if __name__ == "__main__":
    # 自测：模拟两轮历史
    hist = [
        {"role": "user", "content": "西华路人均30以下的肠粉"},
        {"role": "assistant", "content": "为你找到2家西华路肠粉店：合兴庭、桥头轩"},
    ]
    for q in ["那便宜点的呢", "有没有安静点的", "换成粥粉面"]:
        r = rewrite_query(q, hist)
        print(f"Q: {q}\n  -> {r.query}  (changed={r.changed}, reason={r.reason})")
