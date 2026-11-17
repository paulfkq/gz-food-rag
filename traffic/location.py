"""位置解析（docs/02 §6）：优先级链 + 坐标系统一。

面试讲点：'默认获取当前位置'落到后端其实是「位置来源分级 + 坐标系统一 + 隐私授权」
三件事。纯后端拿不到精确 GPS；IP 定位只有城市级，绝不能用于算步行/骑行。
"""
from __future__ import annotations

import math

from .config import COORD_SYSTEM
from .schema import Location

# ---------------- WGS-84 → GCJ-02（火星坐标）----------------
# 浏览器 Geolocation / 小程序 getLocation 返回 WGS-84；店铺 geo 是 GCJ-02，
# 不转换会偏移数百米。这里实现经典转换算法。

_PI = 3.1415926535897932384626
_A = 6378245.0          # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20 * math.sin(6 * x * _PI) + 20 * math.sin(2 * x * _PI)) * 2 / 3
    ret += (20 * math.sin(y * _PI) + 40 * math.sin(y / 3 * _PI)) * 2 / 3
    ret += (160 * math.sin(y / 12 * _PI) + 320 * math.sin(y * _PI / 30)) * 2 / 3
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * math.sqrt(abs(x)))
    ret += (20 * math.sin(6 * x * _PI) + 20 * math.sin(2 * x * _PI)) * 2 / 3
    ret += (20 * math.sin(x * _PI) + 40 * math.sin(x / 3 * _PI)) * 2 / 3
    ret += (150 * math.sin(x / 12 * _PI) + 300 * math.sin(x / 30 * _PI)) * 2 / 3
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02；境外坐标原样返回。"""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return round(lat + dlat, 6), round(lon + dlon, 6)


# 常用商圈中心点兜底（GCJ-02）——档位 A 纯 API 用，零前端零授权
BUSINESS_AREA_CENTERS: dict[str, tuple[float, float]] = {
    "体育西路": (23.1347, 113.3230),
    "珠江新城": (23.1193, 113.3245),
    "北京路": (23.1220, 113.2684),
    "江南西": (23.0874, 113.2595),
    "天河城": (23.1317, 113.3224),
    "岗顶": (23.1341, 113.3436),
    "广州塔": (23.1066, 113.3245),
}


class LocationProvider:
    """优先级链：显式参数 > 会话内存 > 客户端 GPS(WGS84→GCJ02) > IP(城市级) > 追问。"""

    def __init__(self, resolver=None, session_location: dict | None = None):
        self.resolver = resolver          # provider.resolve_location
        self.session_location = session_location

    def resolve(
        self,
        origin: dict | None = None,
        business_area: str | None = None,
        client_gps: dict | None = None,
        use_ip: bool = False,
        ip: str | None = None,
    ) -> Location | None:
        # 1) 请求显式参数
        if origin and origin.get("lat") is not None and origin.get("lon") is not None:
            return Location(
                lat=float(origin["lat"]), lon=float(origin["lon"]),
                coord=origin.get("coord", COORD_SYSTEM),
                precision=origin.get("precision", "precise"),   # 保留显式传入的精度（如 IP city）
                source=origin.get("source", "explicit"),
                display_name=origin.get("display_name", ""),
            )
        # 2) 会话内上次位置（仅内存，不落库）
        if self.session_location:
            sl = {**self.session_location, "coord": self.session_location.get("coord", COORD_SYSTEM)}
            return Location(**sl)
        # 3) 客户端定位：WGS-84 → GCJ-02
        if client_gps:
            lat, lon = wgs84_to_gcj02(float(client_gps["lat"]), float(client_gps["lon"]))
            self.session_location = {
                "lat": lat, "lon": lon, "coord": COORD_SYSTEM,
                "precision": "precise", "source": "geolocation", "display_name": "",
            }
            return Location(**self.session_location)
        # 3.5) 商圈中心点（档位 A：零授权可跑通）
        if business_area and business_area in BUSINESS_AREA_CENTERS:
            lat, lon = BUSINESS_AREA_CENTERS[business_area]
            return Location(
                lat=lat, lon=lon, coord=COORD_SYSTEM, precision="precise",
                source="business_area", display_name=business_area,
            )
        # 4) IP 定位：城市级，仅兜底，调用方应拒绝用它算步行/骑行
        if use_ip and self.resolver:
            loc = self.resolver(ip=ip)
            if loc:
                return Location(
                    lat=loc["lat"], lon=loc["lon"], coord=loc.get("coord", COORD_SYSTEM),
                    precision=loc.get("precision", "city"), source="ip",
                    display_name=loc.get("display_name", ""),
                )
        # 5) 都无法定位 → 需要用户追问，不硬凑
        return None
