"""归一化与方案选择（docs/02 §8.2 / §8.3）——全模块技术核心。

把厂商原始响应（分钟/分、routes[]、steps[].lines[]）转换成项目契约：
  1. 单位归一化：分钟→秒(×60)、分→元(÷100)、distance 米透传；
  2. 最短路选择：对 routes[] 二次按 duration 升序，平局按 transfer_count→walk_total_m 打破；
  3. legs 解析：steps[] 交替结构，取 lines[0]，识别换乘步行，303 停运剔候选；
  4. 粒度分级：transit 带 legs，driving/cycling/walking 只给 duration_sec；
  5. 口径一致性：路线里程 vs 直线距离异常倍数标注。

铁律：duration_sec 是"总预估用时（含候车/换乘等待）"，sum(legs.duration_sec) 只是在途时间，
两者不相等，绝不用分段和替代总时长（docs/02 §8.2 口径提醒）。
"""
from __future__ import annotations

from .schema import SimpleRoutePlan, TransitLeg, TransitRoutePlan


# ---------------- 单位归一化 ----------------

def min_to_sec(minutes: float | int | None) -> int | None:
    """官方 duration 单位=分钟 → 秒。None/负数视为缺失。"""
    if minutes is None or minutes < 0:
        return None
    return int(round(float(minutes) * 60))


def fen_to_yuan(price_cent: float | int | None) -> float | None:
    """官方 price 单位=分（price_unit=1 已统一） → 元；-1 表示缺票价。"""
    if price_cent is None or price_cent < 0:
        return None
    return round(float(price_cent) / 100, 2)


# ---------------- 结构兼容层（腾讯真实 vs mock）----------------
# 关键修复（2026-11-17）：mock 与腾讯真实 WebService 的 steps[] 字段不同：
#   mock:                step["type"] ∈ {walking,bus,subway,rail}，step["duration"] 有值
#   腾讯 direction/transit: step["mode"] ∈ {"WALKING","TRANSIT"}，
#                          TRANSIT 段的 duration 在 step["lines"][0]["duration"]，
#                          step["duration"] 为 None；起末班车叫 start_time/end_time。
# 之前只认 mock 结构，导致真实 key 下所有 transit 段被判成 walk → 0 换乘、
# 无线路明细、全量 no_itinerary 降级；driving/cycling/walking 只读顶层 duration
# （真实在 result.routes[0]）→ 全部 no_duration 不可用。这里统一兼容两种结构。

_TRANSIT_KINDS = {"bus", "subway", "rail", "transit"}


def _step_kind(step: dict) -> str:
    """返回 'transit' / 'walk'，兼容腾讯真实(mode) 与 mock(type) 两种结构。"""
    mode = str(step.get("mode", "")).lower()
    if mode in _TRANSIT_KINDS:
        return "transit"
    if mode == "walking":
        return "walk"
    typ = str(step.get("type", "")).lower()      # mock / 旧结构
    if typ in _TRANSIT_KINDS:
        return "transit"
    return "walk"


def _step_duration_min(step: dict, lines: list[dict]) -> float | int | None:
    """段耗时（分钟）：mock 在 step.duration；腾讯 TRANSIT 段在 lines[0].duration。"""
    v = step.get("duration")
    if v is None and lines:
        v = lines[0].get("duration")
    return v


def _line_time(line: dict) -> tuple[str | None, str | None]:
    """首末班车：腾讯 start_time/end_time；mock first_time/last_time。"""
    return (line.get("first_time") or line.get("start_time"),
            line.get("last_time") or line.get("end_time"))


# ---------------- 非公交档：SimpleRoutePlan ----------------

