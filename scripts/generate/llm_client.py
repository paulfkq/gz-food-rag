"""LLM 客户端抽象：OpenAI 兼容协议，本地 / 云端 / 不同厂商一键切换。

为什么不绑死某一家（面试讲点）：
    生成层是 RAG 里最容易被厂商绑定的地方。抽象一层 LLMClient 后，
    base_url / model / api_key 全走配置 —— 本地 glm / DeepSeek / Qwen / 云端 GPT
    只改三个环境变量即可切换，检索层代码一行不用动。

关键工程点：
    1. 超时 + 重试：本地/云 API 抖动是常态，不能让一次超时拖垮整条 RAG 链路。
    2. 降级：LLM 不可用时返回 None，由上层用模板兜底（答案可降级，系统不能挂）。
    3. reasoning 模型注意：glm-5.3-flash 之类会把答案放在 content，思考放在
       reasoning_content。我们只读 content，别把思考过程当答案输出。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass
class LLMConfig:
    api_key: str
    base_url: str | None
    model: str
    temperature: float = 0.0
    max_tokens: int = 600
    timeout: float = 60.0
    retries: int = 1

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            api_key=os.environ.get("RAG_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("RAG_LLM_BASE_URL"),
            model=os.environ.get("RAG_LLM_MODEL", "glm-5.3-flash"),
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)


class LLMClient:
    """薄封装：只做 chat 文本生成。失败返回 None，绝不抛给上层。"""

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()
        self._client = None
        if self.config.available:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    api_key=self.config.api_key,
                    base_url=self.config.base_url,
                    timeout=self.config.timeout,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[llm] 客户端初始化失败，将走降级：{e}")

    def chat(self, system: str, user: str) -> str | None:
        """返回助手的 content 文本；不可用/失败返回 None。"""
        if self._client is None:
            return None
        last = None
        for attempt in range(self.config.retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    stream=False,
                )
                content = resp.choices[0].message.content
                # reasoning 模型偶尔 content 为空（思考耗尽 max_tokens）-> 视为失败
                return content.strip() if content and content.strip() else None
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self.config.retries:
                    time.sleep(0.5 * (attempt + 1))
        print(f"[llm] 调用失败，将走模板降级：{type(last).__name__}: {str(last)[:160]}")
        return None

    def chat_stream(self, system: str, user: str):
        """流式生成：逐块 yield 正文 content 字符串。

        设计要点（面试可讲）：
            1. reasoning 模型（glm-5.3-flash）的 thinking 走 reasoning_content，
               正文走 content —— 我们【只 yield content】，绝不把思考过程吐给用户。
            2. 流式是"边生成边吐"，拿不到完整答案，所以【无法在吐之前做 guard 校验】。
               这是流式 RAG 的固有矛盾：调用方要么接受"先吐后校验"，
               要么把流缓冲成全文再校验（那就失去打字机体验）。服务端把它做成
               策略开关，交给前端/场景权衡（详见 docs/08）。
            3. 生成器用 try/except 包住：中途断流时 yield 一个特殊标记不可行
               （会和正文混淆），所以错误通过 logger 记录，调用方对比已收长度
               与预期即可发现截断。这里选择"宁可少吐，不吐脏数据"。
            4. 不可用（无 key）时立即返回空生成器，调用方走模板降级。

        Yields:
            str: 正文增量片段（delta.content）。
        """
        if self._client is None:
            return  # 空生成器 -> 调用方降级
        try:
            stream = self._client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 只读 content，忽略 reasoning_content（思考过程不外泄）
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
        except Exception as e:  # noqa: BLE001
            print(f"[llm] 流式调用中断：{type(e).__name__}: {str(e)[:160]}")
