<script setup>
/**
 * 评估中心页面（与对话前端并列的多页面）。
 * 四个能力：① 触发评估任务 ② 实时进度 ③ 结果展示 ④ 与历史评估对比。
 * 所有数据来自后端 /api/eval/*，聚合口径与 eval/report.py 的 Markdown 报告完全一致。
 */
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import {
  getEvalConfig, listRuns, getSummary, getDetail,
  compareRuns, listTasks, getTask, startRun,
} from './eval-api'

// ---------------- 状态 ----------------
const config = ref(null)                 // 指标/配置定义
const runs = ref([])                     // 历史评估运行列表
const summary = ref(null)                // 当前 run 的聚合指标
const detail = ref(null)                 // 当前 run 的逐条结果
const compareData = ref(null)            // 对比结果
const tasks = ref([])                    // 任务列表
const error = ref('')
const loading = ref({ runs: false, summary: false, detail: false, compare: false })

// 当前选中的 run / config
const selectedRun = ref('')
const detailConfig = ref('full')
const detailLimit = ref(50)

// 对比选择
const cmpBase = ref('')
const cmpNew = ref('')

// 触发表单
const form = reactive({
  configs: ['full', 'no_rerank', 'no_hard_filter', 'dense_only', 'rule_slots', 'chunk_topn'],
  limit: 14,
  ragas: true,
  ragas_limit: 12,
  sync_index: false,
  out_name: '',
})
const starting = ref(false)
const activeTaskId = ref('')             // 正在跟踪的任务
const pollTimer = ref(null)

// Tab
const tab = ref('overview')              // overview / run / compare / detail
const tabs = [
  { key: 'overview', label: '📊 结果总览', icon: '' },
  { key: 'run', label: '🚀 触发评估', icon: '' },
  { key: 'compare', label: '🆚 历史对比', icon: '' },
  { key: 'detail', label: '📋 结果明细', icon: '' },
]

