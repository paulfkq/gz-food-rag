"""护栏：参数白名单 / Origin 校验 / 坐标系校验 / 口径一致性 / 降级判定。

这些是 MCP Server 与应用层的安全边界（docs/02 §2 / §5 / §7 / §10）。
"""
from __future__ import annotations

from . import config
from .location import _out_of_china
from .schema import EnrichmentResult, SimpleRoutePlan, TransitRoutePlan

VALID_MODES = ("driving", "cycling", "walking", "transit")


def validate_modes(modes: list[str]) -> list[str]:
    """参数白名单：非法 mode 直接剔除，不传给 provider。"""
    cleaned = [m for m in modes if m in VALID_MODES]
    return cleaned or ["walking"]


def validate_coord(lat: float, lon: float, coord: str = "GCJ-02") -> str | None:
    """坐标系/范围校验；返回错误原因（None=通过）。"""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "coord_out_of_range"
    if coord != config.COORD_SYSTEM:
        return "coord_mismatch"               # 上层标 invalid_coord，不发起调用
    if not _out_of_china(lat, lon):
        return None
    return "outside_mainland"                  # 境内地图服务不覆盖境外坐标


def check_origin_header(origin: str | None, allowed_origins: set[str] | None = None) -> bool:
    """Streamable HTTP 必须校验 Origin 防 DNS rebinding（docs/02 §5）。

    本地 stdio / 服务端内部调用无 Origin 头，视为可信来源放行；
    浏览器类请求带 Origin 时必须在白名单内。
    """
    if not origin:
        return True
    allowed = allowed_origins or {
        f"http://127.0.0.1:{config.MCP_PORT}",
        f"http://localhost:{config.MCP_PORT}",
    }
    return origin in allowed


def decide_status(plans: list[SimpleRoutePlan], location_ok: bool) -> tuple[str, str]:
    """降级矩阵（docs/02 §10）：根据可用方案数给出 enrichment_status。

    返回 (status, message)。铁律：任何情况都不影响 RAG 的 answer/shops。
    """
    if not location_ok:
        return "need_location", "无法获取可用位置（城市级定位不用于步行/骑行），请指定起点或商圈"
    if not plans:
        return "unavailable", "地图服务不可用，交通信息已省略，推荐结果不受影响"
    available = [p for p in plans if p.available]
    if not available:
        return "unavailable", "所有交通方案暂不可用，推荐结果不受影响"
    degraded = [p for p in plans if p.degraded_reason]
    if len(available) == len(plans) and not degraded:
        return "ok", ""
    names = ",".join(p.mode for p in degraded) if degraded else "部分方案"
    return "partial", f"部分方案降级（{names}），其余照常返回"


def city_level_blocked(precision: str, mode: str) -> bool:
    """城市级定位不用于步行/骑行（docs/02 §10）。公交也可给出但精度差，统一拦截。"""
    return precision == "city" and mode in {"walking", "cycling", "transit"}
