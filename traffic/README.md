# 实时交通富化与 MCP 集成（Phase 5.5）

> 对应设计文档 `docs/02-实时交通富化与MCP集成.md` v1.1。
> 本目录**独立开发、零干扰主链路**：不修改 `scripts/` 与 `front/` 的任何代码，独立端口 `8100`。

## 一句话

RAG 返回 Top-K 店铺后，用 MCP 工具并行获取四档交通方案，作为**平行字段** `route_plans` 合并返回——
**不进检索、不进 LLM 事实生成、不改 Top-K 任何集合与顺序**。

## 架构红线（不可绕过）

1. **实时数据只做 post-retrieval enrichment**：不进向量库 / payload filter / RRF / Rerank，不写回索引。
2. **地图服务合规白名单**：仅腾讯 / 高德 / 百度 / 天地图；**Mapbox / Google / Apple / OSM 禁用**（`secrets.assert_compliant_provider` 强制）。
3. **坐标系统一 GCJ-02**：客户端 WGS-84 必须转换；城市级 IP 定位不用于步行/骑行/公交。
4. **密钥只从环境变量读**：不内嵌、不进日志/LLM/前端；无 key 自动降级 mock 并标注 `source=mock`，**绝不伪装真实数据**。
5. **任何富化失败都不影响 answer / shops**（铁律，单测证明）。

## 输出粒度分级（v1.1）

| mode | 输出 | legs |
|---|---|---|
| driving / cycling / walking | `duration_sec`（参考耗时）+ 内部 `distance_m` | ❌ 无 legs / polyline |
| transit | `duration_sec` + `legs[]` 换乘明细 + 汇总量 | ✅ 线路/上下车站/站数/换乘步行/票价/首末班 |

公交多方案默认取**用时最少**一条：`routes[]` 二次按 duration 升序，平局按 transfer_count → walk_total_m 打破，`running_status=303`（停运）剔除。
单位在 Provider 适配层归一化：分钟→秒(×60)、分→元(÷100)。

## 目录结构

```
traffic/
├── config.py           合规白名单 / 供应商 / 超时预算 / 缓存 / 端口(8100)
├── schema.py           RoutePlan / TransitLeg / Location / EnrichmentResult 契约
├── secrets.py          仅环境变量读 key、日志脱敏、白名单校验
├── normalize.py        单位归一化 / 最短路+平局 / legs解析 / 换乘步行 / 粒度分级 ← 技术核心
├── location.py         优先级链 + WGS84→GCJ02 + 商圈中心点兜底
├── cache.py            网格化 key + 5min 时间桶 + LRU + TTL
├── guard.py            参数白名单 / Origin 校验 / 坐标系 / 口径一致性 / 降级判定
├── enricher.py         并发4档 / 超时 / 缓存 / 降级 / merge_into_answer ← 编排核心
├── mcp_client.py       MCP Client：InProcess / Streamable HTTP / stdio 三实现
├── providers/
│   ├── base.py         MapProvider 抽象 + make_provider（无 key 降级 mock）
│   ├── mock.py         确定性模拟数据（结构同腾讯，供无 key 跑通与单测）
│   └── tencent.py      真实 WebService 适配（key 从 env 注入）
├── mcp_server/
│   ├── tools.py        5 个工具：resolve_location/route_plans/route_matrix/poi_validate/health
│   └── server.py       JSON-RPC 2.0 + Streamable HTTP(/mcp) + stdio 双传输
├── app.py              独立 FastAPI（/api/enrich、/mcp、/health、/demo）
├── serve.py            启动入口
└── tests/              36 个单测（unittest）
```

## 快速开始

```bash
# 无 key：自动用 mock 数据（响应标注 real_data=false）
python -m traffic.serve                    # 监听 127.0.0.1:8100
# 打开演示页
#   http://127.0.0.1:8100/demo

# 有真实 key：申请见下，注入环境变量后自动切真实 API
TENCENT_MAP_KEY=<你的key> python -m traffic.serve

# 单测
python -m unittest discover -s traffic/tests -v
```

### 申请腾讯位置服务 key（合规流程）

1. `lbs.qq.com` → 控制台 → 创建应用；
2. 开通 WebService API：路径规划 / 距离矩阵 / 定位 / 地理编码；
3. 配置域名或 IP 白名单，查看 QPS 与日配额；
4. key 写入本机环境变量 `TENCENT_MAP_KEY`，**不要写进任何文件或仓库**。

## API

### `POST /api/enrich`（富化主接口）

```jsonc
// 请求
{
  "shops": [{"shop_id": "GZ-0137", "geo": {"lat": 23.11, "lon": 113.34}}],  // RAG Top-K
  "origin": {"lat": 23.13, "lon": 113.32},     // 优先级最高；也可用 business_area / client_gps
  "modes": ["driving", "cycling", "walking", "transit"]
}
// 响应（EnrichmentResult）
{
  "route_plans": [
    {"shop_id": "GZ-0137", "mode": "driving", "duration_sec": 2460, "source": "mock", ...},
    {"shop_id": "GZ-0137", "mode": "transit", "duration_sec": 2220,
     "legs": [{"seq":1,"kind":"walk",...}, {"seq":2,"kind":"transit","line_name":"公交55路",...}],
     "transfer_count": 0, "transfer_walk_sec": 0, ...}
  ],
  "origin": {...}, "status": "ok", "latency_ms": 4.1, "cache_hits": 0
}
```

