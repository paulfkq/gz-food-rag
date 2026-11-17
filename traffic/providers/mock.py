"""确定性 Mock Provider（无 key 场景）。

关键：模拟的是"厂商原始返回结构"，而不是归一化后的结果——
这样 mock 与真实腾讯 API 走同一条 normalize 路径，单测覆盖的就是真实逻辑。
所有数值由 shop_id 的 hash 决定，保证同一输入恒定输出（docs/02 §7.6 确定性）。
"""
from __future__ import annotations

import hashlib

from .base import MapProvider


def _seed(*parts: str) -> int:
    h = hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _rand(seed: int, lo: int, hi: int) -> int:
    return lo + (seed % (hi - lo + 1))


class MockProvider(MapProvider):
    """生成结构与腾讯 WebService 一致的模拟数据（分钟/分单位）。"""

    name = "mock"

    def __init__(self, real_provider: str = "mock"):
        # real_provider 仅用于标注：若因无 key 降级，明确告诉上层不是真实数据
        self.real_provider = real_provider

    def _simple(self, mode: str, shop_id: str) -> dict:
        """非公交档：模拟 direction* 的原始响应。"""
        s = _seed(shop_id, mode)
        # 步行最慢、驾车最快、居中骑行
        base = {"driving": (12, 45), "cycling": (18, 55), "walking": (25, 90)}[mode]
        minutes = _rand(s, *base)
        return {
            "status": 0,
            "result": {
                "duration": minutes,             # 官方单位：分钟
                "distance": _rand(s, 800, 8000),  # 官方单位：米
            },
        }

    def _transit(self, shop_id: str, departure_time: int | None) -> dict:
        """公交档：模拟 routes[] 多方案 + steps[] 交替结构 + lines[]。"""
        s = _seed(shop_id, "transit")
        n_routes = _rand(s, 2, 4)  # 2~4 条候选方案
        routes = []
        for r in range(n_routes):
            rs = _seed(shop_id, "transit", str(r))
            total_min = _rand(rs, 28, 70)
            n_transfer = _rand(rs, 0, 2)
            steps = []
            seq = 1
            # 起接驳步行
            steps.append(self._walk_step(seq, _rand(rs, 3, 9), _rand(rs, 200, 700), "起点"))
            seq += 1
            for t in range(n_transfer + 1):
                vehicle, line = self._line(rs, t)
                steps.append(self._transit_step(
                    seq, vehicle, line, _rand(rs, 4, 18), _rand(rs, 2, 12),
                    _rand(rs, 200, 500), 303 if (r > 0 and t == 0 and rs % 7 == 0) else 300,
                ))
                seq += 1
                steps.append(self._walk_step(seq, _rand(rs, 2, 8), _rand(rs, 100, 400), "换乘"))
                seq += 1
            # 末段步行
            steps[-1]["instruction"] = "出站步行至目的地"
            route = {
                "duration": total_min,
                "distance": _rand(rs, 3000, 15000),
                "price": _rand(rs, 200, 900),        # 官方单位：分
                "steps": steps,
            }
            routes.append(route)
        return {"status": 0, "result": {"routes": routes}}

    @staticmethod
    def _walk_step(seq: int, minutes: int, meters: int, label: str) -> dict:
        return {
            "type": "walking",
            "duration": minutes,
            "distance": meters,
            "instruction": f"步行 {meters} 米（{label}）",
        }

    @staticmethod
    def _transit_step(
        seq: int, vehicle: str, line: str, minutes: int,
        stations: int, price_cent: int, running_status: int,
    ) -> dict:
        # lines[0] 信息最完整；lines[1:] 只给备选线路名
        main_line = {
            "title": line,
            "vehicle": vehicle,
            "geton": {"title": f"{line}起点站"},
            "getoff": {"title": f"{line}终点站"},
            "station_count": stations,
            "price": price_cent,
            "first_time": "06:00",
            "last_time": "23:00",
            "running_status": running_status,
        }
        alt_lines = [{"title": line + "B线"}, {"title": line + "快线"}]
        return {
            "type": vehicle,
            "duration": minutes,
            "distance": stations * 800,
            "lines": [main_line] + alt_lines,
        }

    @staticmethod
    def _line(seed: int, idx: int) -> tuple[str, str]:
        vehicle = "SUBWAY" if seed % 3 else "BUS"
        line = f"地铁{(seed + idx) % 8 + 1}号线" if vehicle == "SUBWAY" else f"公交{(seed + idx) % 60 + 1}路"
        return vehicle, line

    def route_plans(self, origin, destinations, modes, departure_time=None, policy="LEAST_TIME") -> dict:
        out: dict[str, list[dict]] = {}
        for mode in modes:
            plans = []
            for shop in destinations:
                sid = shop.get("shop_id", "?")
                if mode == "transit":
                    plans.append(self._transit(sid, departure_time))
                else:
                    plans.append(self._simple(mode, sid))
            out[mode] = plans
        return out

    def resolve_location(self, address=None, ip=None) -> dict | None:
        if address:
            s = _seed(address)
            return {
                "lat": 23.0 + (s % 1000) / 100000,
                "lon": 113.0 + (s % 1000) / 100000,
                "coord": "GCJ-02", "precision": "precise",
                "source": "geocoder", "display_name": address,
            }
        if ip:
            return {
                "lat": 23.1291, "lon": 113.2644, "coord": "GCJ-02",
                "precision": "city", "source": "ip", "display_name": "广州（IP 城市级）",
            }
        return None

    def health(self) -> dict:
        return {
            "provider": "mock", "real_data": False, "quota_left": None,
            "note": "无地图 key，使用确定性 mock 数据（结构与腾讯 WebService 一致）",
        }