const metricLabel = (key) => config.value?.metric_labels?.[key] || key
const configLabel = (key) => config.value?.config_labels?.[key] || key
// 轻量 Markdown 行内格式（**加粗**），内容来自后端可信报告，避免引入额外依赖
const mdInline = (s) => String(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')

// 当前任务的实时视图
const activeTask = computed(() => tasks.value.find(t => t.id === activeTaskId.value) || null)
const runningCount = computed(() => tasks.value.filter(t => t.status === 'running').length)

// ---------------- 数据加载 ----------------
async function loadConfig() {
  try { config.value = await getEvalConfig() } catch (e) { error.value = '加载配置失败：' + e.message }
}

async function loadRuns(selectLatest = false) {
  loading.value.runs = true
  try {
    const data = await listRuns()
    runs.value = data.runs || []
    if (selectLatest && runs.value.length && !runs.value.some(r => r.id === selectedRun.value)) {
      selectedRun.value = runs.value[0].id
      await loadSummary()
    }
  } catch (e) { error.value = '加载运行列表失败：' + e.message }
  finally { loading.value.runs = false }
}

async function loadSummary() {
  if (!selectedRun.value) { summary.value = null; return }
  loading.value.summary = true
  try { summary.value = await getSummary(selectedRun.value) }
  catch (e) { error.value = '加载指标失败：' + e.message; summary.value = null }
  finally { loading.value.summary = false }
}

async function onSelectRun() {
  await loadSummary()
  if (tab.value === 'detail') await loadDetail()
}

async function loadDetail() {
  if (!selectedRun.value) { detail.value = null; return }
  loading.value.detail = true
  try { detail.value = await getDetail(selectedRun.value, detailConfig.value, detailLimit.value) }
  catch (e) { error.value = '加载明细失败：' + e.message; detail.value = null }
  finally { loading.value.detail = false }
}

async function loadTasks() {
  try {
    const data = await listTasks()
    tasks.value = data.tasks || []
    // 若有正在跑的任务且当前未跟踪，自动跟上一个
    if (!activeTask.value) {
      const running = tasks.value.find(t => t.status === 'running')
      if (running) activeTaskId.value = running.id
    }
  } catch (e) { /* 任务列表失败不打扰主流程 */ }
}

async function doCompare() {
  if (!cmpBase.value || !cmpNew.value) { error.value = '请选择基线与新结果'; return }
  if (cmpBase.value === cmpNew.value) { error.value = '基线与新结果不能相同'; return }
  loading.value.compare = true
  error.value = ''
  try { compareData.value = await compareRuns(cmpBase.value, cmpNew.value) }
  catch (e) { error.value = '对比失败：' + e.message; compareData.value = null }
  finally { loading.value.compare = false }
}

// ---------------- 触发评估 + 轮询 ----------------
function toggleConfig(c) {
  const i = form.configs.indexOf(c)
  if (i >= 0) form.configs.splice(i, 1)
  else form.configs.push(c)
}

async function handleStart() {
  if (!form.configs.length) { error.value = '至少选择一个消融配置'; return }
  starting.value = true
  error.value = ''
  try {
    const res = await startRun({
      configs: form.configs.join(','),
      limit: form.limit,
      ragas: form.ragas,
      ragas_limit: form.ragas_limit,
      out_name: form.out_name || '',
      sync_index: form.sync_index,
    })
    activeTaskId.value = res.task_id
    tab.value = 'run'
    await loadTasks()
    startPolling()
  } catch (e) {
    error.value = '触发失败：' + e.message
  } finally {
    starting.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer.value = setInterval(async () => {
    await loadTasks()
    const t = activeTask.value
    if (t && t.status !== 'running') {
      stopPolling()
      await loadRuns(true)   // 完成后自动刷新运行列表
    }
  }, 3000)
}
function stopPolling() {
  if (pollTimer.value) { clearInterval(pollTimer.value); pollTimer.value = null }
}
function trackTask(id) {
  activeTaskId.value = id
  tab.value = 'run'
  if (activeTask.value?.status === 'running') startPolling()
}

// 状态徽章样式
const statusCls = (s) => ({ running: 'st-running', done: 'st-done', failed: 'st-failed', interrupted: 'st-warn' }[s] || 'st-warn')
const statusText = (s) => ({ running: '运行中', done: '已完成', failed: '失败', interrupted: '中断' }[s] || s)
const goodBadge = (deltaStr) => String(deltaStr || '').includes('优')
const badBadge = (deltaStr) => String(deltaStr || '').includes('劣')

// 格式化指标单元格：[值, n] -> "值 (n=..)"
const metricVal = (cellArr) => Array.isArray(cellArr) ? cellArr[0] : '—'
const metricN = (cellArr) => Array.isArray(cellArr) ? cellArr[1] : 0

onMounted(async () => {
  await Promise.all([loadConfig(), loadRuns(true), loadTasks()])
  // 预设对比：基线 vs 最新优化结果（若存在）
  const ids = runs.value.map(r => r.id)
  if (ids.includes('baseline_results.jsonl')) cmpBase.value = 'baseline_results.jsonl'
  else if (ids.length > 1) cmpBase.value = ids[ids.length - 1]
  if (ids.includes('results_v2.jsonl')) cmpNew.value = 'results_v2.jsonl'
  else if (ids.length) cmpNew.value = ids[0]
  if (cmpBase.value && cmpNew.value && cmpBase.value !== cmpNew.value) await doCompare()
  if (activeTask.value?.status === 'running') startPolling()
})
onUnmounted(stopPolling)
</script>

<template>
  <div class="eval-app">
    <!-- 顶栏 -->
    <header class="eval-topbar">
      <div class="eval-brand">
        <span class="eval-brand-icon">📈</span>
        <div>
          <strong>评估中心</strong>
          <small>广州美食 RAG · Gold Set / RAGAS / 消融</small>
        </div>
      </div>
      <div class="eval-topnav">
        <a href="/" class="nav-link">💬 返回对话</a>
        <span class="nav-sep"></span>
        <span class="run-count" v-if="runningCount">🟡 {{ runningCount }} 个任务运行中</span>
      </div>
    </header>

    <div class="eval-body">
      <!-- Tab 导航 -->
      <nav class="eval-tabs">
        <button v-for="t in tabs" :key="t.key" class="eval-tab"
                :class="{ active: tab === t.key }" @click="tab = t.key">{{ t.label }}</button>
      </nav>

      <div v-if="error" class="eval-alert">{{ error }}</div>

      <!-- ============ 结果总览 ============ -->
      <section v-if="tab === 'overview'" class="eval-section">
        <div class="section-toolbar">
          <div class="toolbar-left">
            <label>选择评估运行：</label>
            <select v-model="selectedRun" @change="onSelectRun" class="eval-select">
              <option v-for="r in runs" :key="r.id" :value="r.id">
                {{ r.id }} · {{ r.samples }} 样本 · {{ r.configs.length }} 配置 · {{ r.mtime }}
              </option>
            </select>
          </div>
          <button class="btn-mini" @click="loadRuns(true)">🔄 刷新</button>
        </div>

        <div v-if="loading.summary" class="eval-empty">正在加载指标…</div>
        <div v-else-if="!summary" class="eval-empty">暂无评估结果，请先到「触发评估」跑一轮，或确认后端已加载历史结果。</div>

        <template v-else>
          <!-- 元数据条 -->
          <div class="meta-strip">
            <span>📅 运行 <strong>{{ summary.run_id }}</strong></span>
            <span>🎯 Gold Set <strong>{{ summary.gold_n }}</strong> 条</span>
            <span>📝 结果 <strong>{{ summary.total_rows }}</strong> 行</span>
            <span>⚙️ 配置 <strong>{{ Object.keys(summary.configs).length }}</strong> 组</span>
          </div>

          <!-- full 主指标卡片 -->
          <div v-if="summary.configs.full" class="metric-grid">
            <div v-for="(val, key) in summary.configs.full.aggs" :key="key" class="metric-card">
              <div class="metric-name">{{ metricLabel(key) }}</div>
              <div class="metric-value">{{ metricVal(val) }}</div>
              <div class="metric-n">n = {{ metricN(val) }}</div>
            </div>
            <div class="metric-card accent">
              <div class="metric-name">冲突追问命中率</div>
              <div class="metric-value">{{ summary.configs.full.clarify_rate != null ? summary.configs.full.clarify_rate.toFixed(3) : '—' }}</div>
              <div class="metric-n">n = {{ summary.configs.full.clarify_n }}</div>
            </div>
            <div class="metric-card accent">
              <div class="metric-name">槽位字段准确率</div>
              <div class="metric-value">{{ summary.configs.full.slot_acc }}</div>
              <div class="metric-n">n = {{ summary.configs.full.slot_n }} 字段</div>
            </div>
            <div class="metric-card accent">
              <div class="metric-name">平均延迟</div>
              <div class="metric-value">{{ summary.configs.full.latency[0] }}s</div>
              <div class="metric-n">LLM 答案 {{ summary.configs.full.llm_mode }}/{{ summary.configs.full.n }}</div>
            </div>
          </div>

          <!-- 消融表 -->
          <div class="panel">
            <h3 class="panel-title">消融实验 · 单变量对比</h3>
            <table class="eval-table">
              <thead><tr>
                <th>配置</th><th>Recall@K</th><th>Precision@K</th><th>MRR</th>
                <th>硬约束遵守</th><th>平均延迟</th>
              </tr></thead>
              <tbody>
                <tr v-for="(d, cfg) in summary.configs" :key="cfg"
                    :class="{ 'row-highlight': cfg === 'full' }">
                  <td><strong>{{ configLabel(cfg) }}</strong></td>
                  <td>{{ metricVal(d.aggs.recall) }}</td>
                  <td>{{ metricVal(d.aggs.precision) }}</td>
                  <td>{{ metricVal(d.aggs.mrr) }}</td>
                  <td>{{ metricVal(d.aggs.hard_compliance) }}</td>
                  <td>{{ d.latency[0] }}s</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 自动洞察 -->
          <div class="panel" v-if="summary.insights && summary.insights.length">
            <h3 class="panel-title">🔍 关键发现（由数据自动推导）</h3>
            <ol class="insight-list">
              <li v-for="(s, i) in summary.insights" :key="i" v-html="mdInline(s)"></li>
            </ol>
          </div>
        </template>
      </section>

      <!-- ============ 触发评估 ============ -->
      <section v-if="tab === 'run'" class="eval-section">
        <div class="run-cols">
          <!-- 左：触发表单 -->
          <div class="panel">
            <h3 class="panel-title">发起新一轮评估</h3>
            <p class="hint">评估在独立后台进程运行，指向索引副本，不影响在线问答。纯 CPU 环境耗时较长。</p>

            <div class="form-group">
              <label class="form-label">消融配置（可多选）</label>
              <div class="chip-group">
                <button v-for="c in config?.valid_configs || []" :key="c" type="button"
                        class="cfg-chip" :class="{ on: form.configs.includes(c) }"
                        @click="toggleConfig(c)">{{ configLabel(c) }}</button>
              </div>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label class="form-label">正常样本数 (limit)</label>
                <input type="number" v-model.number="form.limit" min="1" max="40" class="eval-input" />
              </div>
              <div class="form-group">
                <label class="form-label">RAGAS 样本数</label>
                <input type="number" v-model.number="form.ragas_limit" min="1" max="40" class="eval-input" :disabled="!form.ragas" />
              </div>
            </div>

            <div class="form-group checks">
              <label class="chk"><input type="checkbox" v-model="form.ragas" /> 跑 RAGAS 四指标（需 LLM key）</label>
              <label class="chk"><input type="checkbox" v-model="form.sync_index" /> 触发前同步最新索引副本</label>
            </div>

            <div class="form-group">
              <label class="form-label">输出文件名（留空自动生成）</label>
              <input type="text" v-model="form.out_name" placeholder="如 results_v3.jsonl" class="eval-input" />
            </div>

            <button class="btn-primary" @click="handleStart" :disabled="starting || runningCount > 0">
              {{ starting ? '启动中…' : (runningCount ? '有任务运行中，请稍候' : '▶ 启动评估') }}
            </button>
          </div>

          <!-- 右：实时进度 -->
          <div class="panel">
            <h3 class="panel-title">任务进度</h3>
            <div v-if="!tasks.length" class="eval-empty">暂无评估任务记录。</div>
            <template v-else>
              <!-- 当前跟踪任务 -->
              <div v-if="activeTask" class="task-active">
                <div class="task-head">
                  <strong>{{ activeTask.id }}</strong>
                  <span class="status-badge" :class="statusCls(activeTask.status)">{{ statusText(activeTask.status) }}</span>
                </div>
                <div class="task-meta">
                  {{ activeTask.configs?.join(' / ') }} · limit={{ activeTask.limit }} · {{ activeTask.out }}
                </div>
                <div v-if="activeTask.progress" class="progress-wrap">
                  <div class="progress-bar"><div class="progress-fill" :style="{ width: activeTask.progress.pct + '%' }"></div></div>
                  <span class="progress-label">{{ activeTask.progress.cur }}/{{ activeTask.progress.total }} ({{ activeTask.progress.pct }}%)</span>
                </div>
                <div class="log-box">
                  <div v-for="(line, i) in activeTask.log_tail" :key="i" class="log-line">{{ line }}</div>
                </div>
              </div>
              <!-- 任务列表 -->
              <div class="task-list">
                <div v-for="t in tasks" :key="t.id" class="task-row" :class="{ on: t.id === activeTaskId }"
                     @click="trackTask(t.id)">
                  <span class="status-badge sm" :class="statusCls(t.status)">{{ statusText(t.status) }}</span>
                  <span class="task-row-id">{{ t.id }}</span>
                  <span class="task-row-out">{{ t.out }}</span>
                </div>
              </div>
            </template>
          </div>
        </div>
      </section>

      <!-- ============ 历史对比 ============ -->
      <section v-if="tab === 'compare'" class="eval-section">
        <div class="section-toolbar">
          <div class="toolbar-left">
            <label>基线：</label>
            <select v-model="cmpBase" class="eval-select" @change="compareData = null">
              <option v-for="r in runs" :key="'b'+r.id" :value="r.id">{{ r.id }}</option>
            </select>
            <label>新结果：</label>
            <select v-model="cmpNew" class="eval-select" @change="compareData = null">
              <option v-for="r in runs" :key="'n'+r.id" :value="r.id">{{ r.id }}</option>
            </select>
          </div>
          <button class="btn-mini primary" @click="doCompare" :disabled="loading.compare">🆚 对比</button>
        </div>

        <div v-if="loading.compare" class="eval-empty">正在计算对比…</div>
        <div v-else-if="!compareData" class="eval-empty">选择两次评估并点击「对比」，查看逐指标变化与优化归因。</div>

        <template v-else>
          <div class="compare-head">
            <span>基线 <strong>{{ compareData.base }}</strong> · {{ compareData.base_meta.samples }} 样本</span>
            <span class="arrow">→</span>
            <span>新结果 <strong>{{ compareData.new }}</strong> · {{ compareData.new_meta.samples }} 样本</span>
          </div>

          <div class="panel">
            <h3 class="panel-title">完整系统（full）逐指标对比</h3>
            <table class="eval-table">
              <thead><tr><th>指标</th><th>基线（旧）</th><th>新结果（新）</th><th>变化</th></tr></thead>
              <tbody>
                <tr v-for="m in compareData.full_metrics" :key="m.key">
                  <td>{{ m.label }}</td>
                  <td>{{ m.base }}</td>
                  <td>{{ m.new }}</td>
                  <td>
                    <span class="delta" :class="{ good: goodBadge(m.delta), bad: badBadge(m.delta) }">{{ m.delta }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="panel">
            <h3 class="panel-title">消融隔离 · 各配置 Recall@K</h3>
            <p class="hint">关掉店铺去重的 chunk_topn 复现优化前行为；若它≈基线、而 full≫两者，则提升可归因于店铺去重。</p>
            <table class="eval-table">
              <thead><tr><th>配置</th><th>基线</th><th>新结果</th><th>变化</th></tr></thead>
              <tbody>
                <tr v-for="a in compareData.ablation" :key="a.config"
                    :class="{ 'row-highlight': a.config === 'full' }">
                  <td>{{ a.label }}</td><td>{{ a.base }}</td><td>{{ a.new }}</td>
                  <td><span class="delta" :class="{ good: goodBadge(a.delta), bad: badBadge(a.delta) }">{{ a.delta }}</span></td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </section>

      <!-- ============ 结果明细 ============ -->
      <section v-if="tab === 'detail'" class="eval-section">
        <div class="section-toolbar">
          <div class="toolbar-left">
            <label>运行：</label>
            <select v-model="selectedRun" @change="loadDetail" class="eval-select">
              <option v-for="r in runs" :key="'d'+r.id" :value="r.id">{{ r.id }}</option>
            </select>
            <label>配置：</label>
            <select v-model="detailConfig" @change="loadDetail" class="eval-select">
              <option v-for="c in Object.keys(summary?.configs || { full:1 })" :key="c" :value="c">{{ configLabel(c) }}</option>
            </select>
            <label>条数：</label>
            <input type="number" v-model.number="detailLimit" min="1" max="100" class="eval-input sm" @change="loadDetail" />
          </div>
          <button class="btn-mini" @click="loadDetail">🔄 刷新</button>
        </div>

        <div v-if="loading.detail" class="eval-empty">加载中…</div>
        <div v-else-if="!detail || !detail.rows.length" class="eval-empty">该配置下暂无明细。</div>
        <template v-else>
          <div class="meta-strip"><span>共 <strong>{{ detail.total }}</strong> 条，显示前 {{ detail.rows.length }} 条</span></div>
          <div class="detail-list">
            <div v-for="(r, i) in detail.rows" :key="i" class="detail-card">
              <div class="detail-head">
                <span class="detail-id">{{ r.sample_id }}</span>
                <span class="detail-type">{{ r.type }}</span>
                <span v-if="r.expect_clarify" class="detail-tag warn">冲突样本 · {{ r.clarify_hit ? '命中' : '未命中' }}</span>
                <span class="detail-lat">{{ r.latency_ms }}ms</span>
              </div>
              <div class="detail-query">Q：{{ r.query }}</div>
              <div class="detail-shops" v-if="r.retrieved_names?.length">
                <span class="shop-pill" v-for="(n, j) in r.retrieved_names" :key="j">{{ j+1 }}. {{ n }}</span>
              </div>
              <div class="detail-metrics">
                <span v-for="(v, k) in r.metrics" :key="k">{{ metricLabel(k) }} <strong>{{ typeof v === 'number' ? v.toFixed(3) : v }}</strong></span>
                <template v-if="r.ragas && Object.keys(r.ragas).length">
                  <span v-for="(v, k) in r.ragas" :key="'g'+k">🏷 {{ metricLabel(k) }} <strong>{{ v != null ? Number(v).toFixed(3) : '—' }}</strong></span>
                </template>
              </div>
              <details v-if="r.answer" class="detail-answer"><summary>查看答案</summary><div>{{ r.answer }}</div></details>
            </div>
          </div>
        </template>
      </section>
    </div>

    <footer class="eval-footer">
      聚合口径复用 eval/report.py · 评估进程独立运行，指向索引副本 · <a href="/">返回对话 →</a>
    </footer>
  </div>
</template>