`status`：`ok / partial / unavailable / need_location / invalid_coord / fallback_static`。

### `POST /mcp`（MCP Streamable HTTP）

标准 JSON-RPC 2.0：`initialize`（能力协商）、`tools/list`、`tools/call`、`ping`。
带 `Mcp-Method` / `Mcp-Name` 响应头；校验 `Origin` 防 DNS rebinding（非法来源 403）；只绑 `127.0.0.1`。

```bash
curl -s http://127.0.0.1:8100/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 接入主链路（✅ 已接入，v2）

本模块已**真正接入**主服务：`scripts/service/app.py` 在检索后、LLM 前调用 `traffic.integration`，交通块注入 prompt；`AnswerResponse.traffic` 与 SSE `meta.traffic` 携带结构化视图；前端 `TrafficCard.vue` 渲染卡片。默认 `enable_traffic=False`，不勾选时零开销、行为与旧版完全一致。

**完整逐步说明（key/白名单/位置/精确改动清单）见 `docs/13-腾讯MapKey配置与RAG接入指南.md` §6.6。**

接入等效代码（主服务已实现，此处是骨架说明）：

```python
from traffic.integration import TRAFFIC_RULES, enrich_for_hits, traffic_prompt_block

# 检索后、LLM 前（异常静默降级，失败返回空串）
enrichment = enrich_for_hits(hits, origin=..., business_area=...)
block = traffic_prompt_block(enrichment, hits) if enrichment else ""
system_prompt = SYSTEM_PROMPT + (TRAFFIC_RULES if block else "")
user_prompt = build_user_prompt(actual_query, slots_summary, hits) + block
```

关键点：
- 交通不参与召回/排序，`hits` 与 `shops` 原值不变 → **RAG 指标开/关富化完全一致**（已实测规则槽位下两次 shops 列表 diff 为空）。
- 交通数字走独立规则（TRAFFIC_RULES 第 6~10 条），不进 `validate()` 的引用校验口径；mock 必须显式标注。
- 前端勾选「交通」+ 填商圈或定位即可触发；`TrafficCard` 展示四档耗时 + 公交换乘明细 + 数据来源徽章。

**配 key**（让交通走真实 API）：`cp traffic/.env.local.example traffic/.env.local` 填入真实 key，重启 8000；`start_backend.sh` 会自动 source 该文件。自检 key：`python -m traffic.check_key`。

## 降级矩阵

| 场景 | status | 行为 |
|---|---|---|
| 全部成功 | ok | 四档齐全 |
| 公交超时/无方案 | partial | 其余三档照常，公交「暂无」 |
| 只返回总耗时、steps 缺失 | partial | `legs=[]`，`degraded_reason=no_itinerary` |
| running_status=303 | partial | 剔除该候选取下一名 |
| 地图全挂/配额耗尽 | unavailable | **answer/shops 100% 正常** |
| 只有城市级定位 | need_location | 不用于步行/公交，追问精确起点 |
| 坐标系不一致 | invalid_coord | 拦截，不发起调用 |
| 无 key | （照常） | 用 mock，标注 source=mock，不伪装真实 |

## 测试覆盖（36 tests，全绿）

- 单位归一化正确率 100%（分钟→秒、分→元、缺票价 -1）；
- 最短路选择正确率 100%（含平局按换乘数/步行量打破、303 剔除、确定性）；
- 粒度分级：非 transit 档响应无 legs / polyline；
- 换乘步行计算（夹在两 transit 间的 walk 段）、lines[0] 取值、alternative_lines；
- 降级正确率 100%（地图全挂时 merge 后 answer/shops 原样）；
- RAG 不变性（开/关富化输入 shops 不被修改）；
- WGS84→GCJ02、优先级链、合规白名单（Mapbox 拒绝）、Origin 校验、key 脱敏。

## 面试讲点索引

docs/02 §12 的 31~44 问全部可讲，重点：
- **31/32** 实时数据不进索引 / enrichment vs reranking 的区别；
- **33/34** FC 与 MCP 分层 / 为何用确定性编排（触发/参数/顺序固定，可单测）；
- **35/36/37** tools vs resources vs prompts / Streamable HTTP 取代 SSE / Origin + 127.0.0.1 防 DNS rebinding；
- **41/42/43/44** 输出粒度分级 / routes[] vs steps[].lines[] 两层"多条" / 二次排序与平局 / 单位归一化与 301/303。

亮点话术：「我用 MCP 但**不让 LLM 决定调不调工具**——这场景触发条件固定、参数固定，交给 LLM 只增加不确定性和失败模式；我用确定性编排，MCP 作为工具供给与解耦层。这比堆 Agentic tool choice 更稳。」
