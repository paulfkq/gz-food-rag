# 多轮会话与 query 改写实现详解

> 面向新手 + 面试讲解。对应 Phase 5.5，实现文件：
> `scripts/service/session_store.py`、`scripts/generate/rewrite.py`、
> `scripts/service/app.py`（会话端点 + 改写集成）、`front/src/App.vue`（聊天界面）。

## 1. 为什么单轮检索不够：指代与省略

之前的前端每次查询都是**独立的**：输入问题 → 检索 → 展示 → 下次清空重来。
但真实对话里，用户第二轮常说什么？

```
轮1：西华路人均30以下的肠粉
轮2：那便宜点的呢          ← 没有区域、没有品类、没有价格数字
轮3：有没有安静点的        ← 只有一个新的偏好词
```

如果把"那便宜点的呢"原样丢进检索，槽位解析器提取不到任何硬约束，
三路召回只能在语义层面瞎猜，结果必然不理想。**这就是多轮对话的核心难题：上下文依赖。**

## 2. 两种关联方案，选 query 改写

| 方案 | 做法 | 优 | 劣 |
|---|---|---|---|
| **A. query 改写**（本项目采用） | 历史 + 当前输入 → LLM 改写成自包含 query → 送回原链路 | 检索层零改动、可复用单轮全部能力、易解释 | 多一次 LLM 调用（+2~5s）、改写需可观测 |
| B. 会话槽位继承 | 维护会话级 slots，增量 merge 新输入 | 确定性强、无额外 LLM | 要写复杂的槽位 merge/覆盖/冲突逻辑，语义偏好难处理 |

选 A 延续了本项目的**哑铃结构**：LLM 只在入口（改写 + 槽位）和出口（答案生成），
中间的召回/融合/Rerank 保持确定性。改写结果通过 `RewriteOut` 显式返回，
前端展示"理解为：xxx"，用户能看懂系统理解了什么——**可解释性优先于黑盒**。

## 3. SQLite 存储层（session_store.py）

### 表结构

```
sessions                       messages
─────────────────              ─────────────────────────────
id       TEXT PK               id              INTEGER PK
title    TEXT                  session_id      TEXT FK
created_at TEXT                role            user/assistant
updated_at TEXT                content         展示文本
                               query_original  用户原始输入
                               query_rewritten 改写后的检索 query
                               meta_json       mode/guard/count 等
```

一对多关系。首轮 user 消息会自动把会话标题设为问题前 24 字（演示时一眼能认出）。

### 三个工程点

1. **为什么 SQLite**：标准库自带、零运维、事务可靠、进程重启数据不丢；
   生产换 PostgreSQL 只需保持同样的方法签名，上层端点不动（存储抽象的价值）。
2. **线程模型**：每次操作新建连接。FastAPI 请求跑在不同线程，
   sqlite3 连接不能跨线程共享；连接创建开销极小（微秒级），换来天然线程安全。
3. **WAL 模式**：`PRAGMA journal_mode=WAL`，写不阻塞读，演示时多标签页刷新更顺。

`get_history(session_id, max_turns=3)` 只取最近 3 轮，避免 prompt 膨胀 + 远期对话干扰当前意图。

## 4. query 改写模块（rewrite.py）

### 触发策略：不是每轮都调 LLM

```python
if not history:                       # 首轮 -> 跳过，省一次调用
    return 原 query
if not llm.available:                 # 没配 key -> 降级
    return 原 query
if len(query) <= 20 and 命中指代信号词（那/便宜/安静/呢/换成...）:
    调 LLM 改写                        # 像追问 -> 才改写
else:
    return 原 query                    # 已经自包含 -> 跳过
```

这是**省调用优化**：判断错了（该改没改）最坏退化成单轮查询，不会出错。

### prompt 的四条硬规则

1. 继承历史中仍有效的条件（区域/品类/价格/忌口/环境）
2. 用户明确修改的以最新为准（"便宜点"→价格下调；"换成粤菜"→替换菜系）
3. 解析指代（"那里"=上次商圈；"第二个"=上次第二家店）
4. 只输出 query 本身，不加引号/解释（配 `_clean()` 清洗兜底）

### 实测效果（本项目真实跑出来的）

```
轮1 西华路人均30以下的肠粉
轮2 那便宜点的呢       -> 西华路人均20以下的肠粉     价格 30→20
轮3 那有没有安静点的   -> 西华路人均15以下安静点的肠粉 价格 20→15 + 追加偏好
轮4 换成粥粉面         -> 西华路人均15以下安静点的粥粉面 品类替换（继承上下文）
```

