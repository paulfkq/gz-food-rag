"""检索层全局配置：路径、模型、常量集中一处，便于换环境。

新手提示：把"路径 / 模型名"这种会变的量集中到一个文件，是工程里最基本的
可维护性习惯。换机器时只改这里，其他模块不用动。
"""
import os
from pathlib import Path

# scripts/retrieve/config.py -> parents[2] 就是项目根 gz-food-rag/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHUNKS_PATH = PROJECT_ROOT / "data" / "chunks" / "chunks.jsonl"
# 默认路径不变；可用 RAG_QDRANT_PATH 指向副本，供评估进程与在线服务并行
# （Qdrant embedded 是单进程独占，同一路径不能被两个进程同时打开）
QDRANT_PATH = Path(os.environ.get("RAG_QDRANT_PATH") or (PROJECT_ROOT / "data" / "qdrant"))
BM25_CACHE_PATH = PROJECT_ROOT / "data" / "bm25.pkl"

COLLECTION = "gz_food_chunks"

# 本地权重目录（已下载好，不联网）
BGE_M3_PATH = "D:/向量模型下载与部署/models/BAAI/bge-m3"
RERANKER_PATH = "D:/向量模型下载与部署/models/BAAI/bge-reranker-v2-m3"

# 检索参数
DENSE_TOPK = 50        # dense 路每路召回数
SPARSE_TOPK = 50
BM25_TOPK = 50
RRF_K = 60             # RRF 平滑常数，论文默认值
RRF_TOPN = 50          # 融合后送入 rerank 的候选数
RERANK_TOPN = 8        # 最终返回给上层的店数

# 广州菜系/商圈/区（与种子数据对齐，供规则槽位解析匹配）
DISTRICTS = ["越秀区", "天河区", "荔湾区", "海珠区", "番禺区", "黄埔区", "白云区"]
CUISINES = ["粤菜", "粥粉面", "烧腊", "海鲜", "广式点心", "宵夜", "湘菜", "素食",
            "清真菜", "西北菜", "客家菜", "川菜", "潮汕菜", "火锅"]
NOISE_LEVELS = ["安静", "适中", "吵"]
DIETARY_FLAGS = {"清真": "清真", "全素": "全素", "素食": "全素", "不吃猪肉": "不含猪肉"}
