# 07 - FastAPI 服务层：从命令行脚本到常驻服务

> 目标：把前面做好的 RAG 管线包装成一个真正的 HTTP 服务，支持并发、健康检查、流式输出。
> 这是"能演示"到"能上线"的关键一步，也是面试里 RAG 工程化绕不开的话题。

---

## 1. 为什么需要服务层？（先回答三个常见疑问）

### 1.1 "每次请求都要加载十几秒模型吗？是不是我机器性能不行？"

**不是性能问题，是架构问题，而且分两种情况：**

| 场景 | 模型加载次数 | 单次请求耗时 |
|---|---|---|
| 之前的命令行 `python answer.py ...` | 每跑一次命令加载一次（15~38s） | 加载 + 检索 + 生成全算进去 |
| FastAPI 服务 | **进程启动时加载一次**（约 16~21s），之后所有请求复用 | 只剩检索 + 生成 |

你之前看到同一份代码第一次 15s、第二次 38s 的波动，主要是**磁盘冷热缓存**差异（权重文件 4.4GB，冷启动从磁盘读，热启动走 OS page cache）。第一次 reranker 加载 64 it/s、第二次 214 it/s 就是证据。

**但要注意一个容易误解的点**：模型加载变成一次性的了，**每次请求的"推理"仍然要十几秒**（CPU 无 GPU 的代价）。实测单次查询的耗时构成：

```
槽位解析(LLM function calling)  5842ms   ← 调用本地 glm 解析条件
三路召回(BGE-M3 编码 + Qdrant)   899ms
Rerank(cross-encoder CPU)      11033ms   ← CPU 上精排 50 个候选，最大头
LLM 生成                        5079ms
----------------------------------------
单次总计                      ~21s
```

面试可以这么讲：**加载是一次性成本，推理是每次成本**。服务层消除的是前者；后者要靠 GPU、模型量化、减小 rerank 候选数、缓存热点 query 等手段优化，那是另一个话题。

### 1.2 FastAPI 服务层的目的是什么？

一句话：**把"一次性脚本"变成"可被任意前端/系统调用的常驻能力"**，具体五个价值：

1. **资源复用**：4.4GB 模型只加载一次（最核心）。
2. **标准接口**：HTTP + JSON，前端网页、小程序、其他后端都能调，不用碰 Python。
3. **参数校验 + 自动文档**：Pydantic 模型自动校验入参，非法请求返回 422；自动生成 Swagger（`/docs`）。
4. **可观测性**：`/health` 健康检查、trace 明细、耗时统计都标准化。
5. **流式输出**：SSE 逐字返回，打字机体验（CLI 做不了）。

### 1.3 能支持流式输出吗？能，但有个必须讲清的矛盾

**流式 vs 引用校验的固有冲突**：我们的 guard 校验（编号合法、店名对齐、数字溯源）必须拿到**完整答案**才能做；但流式是**边生成边吐**，已经吐出去的字收不回来。

所以本服务提供两种策略，用 `buffer_guard` 开关，让调用方按场景权衡：

| 策略 | `buffer_guard=true`（默认） | `buffer_guard=false` |
|---|---|---|
| 做法 | 后台收齐全文 → guard 校验 → 通过才输出 | 逐字边生成边吐 → 流结束后校验 |
| 首字延迟 | 高（约等于 LLM 全程，6s） | 低（实时） |
| 打字机效果 | 无 | 有 |
| 幻觉风险 | 零（吐出的都校验过） | 用户可能先看到问题文本 |
| 校验失败时 | 直接给模板答案 | 追加 `fallback` 事件（带 `replace=true`），前端覆盖 |
| 适用场景 | 严肃场景、答案准确性优先 | 聊天体验优先、前端能处理覆盖 |

**面试讲点**：这不是"流式做得完不完整"的问题，而是**一致性与体验的取舍（trade-off）**。能在面试里主动指出这个矛盾并给出两种方案，比单纯说"我支持流式"有深度得多。

---

## 2. 代码结构与职责

