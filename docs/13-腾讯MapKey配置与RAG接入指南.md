# 13 · 腾讯 Map Key 配置与 RAG 接入指南

> 面向已申请 key、但不确定「申请得对不对」以及「怎么接进现有 RAG」的场景。
> 配套代码：`traffic/check_key.py`（自检）、`traffic/integration.py`（接入辅助）。
> 前置：`docs/02-实时交通富化与MCP集成.md`、`traffic/README.md`。

---

## 0. 先纠正一个措辞造成的困惑

`docs/02` 里写过「开通 **路径规划 / 距离矩阵 / 定位**」，容易被读成要在控制台打开三个独立开关。
**实际不是。** 这三组能力都是腾讯位置服务 **WebService API 这同一个产品**下面的接口族：

| 我们代码用到的接口 | 官方文档归类 | 控制台需要做什么 |
|---|---|---|
| `/ws/direction/v1/driving|bicycling|walking|transit` | WebService API → 路线服务 → 驾车/骑行/步行/公交路线规划 | 勾选 **WebServiceAPI** 即可 |
| `/ws/distance/v1/matrix` | WebService API → 路线服务 → 距离矩阵 | 同上 |
| `/ws/location/v1/ip` | WebService API → 定位 → IP 定位 | 同上 |
| `/ws/geocoder/v1/` | WebService API → 地址服务 → 地址解析 | 同上 |

**结论：你只创建了应用 + 创建了 key + 勾选了 WebService API，方向就是对的。**
唯一的例外：少数**增值接口**返回 `status=113 此功能未被授权` 时，才需要去「配额管理」单独申请。基础四档路线规划、距离矩阵、IP 定位通常在勾选 WebServiceAPI 后即可用。

---

## 1. 控制台逐步操作（照着点即可）

入口：<https://lbs.qq.com/dev/console/application/mine>

1. 左侧 **应用管理 → 我的应用** → **创建应用**
   - 应用名称：如 `gz-food-rag`
   - 应用类型：选「出行」或「工具」均可（只影响归类/配额模板，不影响接口权限）
2. 应用列表中找到刚建的应用 → **添加 Key**
   - Key 名称：如 `rag-traffic-backend`
   - **启用产品**：勾选 **WebServiceAPI** ✅（这是关键一步，你已做）
   - 域名白名单：**服务端调用不用填**（那是浏览器 JSAPI 用的；如果只做后端富化，留空即可）
   - 授权 IP / IP 白名单：**先留空**跑通，跑通后再按第 3 节收紧
   - 勾选服务协议 → **添加** → 复制 Key 字符串
3. 左侧 **配额管理**：看这个 key 的每日额度 / QPS，确认够用（本项目 8 个请求/问题，QPS 很低）。

> ⚠️ Key 形如 `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`。复制时不要带空格/换行。

---

## 2. 自检：确认你申请得对不对（30 秒）

不用去猜。项目里已有自检脚本，它会**实测**五类接口并把错误码翻译成"下一步做什么"：

```bash
cd gz-food-rag
export TENCENT_MAP_KEY='你的key'          # PowerShell: $env:TENCENT_MAP_KEY='你的key'
D:/向量模型下载与部署/.venv/Scripts/python.exe -m traffic.check_key
```

输出示例（全对时）：

```
[OK ] IP 定位    status=0  OK
         -> OK；本机出口公网 IP = 113.xxx.xxx.xxx（广东省广州市）
[OK ] 驾车规划    status=0  OK
[OK ] 公交规划    status=0  OK
[OK ] 距离矩阵    status=0  OK
[OK ] 地址解析    status=0  OK
```

诊断对照（常见）：

| status | 含义 | 你要做的事 |
|---|---|---|
| `0` | 成功 | 无需操作 |
| `110` / `112` | 请求来源/IP 未被授权 | 把脚本打印的出口公网 IP 填进 Key 的授权 IP |
| `113` | 此功能未被授权 | 控制台「配额管理」→ 找到对应接口 → 申请开通 |
| `190` / `199` | key 无效 / 未开启 WebService | 回 Key 编辑页确认未停用、已勾选 WebServiceAPI |
| `311` | key 格式错误 | 检查复制是否完整、有无空格 |
| `120` | QPS 超限 | 等 1-2 秒重试 |
| `121` | **日调用量达上限。新 Key 极常见的原因：该接口默认日配额为 0，并不是你真的调完了**（自检每个接口只调 1 次） | 控制台 → 配额管理 → 账户额度 / 申请配额，给该接口分配日调用次数 |

