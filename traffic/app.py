"""独立 FastAPI 服务（默认 8100）——零干扰主链路（8000/5173）。

暴露：
  POST /api/enrich     接收 shops[Top-K，含 geo] + origin → route_plans + status
  POST /mcp            MCP Streamable HTTP（JSON-RPC 2.0，tools/list + tools/call）
  GET  /mcp            MCP 兼容回退提示
  GET  /health         服务健康 + 数据来源（真实/mock）+ 缓存统计
  GET  /demo           简单的四档方案演示页

设计：不修改 scripts/service/app.py 与 schemas.py。若将来接入主链路，
只需在 /api/answer 里并行调用 enricher.enrich() 并用 merge_into_answer() 合并
（见 enricher.py 与 README 的接入示例）——本服务可独立演进。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import config
from .enricher import RouteEnricher
from .mcp_client import make_client
from .mcp_server.server import build_handler, make_router
from .schema import EnrichmentResult

_START = time.time()


class EnrichRequest(BaseModel):
    shops: list[dict] = Field(default_factory=list, description="RAG Top-K，每项含 shop_id 与 geo{lat,lon}")
    origin: dict | None = Field(None, description="起点 {lat,lon,coord?}；优先级最高")
    business_area: str | None = Field(None, description="商圈名（如'体育西路'），零授权兜底")
    client_gps: dict | None = Field(None, description="客户端 WGS-84 {lat,lon}，服务端转 GCJ-02")
    use_ip: bool = Field(False, description="是否允许 IP 城市级兜底")
    modes: list[str] | None = Field(None, description="默认四档全开")
    departure_time: int | None = Field(None, description="Unix 秒；影响公交运营线路")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.enricher = RouteEnricher(client=make_client("inproc"))
    print("[traffic-service] ready on port", config.MCP_PORT, flush=True)
    yield


app = FastAPI(title="广州美食 RAG · 交通富化服务", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# 模块级默认实例：即使 TestClient 不触发 lifespan 也能工作（健壮性）
app.state.enricher = RouteEnricher(client=make_client("inproc"))

# 挂载 MCP Streamable HTTP 路由（同一 handler 也供 inproc client 使用）
_handler = build_handler()
app.include_router(make_router(_handler), tags=["mcp"])


@app.get("/health", tags=["ops"])
def health():
    enricher: RouteEnricher = app.state.enricher
    return {
        "status": "ok",
        "map_provider": config.MAP_PROVIDER,
        "real_data": config.HAS_REAL_KEY,
        "coord_system": config.COORD_SYSTEM,
        "cache": enricher.cache.stats(),
        "uptime_s": round(time.time() - _START, 1),
        "note": "无 key 时为 mock 数据（不伪装真实）" if not config.HAS_REAL_KEY else "使用真实地图 API",
    }


@app.post("/api/enrich", response_model=EnrichmentResult, tags=["enrich"])
def enrich(req: EnrichRequest):
    enricher: RouteEnricher = app.state.enricher
    return enricher.enrich(
        shops=req.shops, origin=req.origin, modes=req.modes,
        business_area=req.business_area, client_gps=req.client_gps,
        use_ip=req.use_ip, departure_time=req.departure_time,
    )


@app.get("/demo", response_class=HTMLResponse, tags=["ops"], include_in_schema=False)
def demo():
    return HTMLResponse(_DEMO_HTML)


_DEMO_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>交通富化 · 四档方案演示</title>
<style>
body{font-family:system-ui;background:#faf9f7;color:#292524;margin:0;padding:24px;max-width:900px}
h1{font-size:20px} .card{background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:16px;margin:12px 0}
.row{display:grid;grid-template-columns:90px 1fr;gap:8px;padding:6px 0;border-bottom:1px solid #f5f5f4}
.tag{font-weight:600;color:#ea580c} .leg{padding-left:12px;font-size:13px;color:#57534e}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:#888} button{padding:8px 14px;border:0;background:#ea580c;color:#fff;border-radius:8px;cursor:pointer}
</style></head><body>
<h1>交通富化 · 四档方案（post-retrieval enrichment）</h1>
<p class="mono">数据来自 Server 侧；无 key 时为 mock，已在 health 标注。交通不进检索、不进 LLM 生成。</p>
<button onclick="load()">触发富化</button><div id="out"></div>
<script>
async function load(){
  const shops=[{shop_id:'GZ-0137',geo:{lat:23.11,lon:113.34}},{shop_id:'GZ-0200',geo:{lat:23.14,lon:113.30}}];
  const r=await fetch('/api/enrich',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({shops,origin:{lat:23.13,lon:113.32},modes:['driving','cycling','walking','transit']})});
  const d=await r.json();
  document.getElementById('out').innerHTML =
    '<p class="mono">status='+d.status+' · '+d.latency_ms.toFixed(0)+'ms · '+d.message+'</p>'+
    d.route_plans.map(function(p){
      let legs=(p.legs||[]).map(function(l){
        return '<div class="leg">['+l.seq+'] '+(l.kind==='transit'?(l.vehicle+' '+l.line_name+' '+l.geton_station+'→'+l.getoff_station+' '+l.station_count+'站'):(l.instruction||'步行'))+' · '+(l.duration_sec/60).toFixed(0)+'分'+(l.is_transfer_walk?' · 换乘步行':'')+'</div>';
      }).join('');
      var extra=(p.transfer_count!==undefined?' · '+p.transfer_count+'次换乘 · 换乘步行'+(p.transfer_walk_sec/60).toFixed(0)+'分':'')+(p.available?'':' · 【不可用】');
      return '<div class="card"><div class="row"><span class="tag">'+p.shop_id+'</span><span>'+p.mode+' · '+(p.duration_sec/60).toFixed(0)+'分'+extra+'</span></div>'+legs+'</div>';
    }).join('');
}
</script></body></html>"""