```
scripts/
├── serve.py                  # uvicorn 启动入口
└── service/
    ├── __init__.py
    ├── schemas.py            # Pydantic 请求/响应模型（含 OpenAPI 文档）
    └── app.py                # FastAPI 应用：lifespan 单例 + 端点 + SSE
```

### 2.1 lifespan：重资源只加载一次

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    state.pipe = RetrievalPipeline()      # 4.4GB 模型，一次性
    state.llm = LLMClient()
    state.generator = AnswerGenerator(client=state.llm)
    state.status = "ok"
    yield
    # 关闭时清理（这里没有需要显式释放的资源）
```

**为什么用 lifespan 而不是模块级全局变量？**
- 模块级初始化发生在 `import` 时，`uvicorn --reload` 或多 worker 下会重复加载模型；
- lifespan 是 FastAPI 官方推荐的应用级资源钩子，启动/清理成对，语义清晰；
- 加载失败直接 fail fast，不会带着半初始化状态接请求。

单例通过 `app.state` 挂载，端点里用 `request.app.state.pipe` 取。

### 2.2 一个关键工程点：CPU 密集调用丢线程池

检索（BGE-M3 编码、rerank、Qdrant 查询）和 LLM 生成都是**同步阻塞**调用。如果直接写在 `async def` 端点里，会**阻塞整个事件循环**，导致所有并发请求排队。正确做法：

```python
hits, trace = await run_in_threadpool(pipe.search, query, topn=topn)
```

这样 CPU 活儿在线程池跑，事件循环保持空闲，能继续接其他请求。

### 2.3 SSE 流式：同步 generator 桥接到 async

LLM 客户端的 `chat_stream()` 是同步阻塞迭代器。要在 async 端点里逐块推送又不阻塞事件循环，用**后台线程生产 + asyncio.Queue 消费**桥接（见 `app.py::_iter_stream`）。

---

## 3. API 端点速查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务信息与端点列表 |
| GET | `/health` | 健康检查：状态、LLM 是否配置、索引点数、运行时长 |
| POST | `/api/search` | 只检索，返回结构化店铺 + trace（不调 LLM 生成） |
| POST | `/api/answer` | 端到端：检索 + LLM 生成 + guard + 降级，一次性返回 |
| POST | `/api/answer/stream` | SSE 流式答案，支持两种 guard 策略 |
| GET | `/docs` | Swagger 自动文档（可直接在页面里调接口） |

请求体示例（`/api/answer`）：

```json
{
  "query": "西华路人均30以下的肠粉",
  "topn": 8,
  "prefer_llm_slots": true,
  "use_llm": true,
  "include_trace": false
}
```

响应示例（精简）：

```json
{
  "query": "西华路人均30以下的肠粉",
  "answer": "为你找到 1 家符合要求的店：合兴庭·西华路肠粉[1] ...",
  "mode": "llm",
  "guard_reason": "pass",
  "llm_ms": 5079.3,
  "total_ms": 21790.3,
  "count": 1,
  "slots": "商圈=西华路 | 人均≤30 | 软偏好=肠粉",
  "shops": [ { "rank": 1, "shop_name": "合兴庭·西华路肠粉", "price_per_person": 23, "...": "..." } ]
}
```

---

## 4. SSE 流式事件协议

`/api/answer/stream` 返回 `text/event-stream`，每条事件格式：

```
event: 事件名
data: JSON

