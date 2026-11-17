"""归一化与最短路选择的单测（docs/02 §11：单位/最短路/粒度 100% 正确）。"""
import unittest

from traffic.normalize import (
    fen_to_yuan, min_to_sec, normalize_plan, normalize_simple,
    normalize_transit, select_best_route,
)
from traffic.schema import SimpleRoutePlan, TransitRoutePlan


def _route(duration_min, n_transit=1, transfer_walk_min=0, stopped=False, price=500):
    """手工构造一条 route（分钟/分单位），steps 为 [walk, transit, walk, transit, walk]。"""
    steps = [{"type": "walking", "duration": 5, "distance": 300}]
    for i in range(n_transit):
        steps.append({
            "type": "SUBWAY" if i % 2 == 0 else "BUS", "duration": 10, "distance": 1500,
            "lines": [{
                "vehicle": "SUBWAY" if i % 2 == 0 else "BUS",
                "title": f"线路{i}", "geton": {"title": f"上{i}"},
                "getoff": {"title": f"下{i}"}, "station_count": 3,
                "price": price, "first_time": "06:00", "last_time": "23:00",
                "running_status": 303 if stopped else 300,
            }],
        })
        steps.append({"type": "walking", "duration": transfer_walk_min, "distance": 200,
                      "instruction": f"换乘步行{i}"})
    return {"duration": duration_min, "distance": 5000, "price": price, "steps": steps}


class TestUnits(unittest.TestCase):
    def test_min_to_sec(self):
        self.assertEqual(min_to_sec(38), 2280)
        self.assertEqual(min_to_sec(6.5), 390)
        self.assertIsNone(min_to_sec(-1))
        self.assertIsNone(min_to_sec(None))

    def test_fen_to_yuan(self):
        self.assertEqual(fen_to_yuan(500), 5.0)     # 500 分 = 5 元
        self.assertEqual(fen_to_yuan(-1), None)     # -1 表示缺票价
        self.assertEqual(fen_to_yuan(750), 7.5)


class TestSimpleGranularity(unittest.TestCase):
    def test_no_legs_in_simple(self):
        """非 transit 档：只有 duration_sec，schema 里根本没有 legs 字段。"""
        raw = {"status": 0, "result": {"duration": 16, "distance": 3000}}
        plan = normalize_simple("GZ-1", "driving", raw)
        self.assertTrue(plan.available)
        self.assertEqual(plan.duration_sec, 960)
        self.assertNotIn("legs", plan.model_dump())
        self.assertNotIn("polyline", plan.model_dump())

    def test_provider_error_degrades(self):
        plan = normalize_simple("GZ-1", "walking", {"status": -1})
        self.assertFalse(plan.available)
        self.assertIn("provider_error", plan.degraded_reason)


class TestBestRoute(unittest.TestCase):
    def test_pick_min_duration(self):
        routes = [_route(45), _route(32), _route(38)]
        best, count, rank = select_best_route(routes)
        self.assertEqual(count, 3)
        self.assertEqual(best["duration"], 32)
        self.assertEqual(rank, 2)                # 32 分钟在原 routes[1]

    def test_tie_break_by_transfer_then_walk(self):
        # duration 相同：换乘少者优先；再同则步行少者优先
        r_many_transfer = _route(30, n_transit=2, transfer_walk_min=3)
        r_few_transfer = _route(30, n_transit=1, transfer_walk_min=9)
        best, _, _ = select_best_route([r_many_transfer, r_few_transfer])
        self.assertIs(best, r_few_transfer)

    def test_stopped_303_excluded(self):
        # 最快的那条停运（303），应剔除，取下一名
        stopped = _route(20, stopped=True)
        ok = _route(35)
        best, _, rank = select_best_route([stopped, ok])
        self.assertEqual(best["duration"], 35)
        self.assertEqual(rank, 2)

    def test_deterministic(self):
        routes = [_route(40), _route(28), _route(33)]
        a = select_best_route(routes)
        b = select_best_route(routes)
        self.assertEqual(a, b)


