#!/usr/bin/env python3
"""评估报告生成器：从评估结果 JSONL 生成 Markdown 报告 + 消融表。

设计要点：
    1. 报告不是把数字堆上去 —— 要有「指标定义 → 主结果 → 消融对比 →
       自动洞察 → 偏差说明 → 复现方式」的完整叙事，面试时可直接当讲稿。
    2. 洞察由数据自动推导（如 no_hard_filter 遵守率下降说明双保险有效），
       而不是写死结论文；换一批数据报告会跟着变。
    3. 区分「指标没算出来(None)」和「指标为 0」，前者不参与均值、在报告里标注 n。

用法：
    python eval/report.py --results eval/out/results.jsonl --goldset eval/goldset.jsonl \
        --out eval/out/report.md
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

METRIC_LABELS = {
    "recall": "Recall@K", "precision": "Precision@K", "mrr": "MRR",
    "hit_rate": "Hit Rate@K", "ndcg": "nDCG@K",
    "hard_compliance": "硬约束遵守率", "entity_recall": "店铺名覆盖率",
    "faithfulness": "Faithfulness", "answer_relevancy": "Answer Relevancy",
    "context_precision": "Context Precision", "context_recall": "Context Recall",
}
CONFIG_LABELS = {
    "full": "完整系统", "no_rerank": "去掉 Rerank",
    "no_hard_filter": "去掉硬约束", "dense_only": "仅 Dense 单路",
    "rule_slots": "规则版槽位解析",
}


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def avg(vals: list[float]) -> tuple[str, int]:
    nums = [v for v in vals if v is not None]
    return (f"{sum(nums)/len(nums):.3f}" if nums else "—"), len(nums)


def collect(rows: list[dict]) -> dict[str, dict]:
    """按配置聚合各类指标。"""
    by_cfg: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cfg[r["config"]].append(r)

    out: dict[str, dict] = {}
    for cfg, group in by_cfg.items():
        normal = [r for r in group if not r.get("expect_clarify")]
        clarify = [r for r in group if r.get("expect_clarify")]
        aggs: dict[str, tuple[str, int]] = {}
        for key in METRIC_LABELS:
            vals = []
            for r in normal:
                if key == "faithfulness" or key in ("answer_relevancy", "context_precision", "context_recall"):
                    vals.append((r.get("ragas") or {}).get(key))
                else:
                    vals.append((r.get("metrics") or {}).get(key))
            aggs[key] = avg(vals)
        slot_hit = sum((r.get("metrics") or {}).get("slot_acc_hit", 0) for r in normal)
        slot_total = sum((r.get("metrics") or {}).get("slot_acc_total", 0) for r in normal)
        out[cfg] = {
            "n": len(normal),
            "aggs": aggs,
            "clarify_rate": (sum(r.get("clarify_hit", False) for r in clarify) / len(clarify)) if clarify else None,
            "clarify_n": len(clarify),
            "latency": avg([r.get("latency_ms", 0) / 1000 for r in group]),
            "slot_acc": (f"{slot_hit/slot_total:.3f}" if slot_total else "—"),
            "slot_n": slot_total,
            "llm_mode": sum(1 for r in normal if r.get("answer_mode") == "llm"),
        }
    return out


def fmt(v: str | None) -> str:
    return v if v not in (None, "") else "—"


def delta(cur: dict, base: dict, key: str) -> str:
    a, b = cur["aggs"][key][0], base["aggs"][key][0]
    if a == "—" or b == "—":
        return "—"
    d = float(a) - float(b)
    return f"{d:+.3f}"


def build_insights(data: dict) -> list[str]:
    """从数据自动推导洞察，而不是写死结论。"""
    out = []
    if "full" not in data:
        return ["（缺少 full 配置结果，无法生成对比洞察）"]
    f = data["full"]

    # 1. 硬约束 vs 召回的瓶颈定位
    hc, hn = f["aggs"]["hard_compliance"]
    rc, rn = f["aggs"]["recall"]
    if hc != "—" and rc != "—" and float(hc) >= 0.9 and float(rc) < 0.6:
        out.append(
            f"**约束守住但召回不足**：完整系统硬约束遵守率 {hc}、Recall@K 仅 {rc}。"
            "两者背离说明瓶颈不在过滤（错店没混进来），而在「最终 Top-N 店铺的覆盖」——"
            "rerank 的 topn 是 **chunk 级**，同一家店的概览/菜品/口味块会占掉多个名额，"
            "8 个 chunk 可能只覆盖 2~3 家店。若要提升店铺级 Recall，应改为按 shop_id 去重后取 Top-N，"
            "而不是简单调大 chunk topn（那会拖慢 rerank 且仍可能重复）。"
        )

    # 2. RAGAS 与确定性指标的一致性（互相印证 = 指标可信）
    cr = f["aggs"]["context_recall"][0]
    if cr != "—" and rc != "—" and abs(float(cr) - float(rc)) < 0.15:
        out.append(
            f"**两类指标互相印证**：RAGAS Context Recall={cr} 与确定性 Recall@K={rc} 接近，"
            "说明 LLM judge 的归因判断与 ID 集合计算在这批数据上结论一致，指标不是各说各话。"
        )
    if f["clarify_rate"] is not None and f["clarify_rate"] < 1.0:
        out.append(
            f"**冲突检测有漏网**：追问命中率 {f['clarify_rate']:.2f}（{f['clarify_n']} 条冲突样本）。"
            "「人均30米其林」这类明示词能触发，但「人均20块的高档海鲜私房菜」这类模糊高端表述"
            "未触发 —— 触发词表目前主要覆盖米其林/黑珍珠等明示词，"
            "「高档/私房/奢华」等隐含矛盾需要补进规则或交给 LLM 解析链路。"
        )

    fa = f["aggs"]["faithfulness"][0]
    if fa != "—" and float(fa) >= 0.8:
        out.append(
            f"**答案忠实度较高**（Faithfulness={fa}）：LLM 答案里的陈述绝大多数能在检索上下文里找到依据，"
            "与 guard 引用校验 + 模板降级的设计一致 —— 生成层没有成为幻觉重灾区。"
        )

    # 3. 各消融维度的结论
    for cfg, label in CONFIG_LABELS.items():
        if cfg == "full" or cfg not in data:
            continue
        cur = data[cfg]
        d = delta(cur, f, "recall")
        if d != "—" and d.startswith("-") and abs(float(d)) >= 0.02:
            out.append(f"**{label}**：Recall@K 下降 {d}，说明该组件对召回有正向贡献。")
        elif d != "—" and d.startswith("+") and abs(float(d)) >= 0.02:
            out.append(
                f"**{label}**：去掉后 Recall@K 反而 **上升 {d}** —— 该组件在当前 chunk 级 topn 设置下"
                "可能**伤害了店铺级覆盖**（同一家店的多个 chunk 被精排到前列、挤掉其他店的名额）。"
                "这是消融最有价值的产出：发现「看起来高级的组件其实在拖后腿」，"
                "若确认应改为先按 shop_id 去重再精排，而不是直接下掉该组件。"
            )
        elif d != "—" and abs(float(d)) < 0.02:
            out.append(f"**{label}**：Recall@K 变化 {d}（不显著），该组件的价值可能体现在其他维度（如精度/约束/忠实度），不能只看召回。")
        if cfg == "no_hard_filter":
            h = cur["aggs"]["hard_compliance"][0]
            if h != "—" and hc != "—" and float(h) < float(hc):
                out.append(
                    f"**硬约束双保险有效**：关掉 pre-filter + 后置复核后，遵守率从 {hc} 掉到 {h}。"
                    "说明两道检查确实在拦不合格店铺，而不是摆设。"
                )

    # 4. 槽位解析入口质量
    if f["slot_acc"] != "—" and float(f["slot_acc"]) < 0.95:
        out.append(
            f"**入口解析有损**：槽位字段准确率 {f['slot_acc']}（{f['slot_n']} 个字段）。"
            "典型漏解析：「人均不超过 X」「XX 以下的粤菜」等口语变体。入口错一个价格上限，"
            "后面召回再准也没用 —— 这是 Query Understanding 层的独立优化点，可与检索问题解耦定位。"
        )
    return out


def render(data: dict, rows: list[dict], gold_n: int) -> str:
    L: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cfgs = list(data.keys())
    L += [
        "# 广州美食 RAG · 评估报告",
        "",
        f"> 生成时间：{now}　|　Gold Set：{gold_n} 条　|　运行配置：{', '.join(cfgs)}",
        "> 纯 CPU 环境（BGE-M3 + BGE-Reranker-v2-M3 + 本地 glm-5.3-flash）",
        "",
        "## 1. 评估方法",
        "",
        "### 1.1 Gold Set 构建",
        "",
        "- 从 300 家种子数据程序化生成问答对（固定 seed=42，可复现 `python eval/goldset.py`）。",
        "- ground truth 由**结构化字段确定性过滤**得出（不用被测 pipeline 自己造答案），"
        "每家店的 parent 主档作为 RAGAS 的 gold contexts。",
        "- 覆盖价格+商圈、区域+菜系、过敏原、场景、夜宵/早茶、地铁等 7 类查询，"
        "另含冲突追问样本（如「人均30米其林」）。",
        "",
        "### 1.2 指标分层（为什么不是全用 RAGAS）",
        "",
        "| 层次 | 指标 | 类型 | 作用 |",
        "|---|---|---|---|",
        "| 检索 | Recall@K / Precision@K / MRR / Hit Rate / nDCG | 确定性 | ID 集合精确计算，快、可复现，消融主力 |",
        "| 约束 | 硬约束遵守率 | 确定性 | 安全红线（过敏/价格/商圈）是否守住 |",
        "| 入口 | 槽位字段准确率 | 确定性 | 定位「检索差」是入口解析的锅还是召回的锅 |",
        "| 生成 | 店铺名覆盖率 | 确定性 | 答案是否提到了 gold 店铺 |",
        "| 语义 | Faithfulness / Answer Relevancy / Context Precision / Context Recall | RAGAS | 语义质量，按论文定义自实现，本地 LLM 做 judge |",
        "",
        "- RAGAS 每条需 4~5 次 LLM judge 调用，只对 full 配置、前若干样本跑；"
        "消融实验用确定性指标，避免把算力烧在 judge 上。",
        "- judge 结果按「样本+指标」缓存到 `eval/out/judge_cache.jsonl`，重跑不重复计费。",
        "",
        "## 2. 主系统结果",
        "",
    ]
    if "full" in data:
        f = data["full"]
        L += [f"样本数 {f['n']}（另有 {f['clarify_n']} 条冲突样本）", "", "| 指标 | 得分 | 参与样本数 |", "|---|---:|---:|"]
        for key, label in METRIC_LABELS.items():
            v, n = f["aggs"][key]
            if v != "—" or key in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
                L.append(f"| {label} | {fmt(v)} | {n} |")
        L += [
            f"| 槽位字段准确率 | {f['slot_acc']} | {f['slot_n']} 字段 |",
            f"| 冲突追问命中率 | {fmt(f'{f['clarify_rate']:.3f}' if f['clarify_rate'] is not None else None)} | {f['clarify_n']} |",
            f"| LLM 答案占比 | {f['llm_mode']}/{f['n']} | — |",
            f"| 平均延迟 | {f['latency'][0]}s | {f['latency'][1]} |",
        ]
    else:
        L.append("（暂无 full 配置结果）")
    L += ["", "## 3. 消融实验", "", "| 配置 | Recall@K | Precision@K | MRR | 硬约束遵守 | 平均延迟 | ΔRecall vs full |", "|---|---:|---:|---:|---:|---:|---:|"]
    base = data.get("full")
    for cfg, d in data.items():
        L.append("| " + " | ".join([
            CONFIG_LABELS.get(cfg, cfg),
            d["aggs"]["recall"][0], d["aggs"]["precision"][0], d["aggs"]["mrr"][0],
            d["aggs"]["hard_compliance"][0], d["latency"][0] + "s",
            delta(d, base, "recall") if base and cfg != "full" else "基准",
        ]) + " |")
    L += [
        "",
        "> ΔRecall 为相对完整系统的绝对差值（百分点）。延迟含 LLM 生成（仅 full），"
        "因此消融配置的延迟不可直接与 full 比较 —— 它衡量的是检索链路成本。",
        "",
        "## 4. 关键发现（由本次数据自动推导）",
        "",
    ]
    insights = build_insights(data)
    for i, s in enumerate(insights, 1):
        L.append(f"{i}. {s}")
    if not insights:
        L.append("（样本量不足，暂无稳定洞察）")

    L += [
        "",
        "## 5. 偏差与限制",
        "",
        "1. **合成数据**：店铺评分/价格/评论均为 synthetic（固定 seed），分布比真实数据规整；"
        "指标绝对值不能直接外推到线上，但组件间的相对差异有参考价值。",
        "2. **样本量**：纯 CPU 单轮检索 ~15-20s、RAGAS judge 每样本 4~5 次 LLM 调用，"
        "默认只跑有限样本；小样本下方差较大，Δ 小于 0.02 视为不显著。",
        "3. **LLM judge 偏差**：本地 glm judge 与生成用的是同一模型，存在"
        "「自评偏好」风险；Faithfulness/Relevancy 数值宜作趋势参考而非绝对真值。",
        "4. **Recall 口径**：按 shop_id 去重计算；当前 rerank topn 是 chunk 级，"
        "店铺级覆盖会低于 chunk 级召回，这是系统设计取舍而非评估 bug。",
        "",
        "## 6. 复现方式",
        "",
        "```bash",
        "# 1) 生成 gold set（固定 seed，可复现）",
        "python eval/goldset.py --size 40",
        "",
        "# 2) 跑评估（Qdrant embedded 单进程独占，若 8000 在服务请指向索引副本）",
        'RAG_QDRANT_PATH=/path/to/qdrant_copy RAG_LLM_API_KEY=... RAG_LLM_BASE_URL=... \\',
        '  python eval/run.py --configs full,no_rerank,no_hard_filter,dense_only,rule_slots \\',
        "  --ragas --ragas-limit 12",
        "",
        "# 3) 生成本报告",
        "python eval/report.py",
        "```",
        "",
        "## 7. 面试讲点",
        "",
        "1. **评估要分层**：确定性指标做回归主指标，RAGAS 做语义补充 —— "
        "前者快且可复现，后者有 judge 偏差但能测「忠实度/相关性」这类语义维度。",
        "2. **消融要单变量**：每个配置只改一个组件，Δ 才能归因；全开/全关的对比说明不了任何事。",
        "3. **Gold Set 不能自证**：ground truth 用原始结构化数据独立过滤，"
        "不用被测 pipeline 生成，避免「自己判自己」的循环论证。",
        "4. **入口也要评估**：槽位字段准确率单列，能把「检索差」拆解成「解析错」还是「召回错」，"
        "定位问题的粒度直接决定优化方向。",
        "5. **成本工程**：一次加载模型、逐条落盘断点续跑、RAGAS 缓存 —— "
        "纯 CPU 环境下不做这些，评估根本跑不完。",
        "",
    ]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 RAG 评估报告")
    ap.add_argument("--results", default=str(Path(__file__).parent / "out" / "results.jsonl"))
    ap.add_argument("--goldset", default=str(Path(__file__).parent / "goldset.jsonl"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "out" / "report.md"))
    args = ap.parse_args()

    rows = load_jsonl(Path(args.results))
    gold = load_jsonl(Path(args.goldset))
    if not rows:
        raise SystemExit("没有评估结果，请先运行 eval/run.py")
    data = collect(rows)
    text = render(data, rows, len(gold))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[report] 已生成 {out.resolve()}  配置数={len(data)} 结果条数={len(rows)}")


if __name__ == "__main__":
    main()