def normalize_simple(shop_id: str, mode: str, raw: dict, source: str = "mock") -> SimpleRoutePlan:
    """驾车/骑行/步行：只产出参考耗时，不产出任何路径明细（粒度分级）。"""
    result = (raw or {}).get("result") or {}
    if (raw or {}).get("status", 0) != 0 or not result:
        return SimpleRoutePlan(
            shop_id=shop_id, mode=mode, available=False,
            degraded_reason=f"provider_error:{(raw or {}).get('status', 'unknown')}", source=source,
        )
    # 真实腾讯把耗时/里程放在 result.routes[0]；mock 放在 result 顶层。两者都兼容。
    routes = result.get("routes") or []
    route = routes[0] if routes else {}
    duration_raw = result.get("duration")
    if duration_raw is None:
        duration_raw = route.get("duration")
    distance_raw = result.get("distance")
    if distance_raw is None:
        distance_raw = route.get("distance")
    duration = min_to_sec(duration_raw)
    if duration is None:
        return SimpleRoutePlan(
            shop_id=shop_id, mode=mode, available=False,
            degraded_reason="no_duration", source=source,
        )
    # 注意：这里刻意不设置任何 legs / polyline 字段——schema 里也没有，防止泄漏
    return SimpleRoutePlan(
        shop_id=shop_id, mode=mode, available=True,
        duration_sec=duration, distance_m=distance_raw, source=source,
    )


# ---------------- 公交档：TransitRoutePlan ----------------

def _parse_legs(steps: list[dict]) -> tuple[list[TransitLeg], int, int, int]:
    """解析 steps[] → legs[]，并汇总换乘步行。

    返回 (legs, walk_total_sec, transfer_walk_sec, walk_total_m)。
    换乘步行判定：夹在两个 transit 段之间的 walk 段。
    """
    legs: list[TransitLeg] = []
    transit_positions: list[int] = []
    seq = 1
    for step in steps or []:
        kind = _step_kind(step)                      # 兼容 mode(真实) / type(mock)
        is_transit = kind == "transit"
        lines = step.get("lines") or []
        if is_transit:
            transit_positions.append(len(legs))
        legs.append(
            TransitLeg(
                seq=seq,
                kind=kind,
                duration_sec=min_to_sec(_step_duration_min(step, lines)) or 0,
                distance_m=int(step.get("distance") or 0),
                instruction=None if is_transit else step.get("instruction"),
            )
        )
        seq += 1

    # 回填 transit 段字段，并给 walk 段标 is_transfer_walk
    i = 0
    for leg, step in zip(legs, steps or []):
        if leg.kind == "transit":
            lines = step.get("lines") or []
            if lines:
                first = lines[0]                       # 固定取 lines[0]：信息最完整
                leg.vehicle = str(first.get("vehicle", leg.vehicle or "BUS")).upper()
                title = first.get("title") or (first.get("geton") or {}).get("title") or ""
                leg.line_name = title or None
                leg.direction = (first.get("destination") or {}).get("title") or None
                leg.geton_station = (first.get("geton") or {}).get("title")
                leg.getoff_station = (first.get("getoff") or {}).get("title")
                leg.station_count = first.get("station_count")
                leg.fare_yuan = fen_to_yuan(first.get("price"))
                leg.first_time, leg.last_time = _line_time(first)   # start/end_time 兼容
                leg.running_status = first.get("running_status")
                # 真实腾讯上车站把出口放在 geton.exit.title；mock 无此字段
                exit_info = (first.get("geton") or {}).get("exit") or {}
                leg.exit_name = exit_info.get("title") if isinstance(exit_info, dict) else None
                leg.alternative_lines = [str(l.get("title")) for l in lines[1:] if l.get("title")]
        i += 1

    # 换乘步行：索引位于首个与末个 transit 之间的 walk 段
    if len(transit_positions) >= 2:
        first_t, last_t = transit_positions[0], transit_positions[-1]
        for idx in range(first_t + 1, last_t):
            if legs[idx].kind == "walk":
                legs[idx].is_transfer_walk = True

    walk_total_sec = sum(l.duration_sec for l in legs if l.kind == "walk")
    transfer_walk_sec = sum(l.duration_sec for l in legs if l.is_transfer_walk)
    walk_total_m = sum(l.distance_m for l in legs if l.kind == "walk")
    return legs, walk_total_sec, transfer_walk_sec, walk_total_m


def _route_metrics(route: dict) -> dict:
    """从一条 route 提取排序/平局指标（duration 分钟；换乘数=transit 段数-1）。"""
    steps = route.get("steps") or []
    transit_count = sum(1 for s in steps if _step_kind(s) == "transit")
    walk_m = sum(int(s.get("distance") or 0) for s in steps
                 if _step_kind(s) == "walk")
    # 停运候选直接判不可用：任一 transit 段 running_status=303
    stopped = any(
        str((l or {}).get("running_status")) == "303"
        for s in steps for l in (s.get("lines") or [])
    )
    return {
        "duration_min": float(route.get("duration") or 0),
        "transfer_count": max(0, transit_count - 1),
        "walk_total_m": walk_m,
        "stopped": stopped,
    }