脚本刻意 `trust_env=False` **直连**腾讯 API（绕过本机代理）——这一点很重要，否则拿到的是代理出口 IP，填进白名单会不匹配。

### 2.1 为什么只有部分接口报 121（新 Key 高频现象）

腾讯的配额是**按 Key × 接口**分别计量的，新 Key 各接口的默认额度并不相同：IP 定位、驾车规划、地址解析通常有基础额度，而**公交路线规划、距离矩阵**等接口的默认额度可能是 **0**。自检每个接口只发 1 次请求，不可能真把日额度用完——所以"3 个 OK + 2 个 121"几乎可以确定是**默认额度为 0**，而不是被盗用或计数异常。

佐证：若真是白名单问题，全部接口都会返回 `110/112`；若是 key 无效，全部返回 `190/199/311`。部分成功、部分 121，只能是接口级配额。

**解决步骤**：

1. 打开 <https://lbs.qq.com/dev/console/quota/mine>（控制台 → **配额管理**）。
2. 找到「公交路线规划（transit）」和「距离矩阵（matrix）」两行，查看其日额度/分配额度。
3. 在「**账户额度**」里把总配额分配给这两个接口（个人账号免费额度通常够学习项目用）；若接口行显示未开通/额度为 0 且无法分配，走「**申请配额**」提交开通（<https://lbs.qq.com/dev/console/quota/applyList>）。
4. 官方说明：企业认证后账户配额默认提升，也可在配额管理里自行分配；详见 <https://lbs.qq.com/faq/accountQuota/faqKey>。
5. 回到终端重跑 `python -m traffic.check_key`，五项全 OK 即可。

**顺带提醒额度消耗**：当前 `TencentProvider.route_plans()` 是逐店逐 mode 调用的，Top-K=8 时一次提问最多 **8×4=32 次**请求（公交/矩阵各 8 次）。若分配的日额度较小，调试几轮就会见底。缓解手段见 §6.5。

---

## 3. Key 如何设置（Windows，两种场景）

`traffic/config.py` 在**模块 import 时**读取 `TENCENT_MAP_KEY`。因此必须在**启动进程之前**设置好环境变量。

### 临时（本次调试）

Git Bash：

```bash
export TENCENT_MAP_KEY='你的key'
export TRAFFIC_DEFAULT_AREA='体育西路'   # 可选：没有前端传坐标时的商圈兜底
cd traffic && PY="D:/向量模型下载与部署/.venv/Scripts/python.exe" && "$PY" -m traffic.serve
```

PowerShell：

```powershell
$env:TENCENT_MAP_KEY='你的key'
$env:TRAFFIC_DEFAULT_AREA='体育西路'
& "D:/向量模型下载与部署/.venv/Scripts/python.exe" -m traffic.serve
```

### 持久化（避免每次重开终端都要 export）

新建一个本地脚本（**不要提交 Git**），例如 `traffic/start_traffic.sh`：

```bash
#!/usr/bin/env bash
export TENCENT_MAP_KEY='你的key'
export TRAFFIC_DEFAULT_AREA='体育西路'
cd "$(dirname "$0")/.."
exec "D:/向量模型下载与部署/.venv/Scripts/python.exe" -m traffic.serve
```

已在 `.gitignore` 中加入 `traffic/start_traffic.sh` / `traffic/.env.local`。启动后访问 <http://127.0.0.1:8100/health>，
看到 `"real_data": true`、`"map_provider": "tencent"`、`"note": "使用真实地图 API"`，就说明真 key 已生效；否则仍是 mock（脱敏 key 只在启动日志里打一次）。

---

## 4. IP 白名单怎么配

1. 先跑 `python -m traffic.check_key`，输出最后一行就是 **本机出口公网 IP**（形如 `113.x.x.x`）。
   它取自腾讯 IP 定位接口的 `result.ip`，即腾讯服务器实际看到的你的出口地址。
