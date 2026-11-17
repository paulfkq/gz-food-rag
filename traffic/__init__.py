"""实时交通富化与 MCP 集成（Phase 5.5）——独立模块。

设计红线（docs/02）：
  1. 实时数据只做 post-retrieval enrichment，不进向量库 / filter / RRF / Rerank，
     不改变 Top-K 集合与顺序，也不进 LLM 的事实生成。
  2. 无地图 key 时自动降级确定性 mock，并在响应里标注 source=mock，绝不伪装真实数据。

本模块不依赖、也不修改 scripts/ 与 front/，可独立运行（默认端口 8100）。
"""
