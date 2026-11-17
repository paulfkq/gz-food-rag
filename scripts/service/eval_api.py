"""评估系统可视化 API（面试讲点在文末）。

把原本只能在命令行跑的 eval/run.py + report.py + compare.py 包成 HTTP 端点，
让前端能：列历史评估、看指标、触发新评估、看进度、与历史对比。

设计要点：
    1. 【不重启模型】评估 API 只做「文件扫描 + 聚合 + 起子进程」，
       不 import RetrievalPipeline —— 否则会在 8000 服务进程里再加载一份 4.4GB 模型。
       真正跑评估是独立的 eval/run.py 子进程，且指向索引副本（Qdrant embedded 单进程独占）。
    2. 【env 继承】Popen 默认继承父进程环境，8000 服务已注入 RAG_LLM_*，
       子进程直接可用；只强制覆盖 RAG_QDRANT_PATH 指向副本，避免锁冲突。
    3. 【单任务并发限制】一次只允许一个评估在跑（纯 CPU + 共享索引副本），
       多个并发只会互相抢核/写坏副本，收益为零。
    4. 【任务状态落盘】tasks.json 记录历史任务；服务重启后把 running 标成 interrupted
       （子进程随父进程消亡），避免前端永远转圈。
    5. 【run_id 白名单校验】防路径穿越：只接受 OUT_DIR 下 results*.jsonl 的文件名。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
sys.path.insert(0, str(EVAL_DIR))

from fastapi import APIRouter, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import report as ER  # noqa: E402  （复用聚合口径，保证 API 与 md 报告一致）
import compare as EC  # noqa: E402

router = APIRouter(prefix="/api/eval", tags=["eval"])

OUT_DIR = EVAL_DIR / "out"
TASKS_FILE = OUT_DIR / "eval_tasks.json"

# 内存态：进程对象（不可序列化，重启即失效）+ 任务元数据（落盘）
PROCS: dict[str, subprocess.Popen] = {}
TASKS: dict[str, dict] = {}

VALID_CONFIGS = ["full", "no_rerank", "no_hard_filter", "dense_only", "rule_slots", "chunk_topn"]


# ---------------- 任务状态持久化 ----------------

def init_tasks() -> None:
    """模块加载时调用：读取历史任务，把上次服务遗留的 running 标记为中断。"""
    if TASKS_FILE.exists():
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            for tid, t in data.items():
                if t.get("status") == "running":
                    t["status"] = "interrupted"
                    t["note"] = "服务重启导致任务中断，可重新触发"
                TASKS[tid] = t
        except Exception:  # noqa: BLE001
            pass


def _save_tasks() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(TASKS, ensure_ascii=False, indent=2), encoding="utf-8")


def _refresh(tid: str, t: dict) -> None:
    """用 Popen.poll() 刷新任务状态；进程已退出则落终态。"""
    if t.get("status") != "running":
        return
    p = PROCS.get(tid)
    if p is None:
        t["status"] = "interrupted"
        t["note"] = "评估进程已不存在"
        _save_tasks()
        return
    rc = p.poll()
    if rc is not None:
        t["status"] = "done" if rc == 0 else "failed"
        t["returncode"] = rc
        t["finished"] = datetime.now().isoformat(timespec="seconds")
        t["note"] = "完成" if rc == 0 else f"退出码 {rc}，见日志"
        _save_tasks()


def _tail(path: Path, n: int = 50) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:  # noqa: BLE001
        return []


def _progress(lines: list[str]) -> dict | None:
    """从日志尾部解析最后一个 [cur/total] 进度标记。"""
    for line in reversed(lines):
        m = re.search(r"\[(\d+)/(\d+)\]", line)
        if m:
            return {"cur": int(m.group(1)), "total": int(m.group(2)),
                    "pct": round(100 * int(m.group(1)) / max(1, int(m.group(2))), 1)}
    return None


def _is_run_file(name: str) -> bool:
    """是否是评估结果文件：含 results 字样的 jsonl（含 baseline_results / results_v2）。
    judge_cache.jsonl 等缓存不含 results，自动排除。"""
    return (Path(name).name == name and name.endswith(".jsonl") and "results" in name)


def _safe_run_file(run_id: str) -> Path:
    """校验 run_id：只接受 OUT_DIR 下、含 results 的 jsonl 文件名，防路径穿越。"""
    if not _is_run_file(run_id):
        raise HTTPException(status_code=400, detail="非法 run_id")
    p = OUT_DIR / run_id
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"评估结果不存在: {run_id}")
    return p


def _load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _file_meta(path: Path, rows: list[dict]) -> dict:
    st = path.stat()
    configs = sorted({r.get("config", "") for r in rows})
    has_ragas = any((r.get("ragas") or {}).get("faithfulness") is not None for r in rows)
    return {
        "id": path.name,
        "rows": len(rows),
        "samples": len({r.get("sample_id") for r in rows}),
        "configs": configs,
        "has_ragas": has_ragas,
        "size_kb": round(st.st_size / 1024, 1),
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------- 查询端点 ----------------

@router.get("/config")
def eval_config():
    """前端表单/表头渲染所需的静态定义：指标标签、配置标签、合法消融配置。"""
    return {
        "metric_labels": ER.METRIC_LABELS,
        "config_labels": {**ER.CONFIG_LABELS, "chunk_topn": "关店铺去重（旧行为）"},
        "key_metrics": EC.KEY_METRICS,
        "valid_configs": VALID_CONFIGS,
    }


@router.get("/runs")
def list_runs():
    """列出所有历史评估运行（results*.jsonl），按时间倒序。"""
    if not OUT_DIR.exists():
        return {"runs": [], "total": 0}
    runs = []
    for p in OUT_DIR.glob("*.jsonl"):
        if not _is_run_file(p.name):
            continue
        try:
            rows = _load_rows(p)
            if rows:
                runs.append(_file_meta(p, rows))
        except Exception:  # noqa: BLE001
            continue
    runs.sort(key=lambda x: x["mtime"], reverse=True)
    return {"runs": runs, "total": len(runs)}


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: str):
    """某次运行的聚合指标 + 自动洞察（口径与 report.py 完全一致）。"""
    rows = _load_rows(_safe_run_file(run_id))
    data = ER.collect(rows)
    gold_n = 0
    gold_path = EVAL_DIR / "goldset.jsonl"
    if gold_path.exists():
        gold_n = sum(1 for line in gold_path.open(encoding="utf-8") if line.strip())
    return {
        "run_id": run_id,
        "gold_n": gold_n,
        "total_rows": len(rows),
        "configs": data,                       # {cfg: {n, aggs:{key:[str,n]}, ...}}
        "insights": ER.build_insights(data),
    }


@router.get("/runs/{run_id}/detail")
def run_detail(run_id: str, config: str = "full", limit: int = 50):
    """逐条结果（供"结果展示"表格用），按 config 过滤，字段精简。"""
    rows = _load_rows(_safe_run_file(run_id))
    picked = [r for r in rows if r.get("config") == config]
    out = []
    for r in picked[:limit]:
        out.append({
            "sample_id": r.get("sample_id"),
            "query": r.get("query"),
            "type": r.get("type"),
            "expect_clarify": r.get("expect_clarify"),
            "clarify_hit": r.get("clarify_hit"),
            "retrieved_names": r.get("retrieved_names", []),
            "metrics": r.get("metrics", {}),
            "ragas": r.get("ragas", {}),
            "answer": (r.get("answer") or "")[:400],
            "answer_mode": r.get("answer_mode"),
            "latency_ms": r.get("latency_ms"),
        })
    return {"run_id": run_id, "config": config, "total": len(picked), "rows": out}


@router.get("/compare")
def compare_runs(base: str, new: str):
    """两次运行对比：full 主指标逐项 Δ + 各配置 Recall 对比 + 消融归因。"""
    base_rows = _load_rows(_safe_run_file(base))
    new_rows = _load_rows(_safe_run_file(new))
    bd, nd = ER.collect(base_rows), ER.collect(new_rows)

    def metric_rows(bcfg: dict | None, ncfg: dict | None) -> list[dict]:
        out = []
        for key in EC.KEY_METRICS + ["entity_recall"]:
            bv = bcfg["aggs"][key][0] if bcfg else "—"
            nv = ncfg["aggs"][key][0] if ncfg else "—"
            out.append({"key": key, "label": ER.METRIC_LABELS.get(key, key),
                        "base": bv, "new": nv, "delta": EC.cell(nv, bv)})
        # 冲突命中率 & 延迟
        for key, label, higher in [("clarify_rate", "冲突追问命中率", True),
                                   ("latency", "平均延迟(s)", False)]:
            if key == "clarify_rate":
                braw = bcfg.get("clarify_rate") if bcfg else None
                nraw = ncfg.get("clarify_rate") if ncfg else None
                bs = f"{braw:.3f}" if braw is not None else "—"
                ns = f"{nraw:.3f}" if nraw is not None else "—"
            else:
                bs = bcfg["latency"][0] if bcfg else "—"
                ns = ncfg["latency"][0] if ncfg else "—"
            out.append({"key": key, "label": label, "base": bs, "new": ns,
                        "delta": EC.cell(ns, bs, higher_better=higher)})
        return out

    bfull, nfull = bd.get("full"), nd.get("full")
    full_metrics = metric_rows(bfull, nfull)

    ablation = []
    for cfg in VALID_CONFIGS:
        bv = bd.get(cfg, {}).get("aggs", {}).get("recall", ("—", 0))[0]
        nv = nd.get(cfg, {}).get("aggs", {}).get("recall", ("—", 0))[0]
        label = ER.CONFIG_LABELS.get(cfg, cfg)
        if cfg == "chunk_topn":
            label = "关店铺去重（旧行为）"
        ablation.append({"config": cfg, "label": label,
                         "base": bv, "new": nv, "delta": EC.cell(nv, bv)})

    return {
        "base": base, "new": new,
        "base_meta": _file_meta(_safe_run_file(base), base_rows),
        "new_meta": _file_meta(_safe_run_file(new), new_rows),
        "full_metrics": full_metrics,
        "ablation": ablation,
    }


# ---------------- 触发评估 ----------------

class StartRunRequest(BaseModel):
    configs: str = Field("full,no_rerank,no_hard_filter,dense_only,rule_slots,chunk_topn",
                         description="逗号分隔的消融配置")
    limit: int = Field(14, ge=1, le=40, description="正常样本数（冲突样本始终包含）")
    ragas: bool = Field(True, description="是否跑 RAGAS 四指标（需 LLM key）")
    ragas_limit: int = Field(12, ge=1, le=40, description="RAGAS 只跑前 N 条")
    out_name: str = Field("", description="输出文件名，留空自动生成")
    sync_index: bool = Field(False, description="触发前是否从 data/qdrant 同步索引副本")


def _sync_index() -> None:
    src = ROOT / "data" / "qdrant" / "collection"
    dst = OUT_DIR / "qdrant_eval" / "collection"
    if not src.exists():
        raise HTTPException(status_code=400, detail="源索引 data/qdrant/collection 不存在")
    shutil.copytree(src, dst, dir_exist_ok=True, ignore=shutil.ignore_patterns("*.lock"))
    meta_src = ROOT / "data" / "qdrant" / "meta.json"
    if meta_src.exists():
        shutil.copy2(meta_src, OUT_DIR / "qdrant_eval" / "meta.json")


@router.post("/run")
def start_run(req: StartRunRequest):
    """触发一次离线评估（后台子进程），立即返回 task_id 供前端轮询进度。"""
    # 并发限制：已有 running 任务直接拒绝
    for tid, t in list(TASKS.items()):
        _refresh(tid, t)
        if t.get("status") == "running":
            raise HTTPException(status_code=409, detail=f"已有评估任务在运行（{tid}），请等待完成")

    configs = [c.strip() for c in req.configs.split(",") if c.strip()]
    if not configs or any(c not in VALID_CONFIGS for c in configs):
        raise HTTPException(status_code=400, detail=f"configs 只能包含: {VALID_CONFIGS}")

    if req.sync_index:
        _sync_index()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_name = req.out_name.strip() or ("results_" + datetime.now().strftime("%m%d_%H%M%S") + ".jsonl")
    if not out_name.endswith(".jsonl"):
        out_name += ".jsonl"
    if Path(out_name).name != out_name or not out_name.startswith("results"):
        raise HTTPException(status_code=400, detail="输出文件名非法（须以 results 开头）")

    out_path = OUT_DIR / out_name
    log_path = OUT_DIR / (out_name[:-6] + ".log")

    py = os.environ.get("RAG_EVAL_PYTHON") or sys.executable
    cmd = [py, str(EVAL_DIR / "run.py"),
           "--out", str(out_path),
           "--configs", ",".join(configs),
           "--limit", str(req.limit)]
    if req.ragas:
        cmd += ["--ragas", "--ragas-limit", str(req.ragas_limit)]

    env = os.environ.copy()
    env["RAG_QDRANT_PATH"] = str(OUT_DIR / "qdrant_eval")  # 强制指向副本，避免与在线服务抢锁
    env["PYTHONUNBUFFERED"] = "1"                          # 日志实时落盘，前端才能看到进度
    logf = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=str(ROOT), env=env)
    logf.close()

    tid = "eval_" + datetime.now().strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:5]
    TASKS[tid] = {
        "id": tid, "status": "running", "pid": proc.pid,
        "configs": configs, "limit": req.limit, "ragas": req.ragas,
        "out": out_name, "log": log_path.name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "note": "已启动，等待模型加载",
    }
    PROCS[tid] = proc
    _save_tasks()
    return {"task_id": tid, "out": out_name, "log": log_path.name}


# ---------------- 任务查询 ----------------

@router.get("/tasks")
def list_tasks():
    for tid, t in list(TASKS.items()):
        _refresh(tid, t)
    items = []
    for tid, t in sorted(TASKS.items(), key=lambda kv: kv[1].get("created", ""), reverse=True):
        item = dict(t)
        lines = _tail(OUT_DIR / t.get("log", "")) if t.get("log") else []
        item["progress"] = _progress(lines)
        item["log_tail"] = lines[-12:]
        items.append(item)
    return {"tasks": items, "running": sum(1 for t in TASKS.values() if t.get("status") == "running")}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    t = TASKS.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _refresh(task_id, t)
    lines = _tail(OUT_DIR / t.get("log", "")) if t.get("log") else []
    return {**t, "progress": _progress(lines), "log_tail": lines[-30:]}


# ---------------- 模块初始化 ----------------

init_tasks()

# ============================ 面试讲点 ============================
# 1. 【聚合口径单一来源】API 直接复用 report.collect / compare.cell，
#    避免「网页一套算法、Markdown 报告另一套」导致数字对不上。
# 2. 【重任务隔离】评估不在 API 进程内跑，而是子进程 + 索引副本：
#    内存不叠加、Qdrant 锁不冲突、评估崩了不影响在线问答服务。
# 3. 【环境变量继承】Popen 继承父进程 env（LLM key 已在 8000 启动时注入），
#    只覆盖必要的 RAG_QDRANT_PATH —— 密钥不落盘、不经过前端。
# 4. 【并发保护 + 状态落盘】单任务限制 + tasks.json + 重启标记 interrupted，
#    是长任务可视化里最容易漏、却直接影响可用性的工程细节。
