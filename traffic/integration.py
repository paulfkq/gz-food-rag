"""RAG 接入辅助（可选、零耦合）：检索后 → LLM 前，把交通富化成可溯源文本。

本文件**不 import 主服务任何模块**，主链路只需加几行即可接入：

    from traffic.integration import TRAFFIC_RULES, enrich_for_hits, traffic_prompt_block

    enrichment = await run_in_threadpool(enrich_for_hits, hits, origin=...)
    system_prompt = SYSTEM_PROMPT + TRAFFIC_RULES
    user_prompt = build_user_prompt(...) + traffic_prompt_block(enrichment, hits)

设计要点（docs/02 红线）：
    1. 富化结果只作为"交通参考"块追加到 prompt，不修改 hits / shops，
       也不参与检索打分 —— RAG 指标（Recall/NDCG）开/关完全一致。
    2. 交通数字只能来自地图服务；guard 仍按餐厅资料校验引用编号，
       交通块里的数字不参与"照抄餐厅资料"的校验口径。
    3. mock 数据必须显式标注，绝不伪装真实路况。
    4. 任何异常都被吞掉返回 None —— 地图服务挂了，RAG 答案 100% 照常生成。
"""
from __future__ import annotations

import os
from functools import lru_cache

from . import config
from .enricher import RouteEnricher
from .schema import EnrichmentResult

# 追加到现有 SYSTEM_PROMPT 末尾即可；不改动原有 5 条铁律。
TRAFFIC_RULES = """

## 交通信息补充规则（仅当资料里出现【实时交通参考】时生效）
6. 交通数字（耗时/换乘/步行/票价）只能照抄【实时交通参考】里给出的数值，禁止自行估算、换算或四舍五入。
7. 交通信息是地图服务单独计算的参考，不属于餐厅资料；不要把它和资料里的"地铁步行时间"混为一谈，也不要互相覆盖。
8. 若交通信息标注为【模拟数据】，必须在答案里明确说明"交通为演示用模拟数据，非真实路况"。
9. 某家店没有交通数据时就说"暂无交通数据"，不要补数字。
10. 交通信息不要占用餐厅的引用编号 [1][2]；引用编号仍只用于资料中的店铺。
"""


@lru_cache(maxsize=1)
def _enricher() -> RouteEnricher:
    """懒加载单例：首次调用才构造（无 key 时是 mock provider，不加载任何模型）。"""
    return RouteEnricher()


def _shop_id_of(hit) -> str:
    if isinstance(hit, dict):
        p = hit
    else:
        p = getattr(hit, "payload", None) or {}
    return str(p.get("shop_id") or getattr(hit, "shop_id", "") or p.get("id") or "")


def _geo_of(hit) -> dict:
    p = hit if isinstance(hit, dict) else (getattr(hit, "payload", None) or {})
    geo = p.get("geo") or {}
    if isinstance(geo, dict) and geo.get("lat") is not None:
        return {"lat": geo["lat"], "lon": geo.get("lon", geo.get("lng"))}
    return {}


def enrich_for_hits(
    hits,
    origin: dict | None = None,
    *,
    business_area: str | None = None,
    client_gps: dict | None = None,
    use_ip: bool = False,
    modes: list[str] | None = None,
) -> EnrichmentResult | None:
    """从检索结果直接富化；失败返回 None（绝不阻塞主链路）。

    起点优先级与 traffic.enricher 一致：
        origin(显式) > client_gps(WGS84 自动转 GCJ02) > business_area > IP(城市级，会被拦截精细 mode)
    若不传任何起点，会读环境变量 TRAFFIC_DEFAULT_AREA 作为商圈兜底（如"体育西路"）。
    """
    try:
        shops = []
        # 省额度：只富化 Top-N 家（按检索顺序），未富化的店在答案里暂无交通数据
        for i, h in enumerate(list(hits or [])[:config.MAX_SHOPS], 1):
            geo = _geo_of(h)
            if not geo:
                continue
            shops.append({
                "shop_id": _shop_id_of(h) or f"HIT-{i}",
                "geo": geo,
                "name": (h.get("name") if isinstance(h, dict)
                         else getattr(h, "shop_name", "")),
            })
        if not shops:
            return None

        ba = business_area or os.environ.get("TRAFFIC_DEFAULT_AREA") or None
        if origin is None and client_gps is None and ba is None and not use_ip:
            # 没有任何起点信息：不要硬猜，让上层跳过富化
            return None

        return _enricher().enrich(
            shops=shops,
            origin=origin,
            business_area=ba,
            client_gps=client_gps,
            use_ip=use_ip,
            modes=modes,
        )
    except Exception:
        # 地图服务是旁路：任何异常都不能影响 RAG 正常回答
        return None


