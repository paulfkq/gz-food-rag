"""降级与不变性单测（docs/02 §11：降级正确率 100%、RAG 指标不变）。"""
import unittest

from traffic.enricher import RouteEnricher, merge_into_answer
from traffic.guard import decide_status, validate_modes, city_level_blocked
from traffic.mcp_client import McpClient
from traffic.schema import SimpleRoutePlan, TransitRoutePlan


class _BrokenClient(McpClient):
    """模拟地图服务全挂：所有 tools/call 抛异常。"""

    def list_tools(self):
        return []

    def call_tool(self, name, arguments):
        raise ConnectionError("map service down")


class _EmptyClient(McpClient):
    """模拟 provider 返回空方案。"""

    def list_tools(self):
        return []

    def call_tool(self, name, arguments):
        if name == "health":
            return {"structuredContent": {}}
        return {"structuredContent": {"plans": []}}


SHOPS = [
    {"shop_id": "GZ-1", "geo": {"lat": 23.11, "lon": 113.34}},
    {"shop_id": "GZ-2", "geo": {"lat": 23.14, "lon": 113.30}},
]
ORIGIN = {"lat": 23.13, "lon": 113.32}


class TestDegradation(unittest.TestCase):
    def test_all_modes_fail_rag_unaffected(self):
        """铁律：地图全挂，富化 unavailable，但 merge 后 answer/shops 原样保留。"""
        enricher = RouteEnricher(client=_BrokenClient())
        result = enricher.enrich(shops=SHOPS, origin=ORIGIN, modes=["driving", "transit"])
        self.assertIn(result.status, ("unavailable", "partial"))
        answer = {"answer": "推荐 A 店（粤菜）", "shops": SHOPS, "mode": "llm"}
        merged = merge_into_answer(answer, result)
        self.assertEqual(merged["answer"], answer["answer"])
        self.assertEqual(merged["shops"], SHOPS)
        self.assertIn("route_plans", merged)
        self.assertIn("enrichment_status", merged)

    def test_empty_plans_unavailable(self):
        enricher = RouteEnricher(client=_EmptyClient())
        result = enricher.enrich(shops=SHOPS, origin=ORIGIN, modes=["walking"])
        self.assertEqual(result.status, "unavailable")

    def test_no_location_need_location(self):
        enricher = RouteEnricher(client=_EmptyClient())
        result = enricher.enrich(shops=SHOPS, origin=None)  # 无任何位置来源
        self.assertEqual(result.status, "need_location")
        self.assertEqual(result.route_plans, [])

    def test_city_level_blocks_precise_modes(self):
        from traffic.mcp_client import make_client
        enricher = RouteEnricher(client=make_client("inproc"))
        result = enricher.enrich(
            shops=SHOPS, origin={"lat": 23.13, "lon": 113.32, "precision": "city", "source": "ip"},
            modes=["driving", "walking", "transit"],
        )
        # 城市级：只保留 driving，其余被拦截 → 状态 partial，且 message 提示需精确起点
        self.assertEqual(result.status, "partial")
        self.assertIn("城市级", result.message)
        # 只应产出 driving 方案（walking/transit 被拦截）
        modes_out = {p["mode"] for p in result.route_plans}
        self.assertEqual(modes_out, {"driving"})

    def test_decide_status_matrix(self):
        ok = [SimpleRoutePlan(shop_id="1", mode="driving", available=True)]
        self.assertEqual(decide_status(ok, True)[0], "ok")
        degraded = [SimpleRoutePlan(shop_id="1", mode="driving", degraded_reason="no_itinerary")]
        self.assertEqual(decide_status(degraded, True)[0], "partial")
        self.assertEqual(decide_status([], True)[0], "unavailable")
        self.assertEqual(decide_status(ok, False)[0], "need_location")


class TestRagInvariance(unittest.TestCase):
    def test_shops_never_mutated(self):
        """开/关富化，输入的 shops（RAG Top-K）必须完全不变——证明不进检索。"""
        import copy
        shops_snapshot = copy.deepcopy(SHOPS)
        enricher = RouteEnricher(client=_EmptyClient())
        enricher.enrich(shops=SHOPS, origin=ORIGIN, modes=["driving", "transit"])
        self.assertEqual(SHOPS, shops_snapshot)

    def test_transit_legs_preserved_through_enrich(self):
        """transit 经整条链（MCP→enricher→序列化）后 legs 不丢。"""
        from traffic.mcp_client import make_client
        enricher = RouteEnricher(client=make_client("inproc"))
        result = enricher.enrich(shops=[SHOPS[0]], origin=ORIGIN, modes=["transit"])
        t = [p for p in result.route_plans if p["mode"] == "transit"][0]
        self.assertGreater(len(t.get("legs", [])), 0)


class TestGuards(unittest.TestCase):
    def test_validate_modes_whitelist(self):
        self.assertEqual(validate_modes(["driving", "teleport", "walking"]), ["driving", "walking"])
        self.assertEqual(validate_modes(["teleport"]), ["walking"])  # 全非法 → 兜底 walking

    def test_city_level_blocked(self):
        self.assertTrue(city_level_blocked("city", "walking"))
        self.assertTrue(city_level_blocked("city", "transit"))
        self.assertFalse(city_level_blocked("city", "driving"))
        self.assertFalse(city_level_blocked("precise", "walking"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
