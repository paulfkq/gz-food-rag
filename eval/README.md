# 评估系统（Gold Set / RAGAS 量化 / 消融表）

这是广州美食 RAG 项目的离线评估子系统。回答三个问题：

1. **系统现在质量如何？** —— 检索/约束/生成/RAGAS 四层量化，输出 Markdown 报告
2. **去掉某个组件会怎样？** —— 5 组单变量消融，输出对比表与 Δ
3. **差距到底出在哪一环？** —— 槽位解析准确率单列，区分「入口解析错」和「召回错」

## 目录结构

```
eval/
├── goldset.py          # Gold Set 生成器（固定 seed，从种子数据确定性过滤）
├── goldset.jsonl       # 生成的评估集（40 条，已提交，可直接用）
├── metrics.py          # 确定性指标（Recall/MRR/硬约束/槽位准确率/店铺名覆盖）
├── judge.py            # RAGAS 四指标（自实现 + 独立 LLM judge + 带模型名的缓存隔离）
├── run.py              # 评估运行器（一次加载模型、断点续跑、分层成本）
├── report.py           # 报告生成器（自动洞察 + 消融表 + 面试讲点）
├── compare.py          # 优化前后对比报告（基线 vs 新结果，逐指标 Δ + 归因）
└── out/                # 运行结果（gitignore：体积大且可重新生成）
    ├── results.jsonl   # 逐条结果（断点续跑的依据）
    ├── results_v2.jsonl   # 三项优化后的重评结果
    ├── baseline_results.jsonl  # 优化前基线快照（对比用）
    ├── judge_cache.jsonl  # RAGAS judge 缓存（key 带模型名，多模型互不污染）
    ├── report.md       # 最终评估报告
    └── qdrant_eval/    # 索引副本（与在线服务并行用）
```

## 快速开始

```bash
cd gz-food-rag
PY="D:/向量模型下载与部署/.venv/Scripts/python.exe"

# 1) 生成 gold set（已生成可跳过；固定 seed=42 可复现）
$PY eval/goldset.py --size 40

# 2) 跑评估（需要先设 LLM 环境变量；Qdrant 与在线服务不能共用）
RAG_QDRANT_PATH=C:/path/to/qdrant_copy \
RAG_LLM_API_KEY=sk-xxx RAG_LLM_BASE_URL=http://127.0.0.1:8600/v1 RAG_LLM_MODEL=glm-5.3-flash \
$PY eval/run.py --configs full,no_rerank,no_hard_filter,dense_only,rule_slots \
  --ragas --ragas-limit 12

# 3) 生成报告
$PY eval/report.py
# -> eval/out/report.md
```

调试时可以先用 `--limit 4` 只跑前几条（含 2 条冲突样本），确认链路通了再全量跑。

### 关于 `RAG_QDRANT_PATH`

Qdrant embedded 是**单进程独占**的：在线后端（8000）活着时，评估进程不能直接打开同一个
`data/qdrant`。`scripts/retrieve/config.py` 支持用环境变量覆盖索引路径，复制一份即可并行：

```bash
rm -rf eval/out/qdrant_eval && mkdir -p eval/out/qdrant_eval
cp -r data/qdrant/collection eval/out/qdrant_eval/
cp data/qdrant/meta.json eval/out/qdrant_eval/
# 注意跳过 .lock（被在线进程持有，cp 会报 Device or resource busy）
```

## 设计说明

### 1. Gold Set 不自我论证

ground truth 用**原始 shops.jsonl 的结构化字段独立过滤**得出，不调用被测 pipeline ——
否则就是"用被测代码自己造答案"的循环论证。每条样本存：

- `query`：口语化中文查询（从真实店铺字段采样组合）
- `constraints`：结构化硬约束
- `gold_shop_ids` / `gold_shop_names`：满足全部约束的店铺
- `gold_answer`：从结构化字段拼的参考答案
- `gold_contexts`：对应店铺的 parent 主档（RAGAS 的上下文基准）
- `parsed_slots`：规则解析器**实际**解析出的槽位（评估入口质量用）

查询类型覆盖 7 类（`area_price` / `district_cuisine` / `allergen` / `scene` /
`late_night` / `dimsum` / `metro`），加 2 条冲突追问样本。gold 数量限制在 1~8 家，
否则 Recall@8 天然很低、指标失去区分度。

### 2. 指标分层（不是全用 RAGAS）

| 层次 | 指标 | 类型 |
|---|---|---|
| Query Understanding | 槽位字段准确率 | 确定性 |
| 检索 | Recall@K / Precision@K / MRR / Hit Rate / nDCG | 确定性 |
| 约束 | 硬约束遵守率 | 确定性 |
| 生成 | 店铺名覆盖率 | 确定性 |
| 语义 | Faithfulness / Answer Relevancy / Context Precision / Context Recall | RAGAS |

