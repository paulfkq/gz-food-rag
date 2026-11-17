# 广州美食 RAG · 演示前端

Vue 3 + Vite 单页应用，对接本地 FastAPI 服务（8000 端口），**支持多轮对话**，用于演示和面试展示。

## 一、启动（两个终端）

**终端 1 — 后端 FastAPI：**

```powershell
cd gz-food-rag/scripts
$env:RAG_LLM_API_KEY="sk-wb-local-8600"
$env:RAG_LLM_BASE_URL="http://127.0.0.1:8600/v1"
$env:RAG_LLM_MODEL="glm-5.3-flash"
D:\向量模型下载与部署\.venv\Scripts\python.exe serve.py --port 8000
```

首次启动加载 4.4GB 模型约 16~21s，看到 `[startup] 就绪` 即可。会话数据存在 `data/sessions.db`（SQLite，自动创建）。

**终端 2 — 前端 Vite：**

```powershell
cd gz-food-rag/front
npm install          # 首次运行需要
npm run dev
```

看到 `VITE v6 ready` 后，浏览器打开 **http://127.0.0.1:5173/**。

> 端口说明：默认 8000。`vite.config.js` 里的 proxy 指向 8000，改端口时两端要一起改。
> 若启动时报 `.lock` 或 `already accessed by another instance` 错误，说明还有别的进程占用索引——
> 先停掉那个进程（Qdrant embedded 模式不允许两个进程同时打开索引），不要在进程还活着时删锁文件。

## 二、界面功能

**多轮对话**：
- 左侧会话栏：新建会话 / 切换 / 删除，标题取首轮问题，当前会话高亮，`localStorage` 记住上次会话
- 右侧消息流：用户气泡（右）+ 助手消息（左），历史持久化到 SQLite，服务重启后仍在
- **query 改写可视化**：追问时消息上会标注 `🔄 理解为：西华路人均20以下的肠粉`，让用户看懂系统如何结合上下文理解指代

**三种模式**（顶部切换）：
- 💬 流式问答：打字机效果，两种 guard 策略可切（先校验后输出 / 边吐边校验）
- ✨ 完整问答：一次返回完整答案
- 🔎 纯检索：只要结构化店铺，不调 LLM

**其他**：示例问题一键填充（含"那便宜点的呢"这类追问，方便演示多轮）、参数区（返回数 / LLM 槽位 / LLM 生成 / 流式策略 / Trace）、每条助手消息内可展开店铺卡片和 Trace、后端状态徽章。

### 多轮演示建议流程

```
轮1：西华路人均30以下的肠粉
轮2：那便宜点的呢          → 系统改写为"西华路人均20以下的肠粉"
轮3：那有没有安静点的      → 改写为"西华路人均15以下安静点的肠粉"
```

对比第 1、2 轮的店铺和价格，就能直观看到指代消解和上下文继承的效果。

## 三、技术要点（面试可讲）

| 点 | 实现 |
|---|---|
| 前后端联调 | Vite dev proxy 把 `/api`、`/health` 转发到后端，规避浏览器 CORS |
| SSE 流式解析 | EventSource 不支持 POST，改用 `fetch` + `ReadableStream` 手动按 `\n\n` 分帧解析 |
| 半帧处理 | buffer 累积，只解析完整帧，剩余落回 buffer |
| fallback 覆盖 | `buffer_guard=false` 时校验失败推 fallback 事件，前端替换已显示文本并提示 |
| 首屏体验 | 后端在 SSE 的 `meta` 事件里就带上店铺和 trace，不等 LLM 生成完（5~20s） |
| **多轮 query 改写** | 后端读会话历史 → LLM 改写成自包含 query → 前端展示"原始 → 改写"，可解释 |
| 乐观插入 | 发送瞬间先插入 user 气泡 + 空 assistant 气泡，流式逐字填充，不等网络 |
| **Vue 响应式陷阱** | 占位消息必须用 `reactive()` 创建：push 进 ref 数组后若持有原始对象引用，改 `.content` 不触发视图更新——会导致"流式变全文一次性显示""改写标签不显示"。这是真实踩坑 |
| 等待话术 | 检索阶段（约 10~20s CPU 推理）随机轮播话术 + 跳动圆点，收到首个 token / 完整答案即停，避免用户面对空白傻等 |
| **切会话不中断（方案 B）** | 每个会话独立 `AbortController`（`controllers` Map）+ 独立 UI 状态（`sessionState`）。切会话/新建**不再 abort** 旧请求——它继续在后台生成、继续落库，切回来能看到进度/结果。这是对标 DeepSeek 网页版体验的关键改造 |
| **后端并发安全** | 消除 `pipe.prefer_llm` 全局可变状态，改成 per-request 参数传给 `search()`；否则两个并发请求会互相覆盖单例状态（真实竞态），无法安全并行 |
| **按会话消息缓存** | `messageCache`（sessionId→消息数组）常驻内存：切换只换引用、不重拉后端。后台会话的流式回调写它**自己**的数组（`arr`），不串进当前会话；可见时才自动滚动，不拽走用户视图 |
| 会话持久化 | SQLite 存会话与消息，切会话/重启后历史不丢；输入瞬间即落 user 消息并锁定标题（两阶段落库） |
| **富信息同步不被抹掉** | 回答落库后 `syncFromBackend()` 先按消息下标把流式期间已渲染的店铺卡片 / trace / 追问提示 / 条件摘要 / 改写标签搬到后端权威数据上，再整体替换缓存。否则 `backendToMessages()` 写死的 `shops: []` 会让"先显示卡片、LLM 吐完突然消失"（关 `buffer_guard` 时尤其明显）|
| **刷新后回显富信息** | 后端 `_build_persist_meta()` 把精简店铺 / trace / 条件摘要 / 改写一起落进消息 meta，前端 `backendToMessages()` 从 meta 还原。刷新/切走再切回后卡片、Trace、条件摘要、改写标签都还在（已 E2E 验证刷新前后 5/2/2/2 一致）|
| **停止按钮** | 模板必须写 `@click="abort()"`（带括号）——写成 `@click="abort"` 会把 MouseEvent 当会话 id 传进去，按钮静默失效；`abort()` 内再加类型守卫兜底 |
| **设置按会话隔离** | `sessionSettings`（sessionId→设置对象）+ computed 视图，v-model 自动跟随当前会话；新会话用默认值。`run()` 里用 `settingsFor(runSessionId)` 快照而非全局 computed，避免后台任务因切会话串参数 |
| 竞态处理 | 停止按钮只 abort 当前会话自己的请求；同会话串行、不同会话并行；旧轮 finally 不会误删新一轮的 controller |

详见 `docs/09-多轮会话与query改写.md`。

## 四、目录结构

```
front/
├── index.html
├── package.json
├── vite.config.js          # proxy 配置（→ 8000）
├── README.md
└── src/
    ├── main.js
    ├── App.vue             # 聊天界面：会话管理 + 消息流 + 三种模式编排
    ├── styles.css          # 全局样式（聊天布局 + 组件样式）
    ├── api/index.js        # 会话 CRUD + 4 端点封装 + SSE 解析
    └── components/
        ├── ShopCard.vue    # 店铺卡片
        └── TracePanel.vue  # Trace 折叠面板
```

## 五、生产构建

```powershell
npm run build      # 产物在 dist/
npm run preview    # 本地预览构建产物
```

生产部署时用 nginx 托管 `dist/`，并配置反向代理 `/api` → 后端服务。