def select_best_route(routes: list[dict]) -> tuple[dict, int, int]:
    """最短路选择（docs/02 §8.3）：二次排序 + 平局打破 + 303 剔除。

    返回 (选中 route, 候选总数, 选中名次 1-based)。
    排序键：(duration_min, transfer_count, walk_total_m) 升序——保证同一输入恒定输出。
    """
    if not routes:
        return {}, 0, 0
    scored = []
    for idx, r in enumerate(routes):
        m = _route_metrics(r)
        if m["stopped"]:
            continue                                  # 303 停运 → 剔除，取下一名候选
        scored.append((m["duration_min"], m["transfer_count"], m["walk_total_m"], idx, r))
    if not scored:                                    # 全部停运：退而求其次，忽略 303 标记
        for idx, r in enumerate(routes):
            m = _route_metrics(r)
            scored.append((m["duration_min"], m["transfer_count"], m["walk_total_m"], idx, r))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    best = scored[0]
    rank = routes.index(best[4]) + 1                  # 在原 routes[] 中的名次（1-based）
    return best[4], len(routes), rank


def normalize_transit(
    shop_id: str, raw: dict, source: str = "mock",
    policy: str = "LEAST_TIME", alternatives: int = 0,
) -> TransitRoutePlan:
    """公交档：选最短路 → 解析 legs → 产出完整换乘明细。"""
    result = (raw or {}).get("result") or {}
    routes = result.get("routes") or []
    if (raw or {}).get("status", 0) != 0 or not routes:
        return TransitRoutePlan(
            shop_id=shop_id, available=False,
            degraded_reason="no_transit_route", source=source, selected_policy=policy,
        )

    best, candidates, rank = select_best_route(routes)
    legs, walk_sec, transfer_walk_sec, walk_m = _parse_legs(best.get("steps") or [])
    transit_segments = sum(1 for l in legs if l.kind == "transit")
    fare = None
    for l in legs:
        if l.fare_yuan is not None:
            fare = (fare or 0) + l.fare_yuan
    if best.get("price") is not None:
        route_fare = fen_to_yuan(best.get("price"))
        if route_fare is not None:
            fare = route_fare                        # 路线级总价更权威

    plan = TransitRoutePlan(
        shop_id=shop_id, available=True,
        duration_sec=min_to_sec(best.get("duration")),
        distance_m=best.get("distance"), source=source,
        selected_policy=policy, candidates_count=candidates, selected_rank=rank,
        transfer_count=max(0, transit_segments - 1),
        walk_total_sec=walk_sec, transfer_walk_sec=transfer_walk_sec,
        walk_total_m=walk_m, fare_yuan=fare, legs=legs,
    )
    if not legs or transit_segments == 0:
        plan.degraded_reason = "no_itinerary"         # 只给耗时、无明细 → partial
    return plan


# ---------------- 统一入口 ----------------

def normalize_plan(shop_id: str, mode: str, raw: dict, source: str = "mock", **kw) -> SimpleRoutePlan:
    """按 mode 分派到对应归一化函数（粒度分级在这里强制生效）。"""
    if mode == "transit":
        return normalize_transit(
            shop_id, raw, source,
            policy=kw.get("policy", "LEAST_TIME"),
            alternatives=kw.get("alternatives", 0),
        )
    if mode not in {"driving", "cycling", "walking"}:
        raise ValueError(f"未知 mode: {mode}")
    return normalize_simple(shop_id, mode, raw, source)


def check_scale_consistency(route_plan: SimpleRoutePlan, straight_m: float) -> bool:
    """口径一致性：路线里程不应远超直线距离的合理倍数（docs/02 §7.2）。"""
    from . import config
    if not route_plan.distance_m or not straight_m:
        return True
    return route_plan.distance_m <= straight_m * config.ROUTE_TO_STRAIGHT_MAX_RATIO