## 5. 后端集成（app.py）

### 统一入口 `_prepare_query`

```python
实际query, rewrite, history = _prepare_query(req, st.llm)
# 无 session_id -> 原样返回（旧客户端行为完全不变，向后兼容）
# 有历史 -> 读 history -> rewrite_query -> 返回改写后的 query
```

三个检索端点（search / answer / answer/stream）都先过这一步。

### 落库时机：两阶段落库（输入即锁标题 / 回答完成再收尾）

这是实测踩坑后重构的关键设计。旧版只在**回答生成完成**时才落 user 消息并设标题，
于是出现一串连锁 bug：回答中途切走 → 前端 abort → 后端 SSE 被切断 → `persist()` 没执行
→ user 消息根本没落库 → 标题停在"新会话"、刷新后内容全丢。

解法：把一轮落库拆成两阶段，用 `meta.phase` 衔接：

| 阶段 | 触发时机 | 端点 / 方法 | 动作 |
|---|---|---|---|
| **阶段1** | 用户按下发送的瞬间 | `POST /api/sessions/{id}/begin-turn` → `add_user_turn()` | 立即落 user 消息（`phase=pending`）+ **锁定标题**为问题前 24 字 |
| **阶段2** | 回答生成完成（或无命中/clarify） | `_save_turn()` → `finalize_turn()` | 回填 `query_rewritten`、`phase=done`，再落 assistant 消息 |

三个工程点：

1. **解耦**：标题与 user 消息在输入瞬间（毫秒级 SQLite 写）就落库，和后面 10~25s 的
   检索生成彻底分开。回答被中断/刷新，会话名和问题本体也已经持久化。
2. **pending 排除**：`get_history()` 会跳过 `phase=pending` 的 user，避免把"当前正在问的这句"
   当成历史去改写自己（否则 query 改写会自我干扰）。
3. **幂等**：`finalize_turn()` 找不到 pending 时不重复插 user；`add_user_turn()` 遇到
   内容相同且仍 pending 的最后一条（中断重发）直接复用。老客户端没调 begin-turn 时，
   finalize 会自动补落 user —— 行为与旧版完全一致（向后兼容）。

> **坑1**：`rewrite` 是 Pydantic 模型，SSE 里要用 `model_dump()` 不是 `to_dict()`
> （后者是 dataclass 的方法）。这个 bug 实测才发现，已修复。
>
> **坑2**：会话标题不能等回答完成才设。任何"生成后才落库"的设计，都会在中断/刷新时
> 丢标题和消息——这是流式/长耗时系统里非常典型的时序陷阱。

### 新增端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions` | 会话列表（轻量，不含消息正文） |
| GET | `/api/sessions/{id}` | 会话详情（含全部消息） |
| POST | `/api/sessions/{id}/begin-turn` | **阶段1**：输入瞬间落 user + 锁标题 |
| PATCH | `/api/sessions/{id}` | 重命名 |
| DELETE | `/api/sessions/{id}` | 删除 |

## 6. 前端聊天界面（App.vue）

- **左侧会话栏**：新建 / 切换 / 删除，标题 + 消息数，当前会话高亮，localStorage 记住上次会话
- **右侧消息流**：用户气泡（右）/ 助手消息（左），每条助手消息内可含：改写提示、答案、
  guard 徽章、店铺卡片网格、Trace 折叠面板
- **改写可视化**：`🔄 理解为：xxx` 直接标在用户消息下 + 助手答复顶部，用户看得见系统怎么理解
- **流式逐字**：占位一条空 assistant 消息，token 事件逐字追加 `aMsg.content`
- **乐观插入**：发送瞬间先插入 user 气泡 + 空 assistant 气泡，不用等网络
- **输入即锁标题**：`run()` 里乐观插入后立刻调 `beginTurn(runSessionId, userText)`，
  用 `runSessionId` 快照（切走后这条不会写进别的会话）、不传 abort signal（不受中断影响）。
  后端返回的标题直接更新到左侧会话栏；这样回答中途切走、中断、刷新，会话名都已是问题文本，
  不再是"新会话"，问题本身也不会丢

## 6.5 切会话不中断与并发安全（方案 B，对标 DeepSeek 网页版）

### 问题：为什么以前切会话必须中断？

用户实测发现：会话 A 正在回答时切到会话 B，A 只能被"掐断"。这是架构选择，不是技术做不到。
三层原因：

1. **前端全局单例请求状态**：只有单个 `abortController` / `busy` / `thinking`。切会话时 `abort()`
   会一刀切掐断正在跑的请求——这是当初为修"旧流写进新会话"而做的设计取舍。
