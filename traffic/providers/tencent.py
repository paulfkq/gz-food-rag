"""腾讯位置服务 Provider（轨道 B 的底层 WebService 适配）。

真实调用仅在配置了 TENCENT_MAP_KEY 时启用。返回结构与 mock 完全一致，
上层 normalize.py 不需要区分数据来源。key 只从构造参数注入（源自环境变量），
绝不写进代码 / 日志；日志里只打脱敏后的 key（docs/02 §2）。

接口参考（以官方文档实时形态为准，不把 URL 写死成事实）：
  directionDriving/Bicycling/Walking: GET /ws/direction/v1/{mode}
  directionTransit:                   GET /ws/direction/v1/transit
  matrix:                             GET /ws/distance/v1/matrix?mode=driving|walking
  ipLocation:                         GET /ws/location/v1/ip
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlencode

import httpx

from .. import config
from ..secrets import redact_key
from .base import MapProvider

BASE = "https://apis.map.qq.com"

MODE_PATH = {
    "driving": "/ws/direction/v1/driving",
    "cycling": "/ws/direction/v1/bicycling",
    "walking": "/ws/direction/v1/walking",
    "transit": "/ws/direction/v1/transit",
}


class TencentProvider(MapProvider):
    name = "tencent"

    def __init__(self, api_key: str, timeout: float | None = None):
        if not api_key:
            raise ValueError("TencentProvider 需要 api_key（从 TENCENT_MAP_KEY 环境变量注入）")
        self.api_key = api_key
        self.timeout = timeout or config.MODE_TIMEOUT_SEC
        self._last_latency_ms = 0.0
        self._call_count = 0
        self._qps_retries_hit = 0          # 可观测：触发过多少次 120 退避
        self._throttle_lock = threading.Lock()
        self._next_call_at = 0.0           # 全局令牌：保证两次请求间隔 ≥ CALL_MIN_INTERVAL_SEC
        # key 只在初始化时打一次脱敏日志，之后不再出现在任何日志
        print(f"[tencent-provider] 使用真实 key {redact_key(api_key)}（不落日志）", flush=True)

    def _throttle(self) -> None:
        """全局最小调用间隔：把瞬时并发摊平，避免撞腾讯免费 Key 的低 QPS 上限。"""
        if config.CALL_MIN_INTERVAL_SEC <= 0:
            return
        with self._throttle_lock:
            now = time.monotonic()
            wait = self._next_call_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_call_at = max(now, self._next_call_at) + config.CALL_MIN_INTERVAL_SEC

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self.api_key}
        url = BASE + path + "?" + urlencode(params)
        t0 = time.perf_counter()
        attempts = 0
        try:
            # status=120 = 「此key每秒请求量已达到上限」（QPS，非日配额）；
            # 有限退避重试，避免整档方案因瞬时并发被误降级。
            while True:
                self._throttle()
                resp = httpx.get(url, timeout=self.timeout, trust_env=False)
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == 120 and attempts < config.QPS_RETRIES:
                    attempts += 1
                    self._qps_retries_hit += 1
                    time.sleep(config.QPS_RETRY_BACKOFF_SEC * attempts)
                    continue
                return data
        finally:
            self._last_latency_ms = (time.perf_counter() - t0) * 1000
            self._call_count += 1

    @staticmethod
    def _loc_str(p: dict) -> str:
        return f"{p['lat']},{p['lon']}"

    def route_plans(self, origin, destinations, modes, departure_time=None, policy="LEAST_TIME") -> dict:
        out: dict[str, list[dict]] = {m: [] for m in modes}
        origin_str = self._loc_str(origin)
        for mode in modes:
            # matrix 只覆盖 driving/walking；其余逐目的地。这里统一逐目的地，
            # 保持简单与结构一致；matrix 作为可选优化在 route_matrix 工具里提供。
            path = MODE_PATH[mode]
            for shop in destinations:
                params = {"from": origin_str, "to": self._loc_str(shop["geo"])}
                if mode == "transit":
                    params.update({
                        "policy": policy,
                        "departure_time": departure_time or int(time.time()),
                        "price_unit": config.PRICE_UNIT,
                    })
                try:
                    out[mode].append(self._get(path, params))
                except Exception as e:  # 单目的地失败不影响其它，交由上层标 available=False
                    out[mode].append({"status": -1, "error": type(e).__name__})
        return out

    def resolve_location(self, address=None, ip=None) -> dict | None:
        if address:
            data = self._get("/ws/geocoder/v1/", {"address": address})
        elif ip:
            data = self._get("/ws/location/v1/ip", {"ip": ip})
        else:
            return None
        try:
            loc = data["result"]["location"]
            city_level = bool(data["result"].get("ad_info") and ip)
            return {
                "lat": loc["lat"], "lon": loc["lng"], "coord": "GCJ-02",
                "precision": "city" if city_level else "precise",
                "source": "ip" if ip else "geocoder",
            }
        except (KeyError, TypeError):
            return None

    def health(self) -> dict:
        return {
            "provider": "tencent", "real_data": True,
            "latency_ms_last": round(self._last_latency_ms, 1),
            "calls": self._call_count,
            "key": redact_key(self.api_key),
        }
