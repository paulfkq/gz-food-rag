"""位置转换 / 缓存 / MCP 协议单测。"""
import unittest

from traffic.cache import TTLCache, make_key
from traffic.guard import check_origin_header, validate_coord
from traffic.location import (
    BUSINESS_AREA_CENTERS, LocationProvider, wgs84_to_gcj02,
)
from traffic.mcp_client import make_client
from traffic.secrets import assert_compliant_provider, redact_key


class TestLocation(unittest.TestCase):
    def test_wgs84_to_gcj02_shifts(self):
        # 广州某点：转换后应有数百米量级偏移（约 0.001~0.01 度）
        lat, lon = 23.1291, 113.2644
        g_lat, g_lon = wgs84_to_gcj02(lat, lon)
        self.assertNotAlmostEqual(g_lat, lat, places=4)
        self.assertNotAlmostEqual(g_lon, lon, places=4)
        self.assertLess(abs(g_lat - lat), 0.02)     # 但不会偏太离谱
        self.assertLess(abs(g_lon - lon), 0.02)

    def test_out_of_china_passthrough(self):
        self.assertEqual(wgs84_to_gcj02(40.0, -74.0), (40.0, -74.0))  # 纽约原样返回

    def test_priority_chain(self):
        lp = LocationProvider(session_location={"lat": 23.1, "lon": 113.2, "precision": "precise", "source": "session"})
        # 1) 显式参数优先
        loc = lp.resolve(origin={"lat": 23.13, "lon": 113.32})
        self.assertEqual(loc.source, "explicit")
        # 2) 无显式 → 会话内存
        loc2 = lp.resolve()
        self.assertEqual(loc2.source, "session")
        # 3) 客户端 GPS
        lp3 = LocationProvider()
        loc3 = lp3.resolve(client_gps={"lat": 23.1291, "lon": 113.2644})
        self.assertEqual(loc3.source, "geolocation")
        self.assertEqual(loc3.coord, "GCJ-02")
        # 4) 商圈兜底（用干净实例，避免上一步写入的 session_location 抢先）
        loc4 = LocationProvider().resolve(business_area="体育西路")
        self.assertEqual(loc4.source, "business_area")
        self.assertEqual(loc4.lat, BUSINESS_AREA_CENTERS["体育西路"][0])

    def test_no_location_returns_none(self):
        self.assertIsNone(LocationProvider().resolve())


class TestCache(unittest.TestCase):
    def test_key_components(self):
        o = {"lat": 23.13, "lon": 113.32}
        k1 = make_key(o, "GZ-1", "driving", 0)
        k2 = make_key(o, "GZ-1", "driving", 0)
        k3 = make_key(o, "GZ-1", "transit", 0)
        k4 = make_key(o, "GZ-1", "driving", 600)   # 不同 5min 桶
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertNotEqual(k1, k4)

    def test_ttl_and_lru(self):
        c = TTLCache(ttl=1, maxsize=2)
        c.set("a", 1); c.set("b", 2)
        self.assertEqual(c.get("a"), 1)
        c.set("c", 3)                              # 触发 LRU 淘汰
        self.assertIsNone(c.get("b"))
        self.assertGreaterEqual(c.stats()["hits"], 1)


class TestMcpProtocol(unittest.TestCase):
    def setUp(self):
        self.client = make_client("inproc")

    def test_tools_list_has_5(self):
        tools = self.client.list_tools()
        self.assertEqual(len(tools), 5)
        self.assertIn("route_plans", [t["name"] for t in tools])

    def test_tools_call(self):
        r = self.client.call_tool("health", {})
        self.assertIn("structuredContent", r)
        self.assertFalse(r.get("isError", False))

    def test_unknown_tool(self):
        r = self.client.call_tool("no_such_tool", {})
        self.assertTrue(r.get("isError"))

    def test_origin_check(self):
        self.assertTrue(check_origin_header(None))                       # 内部调用可信
        self.assertTrue(check_origin_header("http://127.0.0.1:8100"))
        self.assertFalse(check_origin_header("http://evil.com"))         # 防 DNS rebinding


class TestCompliance(unittest.TestCase):
    def test_whitelist(self):
        for p in ("tencent", "amap", "baidu", "tianditu"):
            assert_compliant_provider(p)                                 # 不抛异常即通过
        with self.assertRaises(ValueError):
            assert_compliant_provider("mapbox")                          # 境外服务禁用

    def test_coord_validation(self):
        self.assertIsNone(validate_coord(23.1, 113.3, "GCJ-02"))
        self.assertEqual(validate_coord(23.1, 113.3, "WGS-84"), "coord_mismatch")
        self.assertEqual(validate_coord(999, 113.3, "GCJ-02"), "coord_out_of_range")

    def test_key_redaction(self):
        self.assertNotIn("secret", redact_key("abcdefghsecret"))
        self.assertEqual(redact_key(None), "<none>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
