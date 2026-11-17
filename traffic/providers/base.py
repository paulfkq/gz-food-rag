"""Provider 抽象接口（docs/02 §8：MapProvider 适配层）。

统一契约：输入 origin + destinations + modes，输出"厂商原始结构字典"列表；
单位归一化 / 最短路选择 / 粒度分级统一交给 normalize.py 处理——
这样新旧厂商、mock 与真实 API 走同一条归一化逻辑，单测只覆盖一处。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import ALLOWED_PROVIDERS


class MapProvider(ABC):
    """所有地图供应商必须实现的接口。"""

    name = "base"

    @abstractmethod
    def route_plans(
        self,
        origin: dict,
        destinations: list[dict],
        modes: list[str],
        departure_time: int | None = None,
        policy: str = "LEAST_TIME",
    ) -> dict[str, list[dict]]:
        """返回 {mode: [每个目的地的原始 route 响应字典]}。

        原始响应保持厂商字段（分钟/分单位、routes[]、steps[]），由 normalize 处理。
        任一 mode 失败时该 mode 返回 []，由上层标 available=False，绝不抛给主链路。
        """
        raise NotImplementedError

    @abstractmethod
    def resolve_location(self, address: str | None = None, ip: str | None = None) -> dict | None:
        """地址/IP → 位置字典 {lat, lon, coord, precision, source}；失败返回 None。"""
        raise NotImplementedError

    def health(self) -> dict:
        """可观测：配额余量 / 延迟 / 是否真实数据。"""
        return {"provider": self.name, "real_data": False}


def make_provider(name: str) -> MapProvider:
    """工厂：合规白名单校验 + 无 key 自动降级 mock。"""
    from .. import secrets
    from .mock import MockProvider
    from .tencent import TencentProvider

    secrets.assert_compliant_provider(name)   # 不在白名单（如 mapbox）直接拒绝
    key = secrets.get_map_key(name)
    if name == "tencent" and key:
        return TencentProvider(api_key=key)
    # amap/baidu/tianditu 尚未实现真实适配，或未配 key → 降级 mock（明确标注）
    return MockProvider(real_provider=name if key else "mock")