2. 控制台 → **我的应用** → 点开应用 → Key 右侧 **编辑** → 在 **WebServiceAPI** 一栏的 **授权 IP** 中填入该 IP → 保存。
3. 重跑自检脚本，五项全 `OK` 即配好。

常见坑：

- ❌ 填 `127.0.0.1` / `localhost`：腾讯看到的是公网出口 IP，不是本机回环地址。
- ❌ 填内网 IP（`192.168.x.x`）：同理，运营商 NAT 后不可见。
- ❌ 开着代理去查 IP：本模块直连不走系统代理，填代理出口会不匹配。
- ✅ 家庭宽带 IP 可能变动；变更后重跑自检脚本拿新 IP 更新白名单。
- ✅ 调试期留空白名单（不限制）也是可行的，跑通后再收紧。

---

## 5. 当前位置 / 目标位置怎么拿

### 目标位置：不用"获取"，检索结果里已经有

每家店的 `FinalHit.payload["geo"]` = `{lat, lon}`，是 **GCJ-02**（火星坐标），与目标坐标口径一致。
`traffic/integration.py` 的 `enrich_for_hits(hits, ...)` 会自动从 hits 提取，你不用管。

### 当前位置：优先级链（已实现于 `traffic/location.py`）

```
显式 origin  >  会话内存  >  客户端 GPS(WGS-84→GCJ-02)  >  商圈中心点  >  IP 定位(城市级)  >  追问用户
```

| 方案 | 怎么拿到 | 精度 | 适用 |
|---|---|---|---|
| **A. 商圈/手动选点（推荐先跑）** | 前端给一个下拉（体育西路/珠江新城/北京路/…），后端传 `business_area`；`BUSINESS_AREA_CENTERS` 已内置 7 个中心 | 街区级 | 零授权、零前端改造 |
| **B. 浏览器定位（真·当前位置）** | `navigator.geolocation.getCurrentPosition()` → 后端 `client_gps` → 自动 `wgs84_to_gcj02()` | 米级 | HTTPS 或 localhost + 用户点"允许" |
| **C. IP 定位兜底** | `use_ip=True`（或不传起点时调用腾讯 `/ws/location/v1/ip`） | **城市级** | 仅粗估；guard 会拦截步行/骑行/公交，只留驾车参考 |

前端 B 方案最小代码（放进现有 Vue 页面即可）：

```js
navigator.geolocation.getCurrentPosition(
  pos => sendAnswer({ client_gps: { lat: pos.coords.latitude, lon: pos.coords.longitude } }),
  err => sendAnswer({ business_area: '体育西路' }),   // 用户拒绝授权 → 商圈兜底
  { enableHighAccuracy: true, timeout: 5000 }
);
```

> 注意：WGS-84 直接当 GCJ-02 用会偏移数百米，`location.py` 已做转换，不要绕过它自己传 `origin`。

---

## 6. 接入 RAG：检索后、LLM 前组织回答（核心）

### 6.1 位置与红线

```
用户 query
  → [现有] query 改写 → 槽位 → 三路召回 → RRF → rerank → 店铺级去重 → hits
  → 【新增起点】并行旁路：enrich_for_hits(hits)  ← 2.5s 总预算，失败返回 None
  → [现有] build_user_prompt(query, slots, hits) + traffic_prompt_block(enrichment, hits)
  → [现有] SYSTEM_PROMPT + TRAFFIC_RULES → LLM → guard → SSE
```

三条不变式（面试重点）：

1. **不进检索**：交通不参与召回/RRF/rerank，`hits` 顺序和内容不变 → Recall/NDCG 不变。
2. **不改店铺字段**：交通是平行字段（`route_plans`），不写回 `shops`。
3. **不污染 guard**：数字照抄校验只针对餐厅资料；交通块用独立规则（TRAFFIC_RULES 第 6~10 条）约束。

### 6.2 精确 patch（改 `scripts/service/app.py` 3 处）

**① 顶部 import**（跟其他 import 放一起）：

```python
from traffic.integration import (  # noqa: E402
    TRAFFIC_RULES, enrich_for_hits, traffic_prompt_block,
)
```

**② `stream_answer()` 的 `gen()` 里，`hits` 拿到之后、`user_prompt` 构造之前**（约 486 行）：

