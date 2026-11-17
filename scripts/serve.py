"""RAG 服务启动入口。

用法（在 scripts/ 目录下）：
    python serve.py                      # 默认 127.0.0.1:8000
    python serve.py --port 9000          # 指定端口
    python serve.py --host 0.0.0.0       # 允许局域网访问（注意安全）

启动后：
    Swagger 文档  http://127.0.0.1:8000/docs
    健康检查      http://127.0.0.1:8000/health

注意：
    - 首次启动要加载约 4.4GB 模型，约 10~40s（视磁盘冷热），期间 /health 返回 loading。
    - 加载完成后所有请求复用同一份模型，不再重复加载 —— 这就是服务层的价值。
    - uvicorn 单 worker 即可：模型占内存大，多 worker 会重复加载多份；
      CPU 密集场景靠线程池并发，不靠多进程。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重启（会重复加载模型）")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    print(f"[serve] 启动 RAG 服务 http://{args.host}:{args.port}  （文档 /docs）", flush=True)
    uvicorn.run(
        "service.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        # 单 worker：模型内存大，避免多进程各加载一份 4.4GB
        workers=1,
    )


if __name__ == "__main__":
    main()
