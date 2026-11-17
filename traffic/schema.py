"""富化层数据契约（docs/02 §8.2）。

字段与粒度严格对齐文档 v1.1：
  - driving/cycling/walking → SimpleRoutePlan，只有 duration_sec，无 legs/polyline；
  - transit → TransitRoutePlan，带 legs[] 换乘明细。
Pydantic 模型同时承担"输出 schema 校验"，防止明细误塞进驾车字段（docs/02 §7.5）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["driving", "cycling", "walking", "transit"]


class Location(BaseModel):
    """解析后的位置：统一 GCJ-02，精度分级。"""
    lat: float
    lon: float
    coord: str = "GCJ-02"
    precision: Literal["precise", "city"] = "precise"
    source: str = "explicit"      # explicit / session / geolocation / ip / business_area
    display_name: str = ""


class SimpleRoutePlan(BaseModel):
    """driving / cycling / walking：v1.1 只给参考耗时，不含任何路径明细。"""
    shop_id: str
    mode: Mode
    available: bool = True
    duration_sec: int | None = None
    distance_m: int | None = None          # 内部字段：口径校验用，可不对外暴露
    source: str = "mock"                    # tencent-map/ws/... 或 mock
    fetched_at: datetime = Field(default_factory=datetime.now)
    degraded_reason: str | None = None


class TransitLeg(BaseModel):
    """一段行程：官方 steps[] 是 [步行, 公交, 步行, 公交, 步行] 交替结构。"""
    seq: int
    kind: Literal["walk", "transit"]
    duration_sec: int                       # 官方 duration(分钟) × 60
    distance_m: int
    instruction: str | None = None           # "步行 255 米，到达体育西路站"
    is_transfer_walk: bool = False           # 夹在两个 transit 段之间 → 换乘步行
    # --- transit 段（对齐官方 BUS / SUBWAY / RAIL）---
    vehicle: Literal["BUS", "SUBWAY", "RAIL"] | None = None
    line_name: str | None = None             # lines[0].title
    direction: str | None = None             # destination.title
    geton_station: str | None = None
    getoff_station: str | None = None
    station_count: int | None = None
    exit_name: str | None = None
    fare_yuan: float | None = None           # 官方 price(分) ÷ 100
    first_time: str | None = None
    last_time: str | None = None
    running_status: int | None = None        # 300 正常 / 301/302 警告 / 303 停运
    alternative_lines: list[str] = Field(default_factory=list)  # lines[1:] 线路名


class TransitRoutePlan(SimpleRoutePlan):
    """transit：完整换乘方案，默认只返回用时最少的一条。"""
    mode: Literal["transit"] = "transit"
    selected_policy: str = "LEAST_TIME"
    candidates_count: int = 1               # routes[] 里的备选方案总数
    selected_rank: int = 1                  # 选中方案按 duration 排序后的名次
    transfer_count: int = 0                 # transit 段数 - 1
    walk_total_sec: int = 0                 # 全程步行合计
    transfer_walk_sec: int = 0              # 仅换乘步行合计 ← 用户要的"中间走路多久"
    walk_total_m: int = 0
    fare_yuan: float | None = None
    legs: list[TransitLeg] = Field(default_factory=list)
    # 明确不返回：polyline / bounds / 站点 location / 任何坐标点串


class EnrichmentResult(BaseModel):
    """富化总结果：由 API 层合并进响应，与 answer/shops 平行。

    route_plans 用 dict 承载：SimpleRoutePlan / TransitRoutePlan 是联合结构，
    若声明为 list[SimpleRoutePlan] 会被 Pydantic 强制降级、丢掉 transit 的 legs。
    """
    route_plans: list[dict] = Field(default_factory=list)
    origin: Location | None = None
    status: Literal[
        "ok", "partial", "unavailable", "need_location", "invalid_coord", "fallback_static"
    ] = "ok"
    latency_ms: float = 0.0
    cache_hits: int = 0
    trace: dict = Field(default_factory=dict)   # 每次 tools/call 的耗时/失败模式
    message: str = ""