```python
            # ===== 交通富化旁路（失败不影响 RAG）=====
            enrichment = await run_in_threadpool(
                enrich_for_hits, hits,
                origin=req.origin if hasattr(req, "origin") else None,
                client_gps=req.client_gps if hasattr(req, "client_gps") else None,
                business_area=req.business_area if hasattr(req, "business_area") else None,
            )
            if enrichment:
                yield _sse("enrichment", {
                    "status": enrichment.status,
                    "message": enrichment.message,
                    "latency_ms": round(enrichment.latency_ms, 1),
                    "cache_hits": enrichment.cache_hits,
                    "real_data": (not any(str(p.get("source", "")).startswith("mock")
                                          for p in enrichment.route_plans)),
                })
```

要点：用 `run_in_threadpool` 而不是同步调用，避免阻塞 SSE 的事件循环。

**③ 第 534 行构造 prompt 时拼接**：

```python
            system_prompt = SYSTEM_PROMPT + TRAFFIC_RULES          # 新增
            user_prompt = build_user_prompt(actual_query, slots_summary, hits)
            if enrichment:                                          # 新增
                user_prompt += traffic_prompt_block(enrichment, hits)
```

然后把该文件后续所有 `SYSTEM_PROMPT` 的引用（第 538、559、599、606 行的 `_collect_stream` / `_iter_stream` 调用）改成 `system_prompt`。

**可选 ④**：在 SSE `meta` 事件里加 `"route_plans": enrichment.route_plans if enrichment else []`，
前端就能像店铺卡片一样渲染四档交通方案；`merge_into_answer()` 提供了平行合并的参考实现。

### 6.3 前端要改什么

最小改动：**什么都不改**——交通信息已经在 LLM 答案文本里（带 `[编号]`），直接显示即可。
想要结构化卡片，则在 SSE `enrichment` / `meta` 事件里加 `route_plans`，按 `shop_id` 归并渲染（可复用 `traffic/app.py` 内 demo 页的卡片逻辑）。

### 6.4 怎么验证接得对

1. `python -m traffic.check_key` 全 OK。
2. 重启 8100，`/health` 显示 `real_data=true`。
3. 重启 8000（带 `TENCENT_MAP_KEY`），发一条「体育西路附近人均 50 的店」：
   - 答案里出现交通描述且标注数据来源；
   - `shops` 与不开富化时**完全一致**（可用 `/api/search` 对比）；
   - `eval/run.py` 重跑消融，Recall/Faithfulness 与基线一致（交通不进检索）。

### 6.5 省额度模式（新 Key 日额度小的时候开）

`TencentProvider.route_plans()` 目前是**逐店逐 mode** 调用：Top-K=8 时一次提问最多 **32 次**请求。额度分配得少时，做几轮调试就会再次触发 121。三种缓解手段：

1. **限制富化店铺数（已实现）**：启动服务前设
   ```bash
   export TRAFFIC_MAX_SHOPS=3     # PowerShell: $env:TRAFFIC_MAX_SHOPS='3'
   ```
   `integration.enrich_for_hits()` 只取前 3 家，请求量降到 **12 次/问**；未富化的店在答案里显示"暂无交通数据"，不影响 RAG 答案本身。默认 8（与 Top-K 一致）。
2. **驾车/步行改距离矩阵（后续优化）**：腾讯 `/ws/distance/v1/matrix` 支持 `from=起点&to=多个终点`，1 次请求可替代 N 次逐店请求；公交不支持批量，骑行矩阵官方不提供。当前 `route_matrix` 工具已预留该入口，可在 provider 层切换。
3. **缓存**：已做起点 100m 网格化 + 5 分钟时间桶，同一区域连续提问会命中缓存，`cache_hits` 可在响应里看到。

> 建议：先把 `TRAFFIC_MAX_SHOPS=3` 配上，配额分配后观察几次真实问答的消耗，再决定是否放开到 8。

---

## 6.6 业务接入已完成（v2，本轮落地）

上一版只给了 patch 示意，本轮已**真正接入主链路并通过端到端验证**。

### 改了哪些文件（精确清单）

