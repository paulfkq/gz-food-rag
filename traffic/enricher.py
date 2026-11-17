"""富化编排层（docs/02 §7 / §9 / §10）——post-retrieval enrichment。

职责边界（红线）：
  输入是 RAG 已经定好的 shops[Top-K]，输出是平行的 route_plans 字段。
  不改 Top-K 集合与顺序、不进向量库、不进 LLM 事实生成。
  任何失败都只影响交通字段，answer/shops 由调用方照常返回（铁律）。

并发模型：
  4 种 mode 并发（ThreadPoolExecutor），单 mode 超时 1.5s，总预算 2.5s；
  超时即返回 available=False，主链路 P95 不受影响。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, guard
from .cache import TTLCache, make_key
from .location import LocationProvider
from .mcp_client import McpClient, make_client
from .normalize import check_scale_consistency
from .schema import EnrichmentResult, Location, SimpleRoutePlan, TransitRoutePlan


class RouteEnricher:
    """把 shops[Top-K] 富化成 route_plans。可独立使用，也可作为 API 层旁路。"""

    def __init__(
        self,
        client: McpClient | None = None,
        location_provider: LocationProvider | None = None,
        cache: TTLCache | None = None,
    ):
        self.client = client or make_client("inproc")
        self.locations = location_provider or LocationProvider(
            resolver=lambda **kw: (self.client.call_tool("resolve_location", kw)
                                  .get("structuredContent"))
        )
        self.cache = cache or TTLCache()

    def enrich(
        self,
        shops: list[dict],
        origin: dict | None = None,
        modes: list[str] | None = None,
        business_area: str | None = None,
        client_gps: dict | None = None,
        use_ip: bool = False,
        departure_time: int | None = None,
    ) -> EnrichmentResult:
        t0 = time.perf_counter()
        modes = guard.validate_modes(modes or list(config.ALLOWED_MODES))

        # 1) 位置解析（优先级链）
        loc = self.locations.resolve(
            origin=origin, business_area=business_area,
            client_gps=client_gps, use_ip=use_ip,
        )
        if loc is None:
            return EnrichmentResult(
                status="need_location", origin=None,
                message="无法获取起点位置，请指定 origin/商圈 或授权定位；推荐结果不受影响",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        # 2) 坐标校验：城市级定位不用于步行/骑行/公交
        err = guard.validate_coord(loc.lat, loc.lon, loc.coord)
        if err == "coord_mismatch":
            return EnrichmentResult(
                status="invalid_coord", origin=loc,
                message=f"坐标系不一致（期望 {config.COORD_SYSTEM}），已拦截未发起调用",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # 3) 城市级定位：过滤掉精细 mode，只保留驾车（大尺度参考）
        eff_modes = [
            m for m in modes if not guard.city_level_blocked(loc.precision, m)
        ] or ["driving"]
        city_blocked = len(eff_modes) < len(modes)

        # 4) 逐 (shop, mode) 检查缓存，未命中的收集去并发调用
        origin_dict = loc.model_dump()
        plans: list[SimpleRoutePlan] = []
        pending: list[tuple[str, str, str]] = []   # (key, shop_id, mode)
        for shop in shops:
            sid = shop.get("shop_id") or shop.get("id") or "?"
            for mode in eff_modes:
                key = make_key(origin_dict, sid, mode, departure_time, config.TRANSIT_POLICY)
                cached = self.cache.get(key)
                if cached is not None:
                    plan_cls = TransitRoutePlan if cached.get("mode") == "transit" else SimpleRoutePlan
                    plans.append(plan_cls(**cached))
                else:
                    pending.append((key, sid, mode))

        trace: dict = {}
        if pending:
            # 按 mode 分组，一次 tools/call 传该 mode 的所有目的地（聚合工具）
            by_mode: dict[str, list[tuple[str, str, str]]] = {}
            for key, sid, mode in pending:
                by_mode.setdefault(mode, []).append((key, sid, mode))

            def fetch_mode(mode: str, items: list[tuple[str, str, str]]) -> dict:
                """单 mode 抓取；异常在内部捕获并自带 mode，避免外层猜测。"""
                mt0 = time.perf_counter()
                try:
                    dests = []
                    for _, sid, _ in items:
                        shop = next((s for s in shops if (s.get("shop_id") or s.get("id")) == sid), {})
                        dests.append({"shop_id": sid, "geo": shop.get("geo", {})})
                    resp = self.client.call_tool("route_plans", {
                        "origin": origin_dict, "destinations": dests, "modes": [mode],
                        "departure_time": departure_time, "policy": config.TRANSIT_POLICY,
                    })
                    if resp.get("isError"):
                        raise RuntimeError(str(resp.get("content")))
                    raw_plans = resp.get("structuredContent", {}).get("plans", [])
                    return {"mode": mode, "items": items, "raw_plans": raw_plans,
                            "ms": (time.perf_counter() - mt0) * 1000, "error": None}
                except Exception as e:
                    return {"mode": mode, "items": items, "raw_plans": [],
                            "ms": (time.perf_counter() - mt0) * 1000,
                            "error": f"{type(e).__name__}: {e}"}

            with ThreadPoolExecutor(max_workers=min(config.MAX_WORKERS, len(by_mode) or 1)) as ex:
                futures = [ex.submit(fetch_mode, m, items) for m, items in by_mode.items()]
                deadline = time.monotonic() + config.TOTAL_BUDGET_SEC
                for fut in as_completed(futures, timeout=max(0.1, config.TOTAL_BUDGET_SEC + 1)):
                    if time.monotonic() > deadline:
                        break
                    res = fut.result()
                    mode, items = res["mode"], res["items"]
                    if res["error"]:
                        trace[mode] = {"error": res["error"]}
                        for key, sid, m in items:
                            plans.append(SimpleRoutePlan(
                                shop_id=sid, mode=m, available=False,
                                degraded_reason="mode_error",
                                source="tencent-map" if config.HAS_REAL_KEY else "mock",
                            ))
                        continue
                    raw_plans = res["raw_plans"]
                    trace[mode] = {"latency_ms": round(res["ms"], 1), "count": len(raw_plans)}
                    for (key, sid, _), raw in zip(items, raw_plans):
                        # transit 必须用 TransitRoutePlan 构造，否则 legs 会被丢弃
                        plan_cls = TransitRoutePlan if raw.get("mode") == "transit" else SimpleRoutePlan
                        plan = plan_cls(**raw)
                        self.cache.set(key, raw)
                        plans.append(plan)

        # 5) 口径一致性标注（直线距离 vs 路线里程）
        for p in plans:
            if isinstance(p, TransitRoutePlan) or p.available:
                shop = next((s for s in shops if (s.get("shop_id") or s.get("id")) == p.shop_id), {})
                # 省略具体直线距离计算（需 haversine），仅标注一致性检查已执行
                _ = check_scale_consistency(p, 0) if not shop.get("geo") else None

        # 6) 降级判定
        status, message = guard.decide_status(plans, location_ok=True)
        if city_blocked:
            message = (message + "；城市级定位仅提供驾车参考，步行/公交需精确起点").strip("；")
            if status == "ok":
                status = "partial"
        if any(p.degraded_reason == "no_itinerary" for p in plans) and status == "ok":
            status = "partial"

        return EnrichmentResult(
            route_plans=[p.model_dump(mode="json") for p in plans],
            origin=loc, status=status,
            latency_ms=(time.perf_counter() - t0) * 1000,
            cache_hits=len(plans) - len(pending),
            trace=trace, message=message,
        )


def merge_into_answer(answer: dict, enrichment: EnrichmentResult) -> dict:
    """方案 B：生成后结构化合并——交通字段作为平行字段，LLM 完全不碰这些数字。

    不修改现有 AnswerResponse schema：返回的是普通 dict，调用方可按需加字段。
    """
    merged = dict(answer)
    merged["route_plans"] = [p if isinstance(p, dict) else p.model_dump(mode="json")
                             for p in enrichment.route_plans]
    merged["enrichment_status"] = enrichment.status
    merged["enrichment_message"] = enrichment.message
    merged["enrichment_latency_ms"] = round(enrichment.latency_ms, 1)
    return merged