2. **后端全局可变状态（真正的竞态）**：检索前用
   `old = pipe.prefer_llm; pipe.prefer_llm = X; finally: 恢复`。`pipe` 是 lifespan 里的单例，
   两个并发请求会互相覆盖（A 设 False、B 设 True，A 结束时恢复的值污染 B）。
3. **纯 CPU 算力**：Rerank 约 11s、LLM 约 5s，本机没有 GPU。

### DeepSeek 为什么没这问题

不是更聪明，是架构 + 资源不同：**每会话独立连接**、**生成任务与连接解耦**（后台跑+落库，
刷新也能续）、**GPU 多 worker 并行**有算力冗余。

### 本项目的解法（方案 B）

| 层 | 改造 |
|---|---|
| 后端并发安全 | `prefer_llm` 从实例可变状态改为 **per-request 参数**传给 `pipe.search(query, topn, prefer_llm=...)`。三处端点（search/answer/stream）去掉 `old=...; try/finally 恢复`。共享的只剩只读模型/索引，并发天然安全 |
| 前端请求隔离 | `abortController` 单例 → `controllers: Map<sessionId, AbortController>`；`busy/thinking/guard/...` → `sessionState[sessionId]`；模板用 computed 跟随 `currentId`。**切会话/新建不再 abort**，旧会话后台继续 |
| 写入隔离 | 乐观消息和流式更新都写 `cacheFor(runSessionId)` 自己的数组（helper 全传 `arr`），`applyRewrite` 也从 `arr` 找 user 气泡，不碰全局 `messages`。仅在会话可见时自动滚动 |
| 失败兜底 | 停止按钮只 abort 当前会话自己的 controller；删除会话才中断对应请求；组件卸载中断全部 |

**代价（诚实说明）**：纯 CPU 下两个会话并发会抢同一批核，实测单请求约 15s、并发时第二个约 30s
——不中断、结果不丢，但做不到"两个都快"，那是 GPU 才有的待遇。要做到 DeepSeek 那种
"关页面/刷新都不中断"，需方案 C（生成任务与连接解耦 + 后台任务队列），对学习项目属于过度设计。

## 6.6 流式富信息同步：卡片不能在 LLM 吐完时消失

### 现象
关闭"先校验后输出"（`buffer_guard=false`）后，店铺卡片在 `meta` 事件到达时先显示出来，
LLM 逐字吐完答案的瞬间，卡片突然被清空；Trace 面板、条件摘要、改写标签也一起没了。
开着 guard 时同样会发生，只是答案一次性出现、不容易察觉。

### 根因（是缓存同步 bug，不是 buffer_guard 的问题）
1. 流式期间：后端 `meta` 事件带店铺列表 → 前端 `doStream` 的 `meta` 回调执行 `aMsg.shops = d.shops`，
   卡片渲染出来。
2. 回答完成：`run()` 调 `syncFromBackend(runSessionId, s)` 用后端权威数据同步缓存。
3. `syncFromBackend` 原本直接 `arr.splice(0, arr.length, ...backendToMessages(s))` **整体替换**，
   而 `backendToMessages()` 出于"历史消息不回填卡片、避免响应体膨胀"的考虑，
   把每条消息的 `shops` **写死成 `[]`**，也不还原 trace / 追问标记 / `meta.slots` / 带 reason 的 rewrite。
4. 于是刚渲染的富信息被后端重建的空壳覆盖。打字机模式持续数秒，所以"先出现再消失"格外刺眼。

### 修法（两层）
1. **前端 `syncFromBackend`**：替换前按消息下标把前端流式期间已拿到的富信息搬回 fresh，
   再用 fresh 整体替换（保持数组引用不变、响应式可靠）——解决"回答完成瞬间消失"。
2. **后端持久化（见 6.7）**：把展示态一起落库，前端 `backendToMessages` 从 meta 还原
   ——解决"刷新后彻底丢失"，同时让第 1 层成为双保险。

第 1 层的核心代码：

```
fresh.forEach((m, i) => {
  const cur = arr[i]
  if (cur.shops.length)   m.shops = cur.shops
  if (cur.trace)          m.trace = cur.trace
  if (cur.needClarify)    m.needClarify = cur.needClarify
  if (cur.clarifyQuestion)m.clarifyQuestion = cur.clarifyQuestion
  if (cur.rewrite)        m.rewrite = cur.rewrite
  if (cur.meta.slots)     m.meta = { ...m.meta, slots: cur.meta.slots }
})
arr.splice(0, arr.length, ...fresh)
```

