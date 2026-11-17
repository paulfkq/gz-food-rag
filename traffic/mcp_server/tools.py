"""MCP 工具集（docs/02 §8：自建 Server，控制在 5 个以内）。

  resolve_location / route_plans / route_matrix / poi_validate / health

tools/list 暴露 JSON Schema，tools/call 执行。工具内部：
  参数白名单 → provider 调用 → normalize 归一化 → 结构化输出（已去 polyline）。
聚合工具 route_plans 是自建核心价值：一次调用内部并发 4 种 mode，
把并发/超时/缓存/部分失败收口在 Server 内，应用侧只见一次调用。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..normalize import normalize_plan
from ..providers.base import MapProvider

# ---------------- tools/list：工具 schema ----------------

TOOLS = [
    {
        "name": "resolve_location",
        "description": "统一位置解析：地址或 IP → 坐标（GCJ-02），IP 结果标 precision=city",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "地址文本，如'体育西路'"},
                "ip": {"type": "string", "description": "IP 地址，仅城市级兜底"},
            },
        },
    },
    {
        "name": "route_plans",
        "description": "聚合工具：一个起点+K个终点+4种出行方式，返回 RoutePlan[]（transit 含换乘明细）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "required": ["lat", "lon"]},
                "destinations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "shop_id": {"type": "string"},
                            "geo": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}},
                        },
                    },
                },
                "modes": {"type": "array", "items": {"type": "string", "enum": list(config.ALLOWED_MODES)}},
                "departure_time": {"type": "integer", "description": "Unix 秒，决定哪些公交线仍在运营"},
                "policy": {"type": "string", "enum": ["LEAST_TIME", "LEAST_TRANSFER", "LEAST_WALKING", "RECOMMEND"]},
            },
            "required": ["origin", "destinations", "modes"],
        },
    },
    {
        "name": "route_matrix",
        "description": "批量距离矩阵：driving/walking 一次算多目的地（减少调用量）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "object"},
                "destinations": {"type": "array", "items": {"type": "object"}},
                "mode": {"type": "string", "enum": ["driving", "walking"]},
            },
            "required": ["origin", "destinations", "mode"],
        },
    },
    {
        "name": "poi_validate",
        "description": "可选：校验店铺 POI 与 geo 是否一致",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shop_name": {"type": "string"},
                "city": {"type": "string"},
                "near": {"type": "object"},
            },
            "required": ["shop_name"],
        },
    },
    {
        "name": "health",
        "description": "可观测：配额余量 / 延迟 / 是否真实数据",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolDispatcher:
    """tools/call 分发器：持有 provider，执行工具并返回结构化结果。"""

    def __init__(self, provider: MapProvider):
        self.provider = provider

    def list_tools(self) -> list[dict]:
        return TOOLS

    def call(self, name: str, arguments: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}
        try:
            data = handler(arguments or {})
            return {"content": [{"type": "text", "text": _to_json(data)}], "structuredContent": data}
        except Exception as e:
            return {"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}

    # ---- 各工具实现 ----

    def _tool_resolve_location(self, args: dict) -> dict:
        loc = self.provider.resolve_location(
            address=args.get("address"), ip=args.get("ip")
        )
        return loc or {"error": "no_location"}

    def _tool_route_plans(self, args: dict) -> dict:
        from ..guard import validate_modes
        origin = args["origin"]
        destinations = args.get("destinations", [])
        modes = validate_modes(args.get("modes", ["driving"]))
        departure = args.get("departure_time")
        policy = args.get("policy", config.TRANSIT_POLICY)
        source = "tencent-map" if self.provider.name == "tencent" else "mock"

        # provider 一次拿回所有 mode 的原始响应（内部已并发）
        raw_map = self.provider.route_plans(origin, destinations, modes, departure, policy)
        plans = []
        for mode in modes:
            raws = raw_map.get(mode, [])
            for shop, raw in zip(destinations, raws):
                plans.append(
                    normalize_plan(
                        shop.get("shop_id", "?"), mode, raw, source,
                        policy=policy, alternatives=args.get("transit_alternatives", 0),
                    ).model_dump(mode="json")
                )
        return {"plans": plans, "count": len(plans), "source": source}

    def _tool_route_matrix(self, args: dict) -> dict:
        # 批量接口优化位：这里复用 route_plans 的单 mode 逻辑，结构一致。
        # 真实 provider 可在 TencentProvider 内改走 /ws/distance/v1/matrix。
        return self._tool_route_plans({
            **args,
            "modes": [args.get("mode", "driving")],
        })

    def _tool_poi_validate(self, args: dict) -> dict:
        return {
            "shop_name": args.get("shop_name"),
            "validated": False,
            "note": "POI 校验为可选工具，mock/未配置时返回 validated=False",
        }

    def _tool_health(self, args: dict) -> dict:
        return self.provider.health()


def _to_json(obj) -> str:
    import json
    from datetime import datetime
    return json.dumps(obj, ensure_ascii=False, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))
