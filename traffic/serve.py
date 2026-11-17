"""交通富化服务启动入口：独立端口 8100，不影响主链路 8000/5173。

用法：
  python -m traffic.serve                     # 默认 8100
  TRAFFIC_MAP_PROVIDER=tencent TENCENT_MAP_KEY=<你的key> python -m traffic.serve
无 key 时自动用 mock 数据，响应标注 real_data=false，不伪装真实。
"""
from __future__ import annotations

import sys

import uvicorn

from . import config


def main():
    if "--stdio" in sys.argv:
        from .mcp_server.server import build_handler, run_stdio
        run_stdio(build_handler())
        return
    uvicorn.run(
        "traffic.app:app",
        host=config.MCP_BIND, port=config.MCP_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