**确定性指标是回归主指标**：快、可复现、零 judge 偏差，适合做消融和 CI。
RAGAS 补的是「答案有没有编造」「检索上下文是否真的有用」这类语义维度。

**judge 与生成模型分离**：生成端用 `glm-5.3-flash`，judge 默认用独立的
`deepseek-v4.1-flash`（可用 `RAG_JUDGE_MODEL / RAG_JUDGE_BASE_URL / RAG_JUDGE_API_KEY`
覆盖），避免"同一模型既当运动员又当裁判"的自评偏好。judge 缓存 key 带模型名前缀，
换模型自动失效旧缓存、多模型判断互不污染。RAGAS 只跑 full 配置的一部分样本（成本考虑）。

RAGAS 四指标按论文定义自实现而非 `pip install ragas`：
- 本环境 `openai==3.51`，ragas 新版依赖 openai>=1.x + 整套 langchain，强装会破坏现有链路；
- 四个指标的算法本身很短，自实现能在面试时讲清每一步（claim 抽取、rank-weighted、反向生成+余弦）。

### 3. 消融是单变量的

| 配置 | 改动 | 验证什么 |
|---|---|---|
| `full` | 无 | 基准 |
| `no_rerank` | 跳过 BGE-Rerank，用 RRF 顺序 | 精排的价值 |
| `no_hard_filter` | 关 pre-filter + 后置复核 | 硬约束双保险的价值 |
| `dense_only` | 只用 dense 单路召回 | 三路召回的边际收益 |
| `rule_slots` | 槽位解析关 LLM，用规则版 | LLM 入口解析 vs 规则 |
| `chunk_topn` | 关店铺级去重，退回 chunk 级 Top-N（优化前行为） | 店铺去重优化的收益 |

`RetrievalPipeline.search(ablation=...)` 是 per-request 参数，不改实例状态、向后兼容；
关硬约束时用空 SearchSlots 编译 filter，但 trace 仍保留原解析结果，便于分析差异。

**店铺级去重（默认开启）**：精排对全部 RRF 候选打分后按 `source_shop_id` 去重，
每家店只保留最高分块，再取 Top-N 家**不同的店**。优化前在 chunk 级取 Top-8，
8 个名额被 2~3 家店的多个块挤占，店铺级 Recall 严重偏低（实测 0.277）。
`chunk_topn` 消融关掉它即可复现旧行为，用于归因证明。

### 4. 成本工程（纯 CPU 必须做）

- **一次加载**：4.4GB 模型只加载一次（约 20s），否则 60 次查询光加载就要 20 分钟
- **断点续跑**：每条结果立即 append 到 JSONL，中断重跑自动跳过已完成项
- **RAGAS 缓存**：judge 结果按「样本+指标」存 JSONL，改报告不用重跑 judge
- **分层成本**：RAGAS 每条 4~5 次 LLM 调用，只跑 full 配置前 N 条

## 已知限制

1. **合成数据**：店铺信息是 synthetic（固定 seed），分布规整，指标绝对值不能外推线上；
   组件间相对差异有参考价值。
2. **店铺级去重的副作用**：召回店铺数从 2~3 增至 7~8 后，个别样本会暴露种子数据里
   `business_area` 与店名后缀不一致的噪声店（如店名带"市桥"但字段标"客村"），
   硬约束遵守率可能微降。这是采样变广暴露的既有数据问题，非去重逻辑引入。
3. **小样本**：纯 CPU 单轮检索约 15~20s，默认只跑有限样本；Δ < 0.02 视为不显著。
4. **judge 换模型的可比性**：judge 默认 deepseek-v4.1-flash（与生成模型 glm 分离，避免自评偏差），
   但不同模型打分尺度有系统偏移，RAGAS 绝对值跨模型不完全可比 —— 重点看同一 judge 下的相对变化。

## 面试讲点

1. **评估要分层**：确定性指标做回归主指标，RAGAS 做语义补充，别把两者混为一谈。
2. **消融要单变量**：每个配置只改一个组件，Δ 才能归因；加 `chunk_topn` 反向消融证明优化收益可归因。
3. **Gold Set 不能自证**：ground truth 独立于被测 pipeline 生成。
4. **发现问题→改进→重评闭环**：评估揭出"chunk 级 topn 挤占名额"，改店铺级去重后 Recall 从 0.277→~1.0；
   揭出"同模型自评偏差"，换独立 judge；揭出"冲突词表漏检"，AI 扩散词表补全。
   用 `eval/compare.py` 量化每项收益。
4. **入口也要评估**：槽位准确率把「检索差」拆成「解析错」还是「召回错」。
5. **成本是工程属性**：一次加载、断点续跑、judge 缓存 —— 没有这些评估根本跑不完。
