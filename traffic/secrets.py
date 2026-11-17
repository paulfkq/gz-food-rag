"""密钥管理：仅从环境变量读取，日志脱敏（docs/02 §2 / §8.2）。

红线：
  - 绝不把 key 写进代码 / 配置 / 日志 / LLM 上下文 / 前端。
  - 这里不提供"默认 key"，没有环境变量就是没有 key，自动降级 mock。
"""
from __future__ import annotations

import os

from . import config


def get_map_key(provider: str | None = None) -> str | None:
    """读取地图 API key。无 key 返回 None，调用方据此降级 mock。

    腾讯以外供应商预留不同环境变量名，保持"一处读取"的收敛。
    """
    provider = (provider or config.MAP_PROVIDER).lower()
    env_names = {
        "tencent": "TENCENT_MAP_KEY",
        "amap": "AMAP_MAP_KEY",
        "baidu": "BAIDU_MAP_KEY",
        "tianditu": "TIANDITU_MAP_KEY",
    }
    name = env_names.get(provider, "TENCENT_MAP_KEY")
    value = os.environ.get(name, "").strip()
    return value or None


def has_real_key(provider: str | None = None) -> bool:
    return get_map_key(provider) is not None


def redact_key(key: str | None) -> str:
    """日志里只显示 key 的前 4 位 + ****，绝不打印全值。"""
    if not key:
        return "<none>"
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-2:]


def assert_compliant_provider(provider: str) -> None:
    """合规校验：不在白名单内的供应商直接拒绝（如 Mapbox）。"""
    if provider not in config.ALLOWED_PROVIDERS:
        raise ValueError(
            f"地图供应商 {provider!r} 不在合规白名单 {config.ALLOWED_PROVIDERS} 内；"
            "境外服务（Mapbox/Google/Apple/OSM）境内数据不合规，禁止使用"
        )
