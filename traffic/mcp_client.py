"""MCP Client：统一调用接口 + 三种传输实现（docs/02 §4 / §5 / §8）。

轨道对比（可讲的消融）：
  - InProcessMcpClient：进程内直连自建 Server 的 dispatcher（默认，零网络开销，便于测试）；
  - HttpMcpClient：Streamable HTTP（POST /mcp），对接官方托管 Server 或独立部署的自建 Server；
  - StdioMcpClient：子进程 stdio（框架实现，演示协议握手）。

关键认知：用 MCP ≠ 不用 function calling。这里由代码确定性调用 tools/call
（触发条件固定、参数固定），不让 LLM 自主决策——见 docs/02 §4 的取舍。
"""
from __future__ import annotations

import json
import subprocess
import sys
from abc import ABC, abstractmethod


class McpClient(ABC):
    @abstractmethod
    def list_tools(self) -> list[dict]:
        ...

    @abstractmethod
    def call_tool(self, name: str, arguments: dict) -> dict:
        ...

    def health(self) -> dict:
        return self.call_tool("health", {}).get("structuredContent", {})


class InProcessMcpClient(McpClient):
    """默认：进程内直连自建 Server 的分发器，无序列化开销。"""

    def __init__(self, handler=None):
        if handler is None:
            from .mcp_server.server import build_handler
            handler = build_handler()
        self.handler = handler

    def list_tools(self) -> list[dict]:
        resp = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return resp["result"]["tools"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self.handler.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return resp.get("result", {"isError": True, "content": [], "error": resp.get("error")})


class HttpMcpClient(McpClient):
    """Streamable HTTP：POST /mcp，带 Mcp-Method / Mcp-Name 头；含向后兼容回退。"""

    def __init__(self, base_url: str, timeout: float = 3.0):
        import httpx
        self.url = base_url.rstrip("/") + "/mcp"
        self.timeout = timeout
        self._ids = 0
        self.httpx = httpx

    def _request(self, method: str, params: dict | None = None) -> dict:
        with self.httpx.Client(timeout=self.timeout, trust_env=False) as c:
            payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
            if params is not None:
                payload["params"] = params
            headers = {"Mcp-Method": method, "Mcp-Name": "gz-food-client",
                       "Content-Type": "application/json"}
            resp = c.post(self.url, json=payload, headers=headers)
            # 兼容回退：旧端点返回 400/404/405 时，尝试无头 POST（旧 HTTP+SSE 兼容形态）
            if resp.status_code in (400, 404, 405):
                resp = c.post(self.url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            return resp.json()

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    def list_tools(self) -> list[dict]:
        return self._request("tools/list")["result"]["tools"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments})["result"]


class StdioMcpClient(McpClient):
    """子进程 stdio：启动自建 Server 的 stdio 传输，按行交换 JSON-RPC。"""

    def __init__(self, python_exe: str | None = None, cwd: str | None = None):
        self.python_exe = python_exe or sys.executable
        self.cwd = cwd
        self._proc = None
        self._ids = 0

    def _ensure(self):
        if self._proc is None:
            self._proc = subprocess.Popen(
                [self.python_exe, "-m", "traffic.mcp_server.server", "--stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=self.cwd,
            )

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._ensure()
        self._ids += 1
        msg = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        return json.loads(line) if line else {"error": {"message": "stdio closed"}}

    def list_tools(self) -> list[dict]:
        return self._request("tools/list")["result"]["tools"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments})["result"]


def make_client(kind: str = "inproc", **kw) -> McpClient:
    """工厂：inproc / http / stdio，供轨道 A/B 与测试切换。"""
    kind = kind.lower()
    if kind == "inproc":
        return InProcessMcpClient(**kw)
    if kind == "http":
        return HttpMcpClient(**kw)
    if kind == "stdio":
        return StdioMcpClient(**kw)
    raise ValueError(f"未知 MCP client 类型: {kind}")
