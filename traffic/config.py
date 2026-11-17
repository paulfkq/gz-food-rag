"""交通富化模块配置：合规白名单、供应商、超时/预算、缓存、传输。

全部走环境变量，代码里没有任何有效 key（docs/02 §2 强制红线）。
换机器 / 换供应商只改这里或环境变量，其他模块不感知。
"""
from __future__ import annotations

import os
from pathlib import Path

TRAFFIC_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TRAFFIC_ROOT.parent

# ---------------- 合规白名单（不可绕过）----------------
# 仅允许境内持牌服务商；Mapbox / Google / Apple / OSM 等境外数据一律禁用。
ALLOWED_PROVIDERS = ("tencent", "amap", "baidu", "tianditu")
COORD_SYSTEM = "GCJ-02"          # 店铺 geo 与本模块内部统一火星坐标

# ---------------- 供应商选择 ----------------
# tencent=轨道 B 自建 MCP 封装腾讯 WebService；amap/baidu 预留适配位。
# mock=确定性模拟数据，无 key 时自动降级到这里（不视为真实数据）。
MAP_PROVIDER = os.environ.get("TRAFFIC_MAP_PROVIDER", "tencent").lower()

# API key 只从环境变量读；绝不写进代码 / 配置 / 日志 / LLM 上下文。
KEY_ENV_VAR = "TENCENT_MAP_KEY"
MAP_API_KEY = os.environ.get(KEY_ENV_VAR, "").strip()

# 是否有真实 key：没有就降级 mock。这是"不内嵌 key"红线的落地开关。
HAS_REAL_KEY = bool(MAP_API_KEY)

# ---------------- 性能预算（docs/02 §9）----------------
MODE_TIMEOUT_SEC = float(os.environ.get("TRAFFIC_MODE_TIMEOUT", "1.5"))   # 单 mode 超时
# 真 key 下 4 mode × N 店要顺序发多次请求 + 120 退避，2.5s 会把后到的 mode 结果丢掉；
# 放宽到 6s 保证四档都能收齐（仍是检索后旁路，失败也静默，不影响 RAG 正确性）。
TOTAL_BUDGET_SEC = float(os.environ.get("TRAFFIC_TOTAL_BUDGET", "6.0"))   # 富化总预算
MAX_WORKERS = 8                  # 并发 worker（Top-K=8，4 组 mode 并发）
# 腾讯免费/个人 Key 的 QPS 很低（实测并发 9 即触发 status=120「此key每秒请求量已达到上限」）。
# 全局最小调用间隔做令牌节流，保证瞬时并发不会撞 QPS 上限；
# 同时对 status=120 做少量退避重试。两者可独立关掉（间隔设 0 / 重试设 0）。
CALL_MIN_INTERVAL_SEC = float(os.environ.get("TRAFFIC_CALL_INTERVAL", "0.3"))  # ≥0.3s ≈ ≤3 QPS
QPS_RETRIES = int(os.environ.get("TRAFFIC_QPS_RETRIES", "2"))   # status=120 最多重试次数
QPS_RETRY_BACKOFF_SEC = float(os.environ.get("TRAFFIC_QPS_BACKOFF", "0.4"))
# 每次提问最多富化几家店：逐店逐 mode 调用，Top-K=8 时一次最多 8×4=32 次地图请求。
# 新 Key 日额度较小时设为 3（12 次/问），避免几轮调试就见底。
MAX_SHOPS = max(1, int(os.environ.get("TRAFFIC_MAX_SHOPS", "8")))

# ---------------- 缓存（docs/02 §9）----------------
CACHE_TTL_SEC = int(os.environ.get("TRAFFIC_CACHE_TTL", "300"))  # TTL 5min
ORIGIN_GRID_M = 100              # 起点网格化到 ~100m，提升命中率
DEPARTURE_BUCKET_SEC = 300       # 出发时刻桶 5min（公交首末班相关）

# ---------------- 出行方式 ----------------
ALLOWED_MODES = ("driving", "cycling", "walking", "transit")

# ---------------- 公交策略 ----------------
TRANSIT_POLICY = "LEAST_TIME"    # 显式写出，抵御厂商改默认值
TRANSIT_ALTERNATIVES = 0         # 默认只返 1 条方案，控 payload
PRICE_UNIT = 1                   # 让官方把 price 统一成"分"再换元

# ---------------- MCP 传输 ----------------
MCP_BIND = "127.0.0.1"           # 本地服务禁止 0.0.0.0（防 DNS rebinding）
MCP_PORT = 8100                  # 独立端口，不占主链路 8000
MCP_PATH = "/mcp"

# ---------------- 口径一致性（docs/02 §7.2）----------------
# 路线里程 / 直线距离 的合理上界，超出标异常（道路绕行通常 < 2.5 倍）。
ROUTE_TO_STRAIGHT_MAX_RATIO = 2.5