class TestTransitDetail(unittest.TestCase):
    def test_transfer_walk_computed(self):
        """1 次换乘：步行段夹在两个 transit 之间 → is_transfer_walk=True。"""
        raw = {"status": 0, "result": {"routes": [
            _route(30, n_transit=2, transfer_walk_min=4, price=600),
        ]}}
        plan = normalize_transit("GZ-1", raw)
        self.assertIsInstance(plan, TransitRoutePlan)
        self.assertTrue(plan.available)
        self.assertEqual(plan.transfer_count, 1)         # 2 transit 段 → 1 次换乘
        transfer_walks = [l for l in plan.legs if l.is_transfer_walk]
        self.assertEqual(len(transfer_walks), 1)
        self.assertEqual(transfer_walks[0].duration_sec, 240)  # 4 分钟 = 240 秒
        self.assertEqual(plan.transfer_walk_sec, 240)
        # _route 构造：起步行5 + 每段transit后walk(transfer_walk_min=4)×2 = 5+4+4=13min
        self.assertEqual(plan.walk_total_sec, (5 + 4 + 4) * 60)

    def test_lines_first_and_alternatives(self):
        vehicle, line = "SUBWAY", "地铁3号线"
        steps = [
            {"type": "walking", "duration": 5, "distance": 300},
            {"type": "SUBWAY", "duration": 10, "distance": 1500, "lines": [
                {"vehicle": vehicle, "title": line, "geton": {"title": "体育西"},
                 "getoff": {"title": "珠江新城"}, "station_count": 1,
                 "price": 300, "first_time": "06:00", "last_time": "23:00", "running_status": 300},
                {"title": "地铁3号线B线"},
            ]},
            {"type": "walking", "duration": 5, "distance": 300},
        ]
        route = {"duration": 30, "distance": 5000, "price": 300, "steps": steps}
        plan = normalize_transit("GZ-1", {"status": 0, "result": {"routes": [route]}})
        transit_leg = [l for l in plan.legs if l.kind == "transit"][0]
        self.assertEqual(transit_leg.line_name, line)     # 取 lines[0]
        self.assertEqual(transit_leg.fare_yuan, 3.0)      # 300 分 = 3 元
        self.assertEqual(transit_leg.alternative_lines, ["地铁3号线B线"])

    def test_no_polyline_leak(self):
        route = _route(30)
        route["polyline"] = "SECRET_COORDS"              # 若泄漏到输出就失败
        plan = normalize_transit("GZ-1", {"status": 0, "result": {"routes": [route]}})
        self.assertNotIn("polyline", plan.model_dump())
        self.assertNotIn("SECRET_COORDS", str(plan.model_dump()))

    def test_no_itinerary_degraded(self):
        # 只有总耗时、steps 缺失 → degraded_reason=no_itinerary，但仍 available
        route = {"duration": 30, "distance": 5000, "price": 500, "steps": []}
        plan = normalize_transit("GZ-1", {"status": 0, "result": {"routes": [route]}})
        self.assertTrue(plan.available)
        self.assertEqual(plan.degraded_reason, "no_itinerary")


class TestDispatch(unittest.TestCase):
    def test_dispatch_granularity(self):
        raw = {"status": 0, "result": {"duration": 20, "distance": 3000}}
        p = normalize_plan("GZ-1", "walking", raw)
        self.assertIsInstance(p, SimpleRoutePlan)
        self.assertNotIsInstance(p, TransitRoutePlan)
        raw_t = {"status": 0, "result": {"routes": [_route(30)]}}
        pt = normalize_plan("GZ-1", "transit", raw_t)
        self.assertIsInstance(pt, TransitRoutePlan)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            normalize_plan("GZ-1", "teleport", {})


