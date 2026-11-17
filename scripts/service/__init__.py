"""FastAPI 服务层：把命令行 RAG 变成常驻 HTTP 服务。

模块划分：
    schemas.py  - Pydantic 请求/响应模型（参数校验 + 自动生成 OpenAPI 文档）
    app.py      - FastAPI 应用：lifespan 单例、各端点、SSE 流式
    （serve.py 在 scripts/ 根目录，是 uvicorn 启动入口）

核心设计：重资源（4.4GB 模型权重 + Qdrant + BM25）在 lifespan 里只加载一次，
之后所有请求共享同一个 RetrievalPipeline —— 这正是服务层要解决的核心痛点。
"""