```

前端按 `event` 名分支处理：

| 事件 | data 字段 | 含义 |
|---|---|---|
| `meta` | count, slots | 检索完成，命中数 |
| `token` | text | 正文增量片段（`buffer_guard=false` 才有） |
| `answer` | text, mode, guard_reason | 完整答案（`buffer_guard=true` 或模板） |
| `guard` | ok, reason | 校验结果 |
| `fallback` | text, replace=true, reason | 校验失败的模板答案，**前端应覆盖已显示文本** |
| `clarify` | question | 条件冲突需追问 |
| `done` | total_ms, count | 结束 |
| `error` | type, message | 异常终止 |

前端消费示例（fetch + ReadableStream，因为 POST 不能用 EventSource）：

```javascript
const resp = await fetch('/api/answer/stream', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({query: '西华路人均30以下的肠粉', buffer_guard: false})
});
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = '', text = '';
while (true) {
  const {value, done} = await reader.read();
  if (done) break;
  buf += decoder.decode(value, {stream: true});
  const frames = buf.split('\n\n');
  buf = frames.pop();
  for (const f of frames) {
    const ev = /event: (.+)/.exec(f)?.[1];
    const data = JSON.parse(/data: (.+)/.exec(f)?.[1] || '{}');
    if (ev === 'token') { text += data.text; render(text); }
    if (ev === 'fallback' && data.replace) { text = data.text; render(text); } // 覆盖
  }
}
```

---

## 5. 启动与测试

### 5.1 启动

在 `scripts/` 目录下（PowerShell）：

```powershell
$env:RAG_LLM_API_KEY="sk-wb-local-8600"
$env:RAG_LLM_BASE_URL="http://127.0.0.1:8600/v1"
$env:RAG_LLM_MODEL="glm-5.3-flash"
python serve.py --port 8000
```

首次启动加载模型约 16~21s，期间 `/health` 返回 `loading`。加载完成后访问 `http://127.0.0.1:8000/docs` 看 Swagger 文档。

### 5.2 curl 测试（本机代理会拦截，务必带 `--noproxy '*'`）

```bash
# 健康检查
curl --noproxy '*' -s http://127.0.0.1:8000/health

# 只检索
curl --noproxy '*' -s -X POST http://127.0.0.1:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"西华路人均30以下的肠粉","topn":3,"include_trace":true}'

# 端到端
curl --noproxy '*' -s -X POST http://127.0.0.1:8000/api/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"西华路人均30以下的肠粉","topn":3}'

# 流式（安全策略）
curl --noproxy '*' -s -N -X POST http://127.0.0.1:8000/api/answer/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"西华路人均30以下的肠粉","buffer_guard":true}'

# 流式（打字机策略）
curl --noproxy '*' -s -N -X POST http://127.0.0.1:8000/api/answer/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"西华路人均30以下的肠粉","buffer_guard":false}'
```

### 5.3 本机实测结果（记录）

```
/health          → status=ok, collection_points=2088, llm_configured=true
/api/search      → count=1, trace timings: slots 5842ms / recall 899ms / rerank 11033ms
/api/answer      → 一次 LLM 引用错位被 guard 拦截，自动降级 template（防护链生效）
/stream buffer=1 → mode=llm, guard pass, 一次性输出完整答案
/stream buffer=0 → 167 个 token 块逐字推送，guard pass，无 fallback
```

---

## 6. 已知边界与生产化建议（面试加分）

| 问题 | 现状 | 生产建议 |
|---|---|---|
| Qdrant embedded | 单进程独占，服务和第二进程不能同时打开索引 | 换 Qdrant server（HTTP），支持多实例 |
| 多 worker | 单 worker（模型内存大） | 模型服务独立部署，API 层水平扩容 |
| 每次推理慢 | CPU 无 GPU，rerank 11s | GPU / 量化 / 减小 RRF_TOPN / 热点 query 缓存 |
| 共享实例改开关 | `prefer_llm` 临时改再还原 | 用 per-request 参数透传，避免并发竞争 |
| CORS 放行全部 | 学习方便 | 生产收紧 `allow_origins` |
| 无鉴权 | 本地学习 | 加 API Key / OAuth |

---

## 7. 面试追问（本主题）

1. lifespan 是什么？为什么不用全局变量在 import 时加载模型？
2. 为什么 CPU 密集调用要 `run_in_threadpool`？直接写在 async 端点里会怎样？
3. SSE 和 WebSocket 的区别？为什么这个场景选 SSE？
4. 流式输出和 guard 校验的矛盾是什么？你怎么解决？（答：buffer_guard 两种策略 + fallback 覆盖）
5. `/health` 为什么要返回索引点数？只看 status=ok 不够吗？（答：服务活着但索引空了的情况）
6. 单例的 pipeline 在并发请求下安全吗？`prefer_llm` 临时改开关有什么风险？
7. 模型加载 16s 和单次请求 21s 分别是什么成本？怎么分别优化？
8. 为什么 Qdrant embedded 不能多进程访问？生产怎么解决？