class TestTencentRealShape(unittest.TestCase):
    """回归：腾讯 WebService 真实响应结构（与 mock 不同），2026-11-17 线上 bug。

    真实结构要点（已用真 key 实测 + 官方文档核对）：
      - driving/cycling/walking: result.routes[0].duration/distance，顶层无 duration；
      - transit steps: step.mode ∈ {WALKING, TRANSIT}（不是 type）；
        TRANSIT 段 step.duration=None，耗时在 lines[0].duration；
        起末班叫 start_time/end_time；上车站出口在 geton.exit.title；
        running_status 是 int 300（正常）/303（停运）。
    """

    def test_real_simple_routes_nested(self):
        raw = {"status": 0, "result": {"routes": [
            {"mode": "DRIVING", "duration": 100, "distance": 103733},
        ]}}
        plan = normalize_simple("GZ-R", "driving", raw, source="tencent-map")
        self.assertTrue(plan.available)
        self.assertEqual(plan.duration_sec, 6000)        # 100 分钟 × 60
        self.assertEqual(plan.distance_m, 103733)
        self.assertIsNone(plan.degraded_reason)

    def test_real_simple_routes_nested_all_modes(self):
        for mode, minutes in (("driving", 12), ("cycling", 30), ("walking", 45)):
            raw = {"status": 0, "result": {"routes": [
                {"mode": mode.upper(), "duration": minutes, "distance": 2000},
            ]}}
            plan = normalize_simple("GZ-R", mode, raw, source="tencent-map")
            self.assertTrue(plan.available, f"{mode} 应可用")
            self.assertEqual(plan.duration_sec, minutes * 60)

    def test_real_transit_mode_and_line_duration(self):
        steps = [
            {"mode": "WALKING", "duration": 12, "distance": 777,
             "instruction": "步行至陈家祠"},
            {"mode": "TRANSIT", "lines": [{
                "vehicle": "SUBWAY", "title": "地铁1号线", "station_count": 1,
                "price": 200, "duration": 4, "start_time": "06:00", "end_time": "23:00",
                "running_status": 300,
                "geton": {"title": "陈家祠", "exit": {"title": "F口"}},
                "getoff": {"title": "西门口"},
                "destination": {"title": "广州东站"},
            }]},
            {"mode": "WALKING", "duration": 2, "distance": 149},
        ]
        route = {"duration": 20, "distance": 1200, "price": 200, "steps": steps}
        raw = {"status": 0, "result": {"routes": [route]}}
        plan = normalize_transit("GZ-R", raw, source="tencent-map")
        self.assertTrue(plan.available)
        self.assertIsNone(plan.degraded_reason)          # 修复前会误判 no_itinerary
        self.assertEqual(plan.transfer_count, 0)         # 1 段 transit → 0 换乘
        transit = [l for l in plan.legs if l.kind == "transit"]
        self.assertEqual(len(transit), 1)                # 修复前会被判成 walk → 0 段
        self.assertEqual(transit[0].duration_sec, 240)   # lines[0].duration=4 分钟
        self.assertEqual(transit[0].line_name, "地铁1号线")
        self.assertEqual(transit[0].first_time, "06:00") # start_time 兼容
        self.assertEqual(transit[0].last_time, "23:00")
        self.assertEqual(transit[0].exit_name, "F口")
        self.assertEqual(transit[0].fare_yuan, 2.0)

    def test_real_transit_multi_transfer_count(self):
        # 3 段 TRANSIT → 2 次换乘；中间两段 walk 标换乘步行
        steps = [{"mode": "WALKING", "duration": 5, "distance": 100}]
        for i in range(3):
            steps.append({"mode": "TRANSIT", "lines": [
                {"vehicle": "BUS", "title": f"公交{i}", "price": 200,
                 "start_time": "06:00", "end_time": "22:00", "running_status": 300,
                 "geton": {"title": f"上{i}"}, "getoff": {"title": f"下{i}"},
                 "destination": {"title": "终点"}},
            ]})
            steps.append({"mode": "WALKING", "duration": 3, "distance": 100})
        route = {"duration": 40, "distance": 5000, "price": 600, "steps": steps}
        plan = normalize_transit("GZ-R", {"status": 0, "result": {"routes": [route]}})
        self.assertEqual(plan.transfer_count, 2)
        self.assertEqual(plan.candidates_count, 1)
        self.assertIsNone(plan.degraded_reason)

    def test_real_transit_303_stopped_excluded(self):
        stopped_steps = [{"mode": "TRANSIT", "lines": [
            {"vehicle": "BUS", "title": "停运线", "price": 200,
             "running_status": 303, "geton": {"title": "A"}, "getoff": {"title": "B"}},
        ]}]
        ok_steps = [{"mode": "TRANSIT", "lines": [
            {"vehicle": "BUS", "title": "正常线", "price": 200,
             "running_status": 300, "geton": {"title": "A"}, "getoff": {"title": "B"}},
        ]}]
        stopped = {"duration": 15, "steps": stopped_steps}
        ok = {"duration": 35, "steps": ok_steps}
        best, _, rank = select_best_route([stopped, ok])
        self.assertEqual(best["duration"], 35)     # 停运的快线被剔除
        self.assertEqual(rank, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
