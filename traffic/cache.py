"""富化缓存（docs/02 §9）：网格化起点 + 时间桶 + LRU + TTL。

缓存 key = (origin 网格化到 ~100m, shop_id, mode, departure_time 桶 5min, policy)
为什么这样设计：
  - 同一商圈反复查询命中率很高（用户问多家店）；
  - departure_time 必须进 key，否则"23:50 查到的停运线路"会被缓存到次日中午；
  - 位置精确到经纬度会几乎不命中，网格化到 100m 平衡精度与命中率。
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from . import config


def _grid(lat: float, lon: float) -> tuple[int, int]:
    """经纬度网格化：约 100m 一格（纬度 1°≈111km，经度按纬度折算）。"""
    lat_step = config.ORIGIN_GRID_M / 111000.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    lon_step = config.ORIGIN_GRID_M / (111000.0 * cos_lat)
    return int(math.floor(lat / lat_step)), int(math.floor(lon / lon_step))


def make_key(
    origin: dict, shop_id: str, mode: str,
    departure_time: int | None = None, policy: str = "LEAST_TIME",
) -> str:
    lat, lon = _grid(float(origin["lat"]), float(origin["lon"]))
    bucket = (departure_time or 0) // config.DEPARTURE_BUCKET_SEC
    return f"{lat}:{lon}|{shop_id}|{mode}|{bucket}|{policy}"


@dataclass
class _Entry:
    value: object
    expires_at: float


class TTLCache:
    """线程安全的 LRU + TTL 缓存（内存级；生产可换 Redis，接口不变）。"""

    def __init__(self, ttl: int | None = None, maxsize: int = 2000):
        self.ttl = ttl if ttl is not None else config.CACHE_TTL_SEC
        self.maxsize = maxsize
        self._data: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at < time.time():
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return entry.value

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = _Entry(value, time.time() + self.ttl)
            self._data.move_to_end(key)
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "size": len(self._data),
        }
