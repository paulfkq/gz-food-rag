"""MCP Server：JSON-RPC 2.0 处理器 + 双传输（Streamable HTTP / stdio）。

协议要点（docs/02 §5）：
  - 方法：initialize（能力协商）、tools/list、tools/call、ping；
  - Streamable HTTP：POST /mcp，带 Mcp-Method / Mcp-Name 头便于网关路由；
  - 必须校验 Origin 防 DNS rebinding；本地只绑 127.0.0.1；
  - 向后兼容回退：客户端收到 400/404/405 时尝试旧传输（本 Server 同时提供）。
这里用纯 JSON-RPC 2.0 实现（不依赖 mcp SDK，环境未安装且避免依赖冲突），
协议语义与官方 SDK 一致，面试可讲"我知道协议层在做什么"。
"""
from __future__ import annotations

import json
import sys

from fastapi import APIRouter, Request  # 模块级导入：配合 future annotations 让 FastAPI 能解析 Request 类型
from fastapi.responses import JSONResponse

from .. import config
from ..guard import check_origin_header
from .tools import ToolDispatcher

SERVER_INFO = {"name": "gz-food-traffic-mcp", "version": "1.0.0"}
PROTOCOL_VERSION = "2025-06-18"   # 2026 修订后的无状态版本；兼容回退见 handle()


class JsonRpcHandler:
    """无状态 JSON-RPC 2.0 处理器：一条消息进，一条消息出。"""

    def __init__(self, dispatcher: ToolDispatcher):
        self.dispatcher = dispatcher

    def handle(self, msg: dict, origin: str | None = None) -> dict | None:
        # Origin 校验（Streamable HTTP 场景）：失败直接 403，由 transport 层转成 HTTP 状态
        if not check_origin_header(origin):
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
                "code": -32000, "message": "Forbidden: invalid Origin"}}

        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")

        # 通知（无 id）不返回响应
        if req_id is None:
            return None

        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            return self._ok(req_id, {"tools": self.dispatcher.list_tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            result = self.dispatcher.call(name, arguments)
            return self._ok(req_id, result)
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    @staticmethod
    def _ok(req_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def handle_batch(self, payload, origin: str | None = None):
        """支持单条 / 批量 JSON-RPC；通知类不返回。"""
        if isinstance(payload, list):
            out = [self.handle(m, origin) for m in payload]
            return [x for x in out if x is not None] or None
        return self.handle(payload, origin)


def build_handler(provider=None):
    """工厂：构造 dispatcher + handler。不传 provider 时按配置自动选（含无 key 降级 mock）。"""
    from ..providers.base import make_provider
    p = provider or make_provider(config.MAP_PROVIDER)
    return JsonRpcHandler(ToolDispatcher(p))


# ---------------- stdio 传输 ----------------
def run_stdio(handler: JsonRpcHandler) -> None:
    """逐行读取 JSON-RPC（stdin），逐行写回（stdout）。stderr 只写日志，不污染协议。"""
    import sys
    print("[mcp-stdio] ready", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = handler.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }) + "\n")
            sys.stdout.flush()


# ---------------- Streamable HTTP 传输（FastAPI router）----------------

def make_router(handler: JsonRpcHandler):
    """返回一个 FastAPI APIRouter，挂载到任意 app 的 /mcp。"""
    router = APIRouter()

    @router.post(config.MCP_PATH)
    async def mcp_post(request: Request):
        origin = request.headers.get("origin")
        if not check_origin_header(origin):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32000, "message": "Forbidden: invalid Origin"}},
                status_code=403,
            )
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )
        resp = handler.handle_batch(payload, origin)
        # 2026 修订：回带 Mcp-Method / Mcp-Name 头便于网关不解析包体即可路由计量
        method = payload.get("method") if isinstance(payload, dict) else "batch"
        headers = {"Mcp-Method": str(method or ""), "Mcp-Name": SERVER_INFO["name"]}
        if resp is None:                      # 纯通知：202 Accepted，无 body
            return JSONResponse(status_code=202, headers=headers, content=None)
        return JSONResponse(resp, headers=headers)

    @router.get(config.MCP_PATH)
    async def mcp_get(request: Request):
        # 向后兼容：旧版 HTTP+SSE 客户端会先 GET；这里给出明确降级提示，
        # 新版无状态客户端不依赖 GET 流（docs/02 §5）。
        return JSONResponse({
            "mcp": "streamable-http", "protocolVersion": PROTOCOL_VERSION,
            "note": "本 Server 无状态，请用 POST /mcp 发送 JSON-RPC；GET 流已废弃",
            "tools": len(handler.dispatcher.list_tools()),
        })

    return router


if __name__ == "__main__":
    # stdio 传输入口：python -m traffic.mcp_server.server --stdio
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true", help="以 stdio 传输运行")
    args = ap.parse_args()
    handler = build_handler()
    if args.stdio:
        run_stdio(handler)
    else:
        print("use --stdio for stdio transport, or import make_router() for HTTP", file=sys.stderr)