关键原则：**后端权威的是"答案文本和 meta 判定"，前端权威的是"本次流式渲染出的展示态"**。
同步时两者取各自所长，而不是无脑用一端覆盖另一端。

### 验证
真实浏览器 E2E（`buffer_guard=false`）：流式期间 2 张卡片 → 回答完成后仍 2 张，
Trace 面板与条件摘要"商圈=西华路 | 人均≤30 | 软偏好=肠粉"均保留；
另用 `vm` 直接加载 `syncFromBackend` 源码做单测，确认富信息全保留、文本/meta 用后端权威值。

## 6.7 刷新后回显富信息：后端把展示态一起落库

### 现象
刷新页面（或切走再切回）后，助手消息只剩正文，店铺卡片 / Trace / 条件摘要 / 改写标签全没了。

### 根因
后端落库时 meta 只存了 `mode/guard_reason/count` 三个运行字段，没有任何展示态；
前端 `backendToMessages(s)` 从后端历史重建消息时把 `shops` 写死成 `[]`、其余字段也不还原。
刷新走的就是这条"从后端重建"的路，于是富信息必然丢失。

### 修法
新增 `_build_persist_meta(req, trace, hits, rewrite, ...)`，三个端点（search/answer/stream）
的落库统一走它，把精简的 `ShopOut` / `TraceOut` / `slots` / `rewrite` / 追问信息一起写进 meta：

```
meta = {
  mode, guard_reason, count,
  slots,                       # 条件摘要字符串
  shops: [ShopOut...],         # 已砍掉 parent_text，单家十几个短字段
  trace: TraceOut,             # 只有计数与耗时 dict
  rewrite: {original, rewritten, reason},  # 仅 changed=True 时
}
```

前端 `backendToMessages` 从 meta 还原这些字段（老数据无 meta 时安全降级为空）。
**取舍变化**：早期为避免"响应体膨胀"故意不回填卡片；实测用户明确要求回显，
一轮 8 家店约几 KB，SQLite 与 getSession 响应都可承受，故让位于体验。这是面试可讲的权衡。
另 `syncFromBackend` 的搬运逻辑保留为双保险（后端万一漏存也能靠前端内存补上）。

### 验证
两轮对话后刷新：卡片 5→5、Trace 2→2、条件摘要 2→2、改写标签 2→2，完全一致。

## 6.8 停止按钮不生效：Vue 隐式传参的坑

### 现象
回答中点"■ 停止"，流程不终止、按钮不复位。

### 根因
模板写成 `@click="abort"`（不带括号），Vue 会把 MouseEvent 作为第一个参数传进去。
而 `abort(sid)` 把 `sid` 当成会话 id：`sid || currentId` 拿到的是事件对象（truthy），
`controllers.get(event)` 自然取不到 controller —— 按钮可点，实则什么都没中断。

### 修法
1. 模板改为 `@click="abort()"`（发送按钮同理 `@click="run()"`）；
2. `abort()` 内加类型守卫：`typeof sid === 'string' ? sid : currentId`，兜底防止别处误传。

### 验证
E2E：检索阶段点停止 → 停止按钮消失、发送按钮回来，UI 正确复位。

## 6.9 设置按会话隔离

### 现象
在会话1关闭"先校验后输出"，其他会话的设置也跟着变了——不合理。

### 根因
`topn / preferLlmSlots / useLlm / bufferGuard / includeTrace / mode` 原本是全局 `ref`，
所有会话共享一份。

### 修法
参照 `sessionState` 的模式：`sessionSettings: reactive({})` 按 sessionId 存，
再用 computed（`curSettings` + 每个字段的 get/set）暴露"当前会话视图"。
模板的 `v-model` 与业务代码几乎不用改，就自动跟随 `currentId` 切换；新会话首次访问拿默认配置。

**关键细节**：`run()` 里不能用全局 computed 读设置——后台会话运行时用户切走，
computed 会跟随新会话导致本轮参数串台。改为发起时 `settingsFor(runSessionId)` 取快照，
存进普通对象 `common` 再传给各 do* 函数（含 buffer_guard），彻底解耦。

### 验证
E2E：会话1 设 bufferGuard=false / topn=5 → 新建会话2 是默认 true/8 → 切回会话1 恢复 false/5。

## 7. 实测验证（全部通过）