def _fmt_min(sec) -> str:
    if sec is None:
        return "暂无"
    return f"{round(int(sec) / 60)}分钟"


def traffic_prompt_block(enrichment, hits=None, *, with_lines: bool = True) -> str:
    """把富化结果转成紧凑 prompt 块，按 hits 顺序绑定 [编号]。

    只输出duration/换乘摘要，不输出坐标串/polyline（合规红线）。
    """
    if enrichment is None:
        return ""
    data = enrichment if isinstance(enrichment, dict) else enrichment.model_dump(mode="json")
    plans = data.get("route_plans") or []
    if not plans:
        return ""

    # 编号映射：检索第 1 家 = [1]，与 build_user_prompt 的编号一致
    id_to_ref: dict[str, int] = {}
    id_to_name: dict[str, str] = {}
    if hits:
        for i, h in enumerate(hits, 1):
            sid = _shop_id_of(h)
            if sid:
                id_to_ref[sid] = i
                p = h if isinstance(h, dict) else (getattr(h, "payload", None) or {})
                nm = p.get("name") or getattr(h, "shop_name", "")
                if nm:
                    id_to_name[sid] = str(nm)

    by_shop: dict[str, dict] = {}
    for p in plans:
        if not p.get("available", True) or p.get("duration_sec") is None:
            continue
        sid = str(p.get("shop_id", ""))
        by_shop.setdefault(sid, {})[p.get("mode")] = p

    if not by_shop:
        return ""

    is_mock = any(str(p.get("source", "")).startswith("mock")
                  for p in plans if p.get("available", True))
    lines = ["", "## 实时交通参考（由地图服务在检索后单独计算，不属于餐厅资料）"]
    if is_mock:
        lines.append("【模拟数据】当前未配置真实地图 key，以下交通为演示用模拟数据，非真实路况。")

    for sid, by_mode in by_shop.items():
        ref = f"[{id_to_ref[sid]}]" if sid in id_to_ref else f"（{sid}）"
        label = id_to_name.get(sid, sid)
        bits = []
        for mode in ("driving", "cycling", "walking", "transit"):
            p = by_mode.get(mode)
            if not p:
                continue
            if mode == "transit":
                detail = f"公交 {_fmt_min(p.get('duration_sec'))}"
                extra = []
                if p.get("transfer_count") is not None:
                    extra.append(f"{p['transfer_count']}次换乘")
                if p.get("transfer_walk_sec") is not None:
                    extra.append(f"换乘步行{_fmt_min(p['transfer_walk_sec'])}")
                if p.get("fare_yuan") is not None:
                    extra.append(f"票价¥{p['fare_yuan']:g}")
                if extra:
                    detail += "（" + "，".join(extra) + "）"
                if with_lines:
                    seq = []
                    for leg in p.get("legs", []):
                        if leg.get("kind") == "transit" and leg.get("line_name"):
                            seq.append(leg["line_name"])
                    if seq:
                        detail += "：" + "→".join(seq)
                bits.append(detail)
            else:
                name = {"driving": "驾车", "cycling": "骑行", "walking": "步行"}[mode]
                bits.append(f"{name} {_fmt_min(p.get('duration_sec'))}")
        if bits:
            lines.append(f"- {ref} {label}：" + "；".join(bits))
    return "\n".join(lines) + "\n"