| 文件 | 改动 |
|---|---|
| `scripts/service/schemas.py` | `AnswerRequest` 新增 `enable_traffic/traffic_origin/client_gps/traffic_area/use_traffic_ip`；新增 `TrafficOut/TrafficView/TrafficOrigin`；`AnswerResponse.traffic`、`HealthResponse.traffic_configured` |
| `scripts/service/app.py` | 新增 `_enrich_traffic()`（检索后、LLM 前的旁路，异常静默降级）；`answer()` / `stream_answer()` 在生成前调用它，把交通块拼进 system/user prompt；SSE `meta` 事件带 `traffic`；落库 meta 带 traffic（刷新回显） |
| `scripts/generate/answer.py` | `generate()` 加 `system_extra/user_extra` 两个**默认空串**的可选参数，向后兼容；交通块只进 prompt，不进 `validate()` 的引用校验口径 |
| `front/src/App.vue` | 设置按会话隔离新增交通开关/商圈/IP；`common` body 带交通参数；`aMsg.traffic` 在 SSE meta / 落库回显中保留；新增定位按钮（`navigator.geolocation`，WGS-84） |
| `front/src/components/TrafficCard.vue` | 新增交通卡片：四档耗时、公交换乘 legs、票价、数据来源徽章 |
| `front/src/styles.css` | 交通选项区与卡片样式 |
| `scripts/start_backend.sh` | 启动时若未 export key，则 source `traffic/.env.local`（已 gitignore） |
| `traffic/.env.local.example` | key 配置模板（无真实值，可提交） |

### 用户使用方式

1. **配 key**（一次性）：`cp traffic/.env.local.example traffic/.env.local`，填入真实 key；可选 `TRAFFIC_MAX_SHOPS=3` 省额度、`TRAFFIC_DEFAULT_AREA=体育西路` 设默认起点。重启 8000 即自动生效（`/health` 的 `traffic_configured=true` 表示真实 API 已启用）。
2. **前端发问**：输入区勾选「交通」，二选一填出发商圈或点「用我的位置」定位；IP 粗定位仅给城市级结果（精细 mode 会被 guard 拦截）。不勾选则**零开销、行为与旧版完全一致**。
3. **接口直连**：`/api/answer` 或 `/api/answer/stream` 带 `"enable_traffic":true,"traffic_area":"体育西路"`；响应多出 `traffic` 字段，SSE `meta` 事件多出 `traffic`。

### 关键不变量（已实测）

- **RAG 不受污染**：用规则槽位（确定性）跑两次，开/关交通的 `shops` 店铺列表**完全一致**（`diff` 为空）；`_enrich_traffic` 只读 hits、不改 hits/shops、不进检索打分。
- **地图挂了不影响答案**：`enrich_for_hits()` 内部 try/except 吞掉所有异常返回 None，`_enrich_traffic` 同样双层保护；交通为 None 时 prompt 与旧版逐字一致。
- **数据来源不伪装**：无 key 时 `real_data=false`、卡片显示"模拟数据"徽章 + prompt 里 `【模拟数据】` 明示；有 key 时自动切真实 API、徽章变"真实路况"。
- **刷新回显**：交通视图随 meta 落库，`backendToMessages/syncFromBackend` 保留，`刷新后卡片不丢`。

### 配额消耗估算

按「每店 4 种 mode = 4 次请求」计，`TRAFFIC_MAX_SHOPS=8` 时一次提问最多 32 次（其中公交走公交规划接口、驾车/步行/骑行各走对应接口）；设 `TRAFFIC_MAX_SHOPS=3` 降到 12 次/问。起点 100m 网格 + 5 分钟时间桶缓存，同区域连续提问命中缓存不重复计费。

---

## 7. 面试讲点速查

- **为什么不把交通塞进检索条件？** 时效性数据与静态 POI 口径不同，会让 Recall 不可复现；post-retrieval enrichment 保证离线评估可重放。
- **为什么 prompt 注入而不是让 LLM 自己调工具？** 减少一次 LLM 往返、数字不可被模型改写；工具调用留给需要多轮规划的场景。
- **guard 怎么处理两类数字？** 餐厅资料走"照抄"校验；交通块走独立规则，并要求 mock 显式标注，避免混淆两个数据源。
- **key 怎么不泄漏？** 只从环境变量读、启动日志脱敏、不进 LLM 上下文、白名单收紧到出口 IP。