```
✓ 新建会话 -> 首轮查询落库（首轮 rewrite=None，正确跳过）
✓ 追问"那便宜点的呢" -> 改写为"西华路人均20以下的肠粉"，命中更便宜的桥头灶
✓ 第三轮"那有没有安静点的" -> 价格再降到15 + 追加安静偏好
✓ 会话隔离：新会话首轮说指代句，无历史可继承，原样检索
✓ 流式端点 meta 事件带 rewrite，追问改写正常，历史落库完整
✓ 会话列表重启后仍在（SQLite 持久化验证）
✓ 不传 session_id 时 rewrite=None、行为与单轮版完全一致（向后兼容）
✓ 两阶段落库：输入即锁标题/落 user，中断后刷新标题和问题仍在；中断重发去重
✓ 后端并发：4 线程不同 prefer_llm 参数互不污染，实例默认值未被改写
✓ 真实服务并发两请求（规则版 vs LLM版）各自用对策略、无串扰（15s / 30s，CPU 抢核符合预期）
✓ E2E（playwright + 系统 Chrome）：会话 A 发问→立刻新建 B→A 不被中断→后台完成并落库→切回 A 看到完整答案
✓ 刷新回显：后端 meta 带齐展示态，刷新后卡片/trace/条件摘要/改写标签全部保留（5/2/2/2 一致）
✓ 停止按钮：修复 Vue 隐式传参后，检索阶段点停止能终止并复位 UI
✓ 设置按会话隔离：会话1 关 bufferGuard+topn5，会话2 仍默认，切回会话1 设置恢复
✓ 后端 _build_persist_meta 单测：展示态字段齐全，改写/clarify/首轮降级均正确
```

## 8. 面试讲点

1. **多轮 RAG 的本质是上下文依赖**，两种方案（query 改写 vs 槽位继承）的取舍。
2. **改写结果必须可观测**：展示"原始 → 改写"，而不是黑盒替换——用户信任 + 方便排错。
3. **降级链完整**：无历史/无 key/调用失败/返回空 → 都用原 query，系统永不挂。
4. **存储抽象**：SQLite 方法签名稳定，换 Postgres 上层不动。
5. **落库的终止分支完备性**：流式有 5 条退出路径，每条都要 persist——漏一条就丢历史。
6. **两阶段落库**：输入瞬间落 user + 锁标题，回答完成再收尾。核心教训是
   "长耗时操作不能把状态持久化押在成功路径末尾"——中断是常态，要解耦。
7. **幂等设计**：pending 去重 + finalize 幂等回填 + get_history 排除 pending，
   三处配合保证中断重发不产生脏历史、query 改写不自我干扰。
8. **向后兼容**：session_id 可选；begin-turn 缺失时 finalize 自动补落，老客户端零改动。
9. **并发安全的根本是消除共享可变状态**：单例 pipeline 上用 `old=X; finally 恢复` 改配置，
   并发必串。改成 per-request 参数传递后，共享的只有只读模型/索引，天然线程安全。
10. **切会话不中断的本质是状态按会话隔离**：前端每人一套 controller/UI 状态、写入各自的消息数组，
    "旧流写错会话"的问题从源头消除，而不是靠 abort 回避。能讲清"为什么 abort 是偷懒、隔离才是正解"。
11. **流式展示态与后端权威态要分治**：同步缓存时不能一端无脑覆盖另一端——答案文本/meta 判定用后端，
    本次流式渲染出的卡片/trace/条件摘要用前端。"卡片在 LLM 吐完瞬间消失"就是整体替换覆盖了富信息。
12. **刷新回显靠"展示态随答案一起落库"**：把精简的店铺/trace/条件摘要/改写存进 meta，
    刷新时从后端重建即可完整还原。取舍是响应体略变大，换来体验完整——工程里没有免费午餐，要会权衡。
13. **Vue 事件隐式传参**：`@click="fn"` 会把事件对象当第一个参数，若 fn 有形参会踩坑；
    `@click="fn()"` 才是无参调用。这类 bug 静默失败（不报错、只是没效果），E2E 才抓得到。
14. **UI 状态按会话隔离时，异步任务里必须用发起时快照**：不能用跟随 currentId 的 computed，
    否则后台任务运行时用户切会话会让任务参数串台。快照存进普通对象传递，是并发正确的关键。

## 9. 已知边界 / 后续可做

- 展示态随 meta 落库后响应体略增（一轮 8 家店约几 KB）；若会话消息很多、对首屏加载敏感，
  可做消息分页 + 展示态懒加载，或把精简店铺列表拆到单独的表按需取。
- 改写目前只看最近 3 轮，超长对话会丢失更早的约束（可改成维护一份会话级"长期槽位摘要"）。
- 没有做 query 改写的 ABCD 对照评估（可加 golden set 测指代消解准确率）。
- 生产化：会话需加用户归属与鉴权，SQLite 换 Postgres，消息分页加载。
