<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import {
  getHealth, search, answer, answerStream,
  listSessions, createSession, getSession, deleteSession, beginTurn,
} from './api'
import ShopCard from './components/ShopCard.vue'
import TracePanel from './components/TracePanel.vue'
import TrafficCard from './components/TrafficCard.vue'

// ---------------- 状态 ----------------
// 会话
const sessions = ref([])          // 会话列表
const currentId = ref('')         // 当前会话 ID
const messages = ref([])          // 当前会话的消息流（始终指向 messageCache[currentId] 那个数组）
const sessionTitle = ref('')

// ★ 按会话缓存消息（关键修复）：
// 后端只在【回答生成完成】时才把一轮消息落库（persist），回答前的乐观消息只存在于前端。
// 若切换会话时无脑 getSession 重拉，会把还没落库的乐观消息抹掉 —— 表现就是"切走再切回内容消失"。
// 缓存保证：每个会话的消息数组一旦加载就常驻内存，切换只换 messages 的引用，不重新拉；
// 这样未落库的消息也被保留，回答完成后再用后端权威数据同步一次（见 syncFromBackend）。
const messageCache = new Map()    // session_id -> reactive 消息数组

// 输入与参数（保留原有三种模式）
const query = ref('')

// ★ 设置按会话隔离：每个会话记住自己的 topn / 槽位 / 生成 / 校验 / trace / 模式。
//   早版这些是全局 ref，改一个会话的设置会影响所有会话（不合理）。
//   方案：reactive map 按 sessionId 存，再用 computed 暴露"当前会话视图"，
//   于是模板里的 v-model 与业务代码无需改动即可自动跟随 currentId 切换。
//   新会话首次访问时自动拿到一份默认值。
const sessionSettings = reactive({})
function settingsFor(id) {
  if (!sessionSettings[id]) {
    sessionSettings[id] = {
      mode: 'stream', topn: 8, preferLlmSlots: true,
      useLlm: true, bufferGuard: true, includeTrace: true,
      // 交通富化：默认关闭（不消耗地图配额）；开启后需填出发商圈或用定位
      enableTraffic: false, trafficArea: '', useTrafficIp: false,
    }
  }
  return sessionSettings[id]
}
const curSettings = computed(() => settingsFor(currentId.value))
const mode = computed({ get: () => curSettings.value.mode, set: v => { curSettings.value.mode = v } })
const topn = computed({ get: () => curSettings.value.topn, set: v => { curSettings.value.topn = v } })
const preferLlmSlots = computed({ get: () => curSettings.value.preferLlmSlots, set: v => { curSettings.value.preferLlmSlots = v } })
const useLlm = computed({ get: () => curSettings.value.useLlm, set: v => { curSettings.value.useLlm = v } })
const bufferGuard = computed({ get: () => curSettings.value.bufferGuard, set: v => { curSettings.value.bufferGuard = v } })
const includeTrace = computed({ get: () => curSettings.value.includeTrace, set: v => { curSettings.value.includeTrace = v } })
const enableTraffic = computed({ get: () => curSettings.value.enableTraffic, set: v => { curSettings.value.enableTraffic = v } })
const trafficArea = computed({ get: () => curSettings.value.trafficArea, set: v => { curSettings.value.trafficArea = v } })
const useTrafficIp = computed({ get: () => curSettings.value.useTrafficIp, set: v => { curSettings.value.useTrafficIp = v } })

const error = ref('')
const health = ref(null)

// 交通起点：浏览器 GPS（WGS-84）。后端收到后自动转 GCJ-02。
// 用 ref 而非 sessionSettings：定位是设备级信息，与会话无关，各会话共用最后一次定位即可。
const clientGps = ref(null)
const locating = ref(false)
function locate() {
  if (!navigator.geolocation) { error.value = '当前浏览器不支持定位'; return }
  locating.value = true
  error.value = ''
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      clientGps.value = {
        lat: pos.coords.latitude, lon: pos.coords.longitude,
        display_name: '我的位置（GPS）',
      }
      locating.value = false
      // 开启交通但没填商圈时，自动勾上交通开关
      if (!enableTraffic.value) enableTraffic.value = true
    },
    (err) => {
      locating.value = false
      error.value = `定位失败：${err.message || '用户拒绝或不可用'}（可改用填商圈）`
    },
    { timeout: 8000, maximumAge: 60000 },
  )
}

// ★ 按会话隔离的"运行中状态"（方案 B 核心）：
// 早版这里是全局单例（单个 abortController / busy / thinking），所以切会话只能 abort 掉正在跑的请求。
// 现在每个会话拥有独立的控制器与 UI 状态：
//   controllers  : sessionId -> AbortController（非响应式，纯控制对象）
//   sessionState : sessionId -> { busy, streaming, thinking, phrase, thinkTimer, guard, degraded, totalMs }
// 于是可以做到"会话 A 在后台继续生成，用户切到 B 继续聊"——
// A 的流式回调写它自己的 messageCache 数组，UI 状态也各记各的，切回 A 能接着看到进度。
const controllers = new Map()
const sessionState = reactive({})

function stateFor(id) {
  if (!sessionState[id]) {
    sessionState[id] = {
      busy: false,
      streaming: false,
      thinking: false,
      phrase: '',
      thinkTimer: null,
      guard: { ok: false, reason: '', mode: '' },
      degraded: false,
      totalMs: 0,
    }
  }
  return sessionState[id]
}

// guard_reason 判定：pass 与 auto_repaired(引用已自动校正)都算校验通过。
// auto_repaired: 后面跟修复明细，故用前缀判断，不能只 Equality 比较 'pass'。
const guardIsOk = (reason) => reason === 'pass' || (typeof reason === 'string' && reason.startsWith('auto_repaired:'))
const guardLabel = (reason) => reason && reason.startsWith('auto_repaired:') ? '引用已校正' : reason

// 下面这些 computed 是"当前显示会话"的状态视图 —— 模板无需改动，切换会话自动跟随。
const curState = computed(() => stateFor(currentId.value))
const busy = computed(() => curState.value.busy)
const streaming = computed(() => curState.value.streaming)
const activeGuard = computed(() => curState.value.guard)
const activeDegraded = computed(() => curState.value.degraded)
const activeTotalMs = computed(() => curState.value.totalMs)
const thinking = computed(() => curState.value.thinking)
const thinkPhrase = computed(() => curState.value.phrase)

// ---------------- 等待话术（检索阶段避免傻等）----------------
// 设计：请求发出即显示，随机切换；收到首个 token / 完整 answer / clarify / fallback 即停。
// 关键点：检索（三路召回+Rerank，CPU 上约 10~20s）期间后端还不会推正文，
//         这段时间给点"系统在干活"的反馈，体验上比空白等待好很多。
const THINKING_PHRASES = [
  '正在翻遍广州的街头巷尾…',
  '疯狂检索中，舌尖已经在路上了 🍜',
  '粥粉面饭，一个都别想逃出我的搜索',
  '正在认真干活，老板请再等我一秒',
  '把整条街的店都问了个遍…',
  '味蕾雷达全开，扫描中 📡',
  '正在和历史对话，理清你的话外之音…',
  '翻菜单中，别催别催（其实也想催）',
  '正在给每家店打分，公平公正',
  '骑楼底下都找遍了，马上就好',
  '正在用洪荒之力重新排序…',
  '老广带路中，跟着我吃准没错',
  '正在核对哪家最抵食，稍等片刻',
  '搜索雷达嗡嗡响，正在锁定目标',
  '正在把好店一个个捞出来 🍲',
]
function pickPhrase(prev) {
  if (THINKING_PHRASES.length < 2) return THINKING_PHRASES[0] || ''
  let next = prev
  while (next === prev) {
    next = THINKING_PHRASES[Math.floor(Math.random() * THINKING_PHRASES.length)]
  }
  return next
}
// 每个会话各自一个话术定时器，互不影响：A 在后台等模型时切到 B，A 的话术照常跳、
// B 从自己的状态读，不会串。定时器句柄存在 sessionState[id].thinkTimer。
function startThinking(sid) {
  stopThinking(sid)
  const st = stateFor(sid)
  st.thinking = true
  st.phrase = pickPhrase(st.phrase)
  st.thinkTimer = setInterval(() => {
    const s = stateFor(sid)
    s.phrase = pickPhrase(s.phrase)
  }, 1800)
}
function stopThinking(sid) {
  const st = stateFor(sid)
  st.thinking = false
  if (st.thinkTimer) { clearInterval(st.thinkTimer); st.thinkTimer = null }
}

const examples = [
  '西华路人均30以下的肠粉',
  '那便宜点的呢',
  '有没有安静点的',
  '不要香菜和花生',
  '人均30米其林',
]

// abort 只针对指定会话（默认当前会话），不再一刀切掐断所有请求。
// ★ 注意：模板里必须写 @click="abort()"（带括号）。
//   若写 @click="abort"，Vue 会把 MouseEvent 作为第一个参数传进来，
//   sid 就成了事件对象（truthy），controllers.get(event) 取不到 controller，
//   停止按钮看似可点、实则什么都没中断 —— 这是实测踩过的坑。
//   这里再加一道类型守卫兜底，避免别处误传非字符串 id。
function abort(sid) {
  const id = (typeof sid === 'string' && sid) ? sid : currentId.value
  const c = controllers.get(id)
  if (c) c.abort()
}
const messagesEl = ref(null)
const scrollToBottom = async () => {
  await nextTick()
  if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
}

const healthBadge = computed(() => {
  if (!health.value) return { text: '连接中…', cls: 'badge-loading' }
  if (health.value.status === 'ok') {
    return { text: `在线 · ${health.value.collection_points} 条索引`, cls: 'badge-online' }
  }
  return { text: health.value.status, cls: 'badge-error' }
})

// ---------------- 会话操作 ----------------
async function loadSessions() {
  sessions.value = await listSessions()
}

// 后端消息 -> 前端消息结构。
// 展示态（店铺卡片/trace/条件摘要/改写标签/追问提示）现在随后端 meta 一起落库，
// 这里全部还原 —— 否则刷新后只剩正文，用户会看到"内容突然变少"。
// 兼容老数据：meta 里没有的字段安全降级为空，不报错。
function backendToMessages(s) {
  return (s.messages || []).map(m => {
    const meta = m.meta || {}
    // 改写标签优先用 meta 里的完整版（带 reason）；老数据回退到 query_original/rewritten 推导
    let rewrite = meta.rewrite
      ? { original: meta.rewrite.original, rewritten: meta.rewrite.rewritten, reason: meta.rewrite.reason }
      : (m.query_original && m.query_rewritten && m.query_original !== m.query_rewritten)
        ? { original: m.query_original, rewritten: m.query_rewritten }
        : null
    return {
      role: m.role,
      content: m.content,
      queryOriginal: m.query_original,
      queryRewritten: m.query_rewritten,
      meta,
      shops: meta.shops || [],
      trace: meta.trace || null,
      traffic: meta.traffic || null,
      needClarify: !!meta.need_clarify,
      clarifyQuestion: meta.clarify_question || '',
      rewrite,
    }
  })
}

// 取某会话的消息数组；没有就建一个 reactive 空数组并缓存。
// 必须用 reactive()：messageCache 存的是数组本体，messages.value 指向它，
// 后续 push（乐观插入）要能触发视图更新。
function cacheFor(id) {
  if (!messageCache.has(id)) messageCache.set(id, reactive([]))
  return messageCache.get(id)
}

// 回答落库完成后，用后端权威数据同步缓存（in-place 替换，保持数组引用不变）。
// 这样能抹平乐观消息与最终落库结果的差异（如 guard 降级后文案变化、改写标签补全）。
//
// ★ 关键：不能直接整体替换。
//   backendToMessages() 出于"历史消息不回填卡片、避免响应体膨胀"的考虑，把 shops 写死成 []，
//   而 trace / needClarify / clarifyQuestion / meta.slots / rewrite(reason) 这些也是后端历史里没有的。
//   若直接 splice 替换，流式期间由 meta 事件渲染出的店铺卡片，会在 LLM 吐完的瞬间被抹掉 ——
//   关闭"先校验后输出"时打字机效果持续数秒，卡片先出现再突然消失，就是这个原因。
//   所以替换前先把这些"前端富信息"按消息下标搬到 fresh 上，再整体替换（保持响应式可靠）。
function syncFromBackend(id, s) {
  if (!s) return
  const arr = cacheFor(id)
  const fresh = backendToMessages(s)
  fresh.forEach((m, i) => {
    const cur = arr[i]
    if (!cur) return
    if (cur.shops && cur.shops.length) m.shops = cur.shops
    if (cur.trace) m.trace = cur.trace
    if (cur.traffic) m.traffic = cur.traffic       // 交通卡片：meta 落库版优先，前端富信息兜底
    if (cur.needClarify) m.needClarify = cur.needClarify
    if (cur.clarifyQuestion) m.clarifyQuestion = cur.clarifyQuestion
    if (cur.rewrite) m.rewrite = cur.rewrite            // applyRewrite 补的带 reason 版本更全
    // 条件摘要只在 SSE meta 事件里给，后端 assistant 的 meta 里没有
    if (cur.meta && cur.meta.slots && m.meta && !m.meta.slots) {
      m.meta = { ...m.meta, slots: cur.meta.slots }
    }
  })
  arr.splice(0, arr.length, ...fresh)
}

async function newSession() {
  // 方案 B：新建会话【不再中断】当前会话的请求 —— 它会在后台继续生成、继续落库。
  const s = await createSession()
  cacheFor(s.id)                 // 新会话先建空缓存
  currentId.value = s.id
  sessionTitle.value = s.title
  messages.value = cacheFor(s.id)
  localStorage.setItem('rag_session_id', s.id)
  await loadSessions()
  error.value = ''
  await scrollToBottom()
}

async function switchSession(id) {
  if (id === currentId.value) return
  // ★ 方案 B 核心：切会话【不 abort】。旧会话的请求在后台继续跑，
  //   它的流式回调写的是 cacheFor(旧id) 那个数组，不会串进新会话。
  //   UI 状态也按会话隔离（computed 跟随 currentId），切回来能接着看进度/结果。

  if (!messageCache.has(id)) {
    // 首次切到该会话：才从后端拉历史并填缓存
    const s = await getSession(id)
    sessionTitle.value = s.title
    const arr = cacheFor(id)
    arr.splice(0, arr.length, ...backendToMessages(s))
  } else {
    // 已缓存：直接用缓存（保住尚未落库的乐观消息），标题从列表取，不重拉
    const meta = sessions.value.find(x => x.id === id)
    sessionTitle.value = meta?.title || sessionTitle.value
  }
  currentId.value = id
  messages.value = cacheFor(id)
  localStorage.setItem('rag_session_id', id)
  error.value = ''
  await scrollToBottom()
}

async function removeSession(id, evt) {
  evt.stopPropagation()
  if (!confirm('确定删除这个会话？')) return
  abort(id)                      // 会话将被删除，中断它自己的请求（不影响别的会话）
  stopThinking(id)
  controllers.delete(id)
  delete sessionState[id]
  await deleteSession(id)
  messageCache.delete(id)        // 同步清缓存
  await loadSessions()
  if (id === currentId.value) {
    // 删的是当前会话 -> 切到剩下第一个，或新建一个
    if (sessions.value.length) await switchSession(sessions.value[0].id)
    else await newSession()
  }
}

async function fillExample(ex) {
  query.value = ex
  await run()
}

// ---------------- 核心：发送一轮 ----------------
async function run() {
  // busy 是"当前会话"是否忙：当前会话正在生成时再点发送 = 停止它（保持原交互）。
  // 注意别的会话在后台生成不影响这里 —— 各会话独立。
  if (busy.value) { abort(); return }
  if (!query.value.trim()) { error.value = '请输入内容'; return }

  // 没有当前会话就先建一个（用户可能直接开聊）
  if (!currentId.value) await newSession()

  // 会话 ID 快照：即便中途用户切了会话，本轮所有写入都用发起时的会话
  const runSessionId = currentId.value
  const st = stateFor(runSessionId)
  const userText = query.value.trim()
  query.value = ''
  error.value = ''
  st.busy = true
  startThinking(runSessionId)
  st.guard = { ok: false, reason: '', mode: '' }
  st.degraded = false
  st.totalMs = 0
  const controller = new AbortController()
  controllers.set(runSessionId, controller)   // 按会话存控制器，切会话不再误 abort
  const t0 = performance.now()

  // ★ 写入发起会话自己的缓存数组（不是全局 messages.value）。
  //   即便用户中途切到别的会话，这些消息和后续流式更新都落在本会话数组里。
  const arr = cacheFor(runSessionId)
  arr.push({
    role: 'user', content: userText, rewrite: null, shops: [],
  })
  // 占位 assistant 消息，流式时逐字填充。
  // ★ 必须用 reactive()：push 进 reactive 数组后若持有普通对象引用，
  //   修改 aMsg.content 不会触发 Vue 视图更新 —— 会导致"全文一次性显示"和"改写标签不显示"。
  const aMsg = reactive({
    role: 'assistant', content: '', rewrite: null, shops: [],
    needClarify: false, clarifyQuestion: '', meta: {}, traffic: null,
  })
  arr.push(aMsg)
  await scrollToBottom()

  // ★ 阶段1：按下发送的瞬间先落库 user 消息 + 锁定会话标题（快速 SQLite 写，毫秒级）。
  //   与后面 10~25s 的检索/生成彻底解耦：回答中途切走、中断、刷新都不影响标题和问题本体。
  //   用 runSessionId 快照，切走后这条也不会写进别的会话；不传 signal，不受 abort 影响。
  //   失败也不阻断回答（catch 里吞掉）——落库是增强，不该让整个问答挂掉。
  try {
    const locked = await beginTurn(runSessionId, userText)
    // 只更新发起时的会话，避免中途切走后标题串到当前会话
    const item = sessions.value.find(x => x.id === runSessionId)
    if (item) item.title = locked.title
    if (currentId.value === runSessionId) sessionTitle.value = locked.title
  } catch (e) {
    console.warn('[begin-turn] 锁定标题失败（不影响回答）:', e)
  }

  // ★ 设置快照：从发起会话的设置对象里取值，存成普通字段。
  //   不能用全局 computed（topn.value 等）——它们跟随 currentId，后台会话运行时
  //   用户一切走，值就会变成新会话的，导致本轮参数串台。用快照彻底解耦。
  const snap = settingsFor(runSessionId)
  const runMode = snap.mode
  const common = {
    query: userText,
    topn: snap.topn,
    prefer_llm_slots: snap.preferLlmSlots,
    use_llm: snap.useLlm,
    include_trace: snap.includeTrace,
    buffer_guard: snap.bufferGuard,
    session_id: runSessionId,
    enable_rewrite: true,
    // 交通富化参数（发起时快照，后台运行不串台）
    enable_traffic: !!snap.enableTraffic,
    traffic_area: (snap.trafficArea || '').trim() || null,
    use_traffic_ip: !!snap.useTrafficIp,
    client_gps: clientGps.value,   // 由「定位」按钮填充（WGS-84），无则为 null
  }

  try {
    if (runMode === 'search') await doSearch(common, aMsg, controller.signal, st, runSessionId)
    else if (runMode === 'answer') await doAnswer(common, aMsg, controller.signal, st, runSessionId)
    else await doStream(common, aMsg, controller.signal, st, runSessionId, arr)

    // 回答已落库：用后端权威数据同步该会话缓存（in-place，保持数组引用），
    // 抹平乐观消息与最终落库结果的差异。刷新标题 + 会话列表。
    const s = await getSession(runSessionId)
    // 只在用户仍停留在本会话时才改标题栏；否则后台完成不该覆盖当前看到别的会话的标题
    if (currentId.value === runSessionId) sessionTitle.value = s.title
    syncFromBackend(runSessionId, s)
    await loadSessions()
  } catch (e) {
    if (e.name === 'AbortError') {
      // 用户主动停止：给占位的 assistant 留个友好标记（消息本体仍在缓存里）
      if (!aMsg.content) aMsg.content = '⏸ 已停止'
    } else {
      const msg = e.message || String(e)
      if (currentId.value === runSessionId) error.value = msg
      aMsg.content = '⚠️ 请求失败：' + msg
    }
  } finally {
    stopThinking(runSessionId)
    st.streaming = false
    st.busy = false
    st.totalMs = Math.round(performance.now() - t0)
    // 只在控制器仍是本轮自己的时候才删：同一会话若已开启新一轮（新 controller），
    // 旧轮的 finally 不能把新一轮的控制器误删。
    if (controllers.get(runSessionId) === controller) controllers.delete(runSessionId)
    // 只在本会话可见时滚动，避免后台会话的更新把用户当前视图拽到底部
    if (currentId.value === runSessionId) await scrollToBottom()
  }
}

function applyRewrite(aMsg, data, arr) {
  if (data.rewrite && data.rewrite.changed) {
    aMsg.rewrite = {
      original: data.original_query || data.query,
      rewritten: data.query,
      reason: data.rewrite.reason,
    }
  }
  // 给用户气泡补改写标记（倒数第二条就是本轮插入的 user，从本会话数组找，不用全局 messages）
  const uMsg = arr[arr.length - 2]
  if (uMsg && uMsg.role === 'user' && aMsg.rewrite) uMsg.rewrite = aMsg.rewrite
}

function fillResult(aMsg, data, arr) {
  applyRewrite(aMsg, data, arr)
  aMsg.shops = data.shops || []
  aMsg.meta = { slots: data.slots || '', mode: data.mode || '', guard_reason: data.guard_reason || '' }
  aMsg.trace = data.trace || null
  aMsg.traffic = data.traffic || null
  aMsg.needClarify = !!data.need_clarify
  aMsg.clarifyQuestion = data.clarify_question || ''
}

// 只有当前会话可见时才自动滚动，避免后台会话的 token 推送把用户正在看的视图拽走
function scrollIfVisible(sid) {
  if (currentId.value === sid) scrollToBottom()
}

async function doSearch(body, aMsg, signal, st, sid, arr) {
  const data = await search(body, signal)
  stopThinking(sid)
  fillResult(aMsg, data, arr)
  aMsg.content = data.need_clarify
    ? `⚠️ 条件冲突：${data.clarify_question}`
    : data.count
      ? `为你找到 ${data.count} 家符合要求的店。`
      : '没有找到符合条件的店，试试放宽条件。'
  st.guard = { ok: true, reason: '检索完成', mode: 'search' }
}

async function doAnswer(body, aMsg, signal, st, sid, arr) {
  const data = await answer(body, signal)
  stopThinking(sid)
  fillResult(aMsg, data, arr)
  if (data.need_clarify) {
    aMsg.content = `⚠️ 条件冲突：${data.clarify_question}`
  } else {
    aMsg.content = data.answer || ''
    st.guard = {
      ok: guardIsOk(data.guard_reason),
      reason: guardLabel(data.guard_reason) || '', mode: data.mode || '',
    }
  }
}

async function doStream(body, aMsg, signal, st, sid, arr) {
  st.streaming = true
  // buffer_guard 取自发起时快照的 body，不读全局 computed（避免后台切会话串台）
  await answerStream(body, {
    meta: (d) => {
      applyRewrite(aMsg, d, arr)
      if (d.shops) aMsg.shops = d.shops
      if (d.trace) aMsg.trace = d.trace
      if (d.traffic) aMsg.traffic = d.traffic
      aMsg.meta = { slots: d.slots || '' }
      scrollIfVisible(sid)
    },
    clarify: (d) => {
      stopThinking(sid)
      aMsg.needClarify = true
      aMsg.clarifyQuestion = d.question
      aMsg.content = `⚠️ 条件冲突：${d.question}`
    },
    token: (d) => { stopThinking(sid); aMsg.content += d.text; scrollIfVisible(sid) },
    answer: (d) => {
      stopThinking(sid)
      aMsg.content = d.text
      st.guard = { ok: guardIsOk(d.guard_reason), reason: guardLabel(d.guard_reason) || '', mode: d.mode || '' }
    },
    corrected: (d) => {
      stopThinking(sid)
      // 引用错位已自动修正：覆盖已吐出的错文本，但仍是 LLM 答案，不标降级。
      if (d.replace) aMsg.content = d.text
      st.guard = { ok: true, reason: guardLabel(d.reason) || '引用已校正', mode: d.mode || 'llm' }
    },
    guard: (d) => { st.guard = { ok: d.ok, reason: guardLabel(d.reason) || '', mode: st.guard.mode } },
    fallback: (d) => {
      stopThinking(sid)
      if (d.replace) { aMsg.content = d.text; st.degraded = true }
      st.guard = { ok: false, reason: d.reason || '校验未通过', mode: 'template' }
    },
    error: (d) => {
      stopThinking(sid)
      if (currentId.value === sid) error.value = `流式错误：${d.type}: ${d.message}`
    },
  }, signal)
}

onMounted(async () => {
  try {
    health.value = await getHealth()
    await loadSessions()
    // 恢复上次的会话（localStorage）
    const saved = localStorage.getItem('rag_session_id')
    if (saved && sessions.value.some(s => s.id === saved)) {
      await switchSession(saved)
    } else if (sessions.value.length) {
      await switchSession(sessions.value[0].id)
    } else {
      await newSession()
    }
  } catch (e) { error.value = '后端未连接：' + e.message }
})
onUnmounted(() => {
  // 组件销毁：中断所有仍在后台跑的会话请求，清掉所有话术定时器
  controllers.forEach((c) => c.abort())
  controllers.clear()
  Object.keys(sessionState).forEach((sid) => stopThinking(sid))
})
</script>

<template>
  <div class="chat-app">
    <!-- 左侧：会话列表 -->
    <aside class="sidebar">
      <div class="sidebar-head">
        <div class="brand-mini">
          <span class="brand-icon">🍜</span>
          <div><strong>广州美食 RAG</strong><small>多轮对话演示</small></div>
        </div>
        <!-- 不再因 busy 禁用：等模型时可以随时新建会话 -->
        <button class="btn-new-chat" @click="newSession">＋ 新会话</button>
      </div>
      <div class="session-list">
        <div v-for="s in sessions" :key="s.id"
             class="session-item" :class="{ active: s.id === currentId }"
             @click="switchSession(s.id)">
          <span class="session-icon">💬</span>
          <span class="session-title">{{ s.title }}</span>
          <span class="session-count">{{ s.message_count }}</span>
          <button class="session-del" @click="removeSession(s.id, $event)" title="删除">×</button>
        </div>
      </div>
      <!-- 跳转评估中心（独立多页面）：与对话页同源，走同一后端 /api/eval/* -->
      <a href="/eval.html" class="eval-entry" title="查看评估指标 / 触发评估 / 历史对比">
        <span class="eval-entry-icon">📈</span>
        <span class="eval-entry-text">
          <strong>评估中心</strong>
          <small>指标报告 · 触发评估 · 历史对比</small>
        </span>
        <span class="eval-entry-arrow">→</span>
      </a>
      <div class="sidebar-foot">
        <span class="status-dot" :class="healthBadge.cls"></span>{{ healthBadge.text }}
      </div>
    </aside>

    <!-- 右侧：对话区 -->
    <main class="chat-main">
      <header class="chat-header">
        <div>
          <h2>{{ sessionTitle || '新会话' }}</h2>
          <p>父子块切分 · 三路召回 · Rerank · LLM 生成 · 引用校验 · 多轮改写</p>
        </div>
        <div class="mode-switch">
          <button v-for="m in [
              { key: 'stream', label: '💬 流式' },
              { key: 'answer', label: '✨ 完整' },
              { key: 'search', label: '🔎 检索' },
            ]" :key="m.key" class="mode-btn" :class="{ active: mode === m.key }"
            @click="mode = m.key">{{ m.label }}</button>
        </div>
      </header>

      <!-- 消息流 -->
      <div class="messages" ref="messagesEl">
        <div v-if="!messages.length" class="welcome">
          <div class="welcome-icon">🍜</div>
          <h3>想吃点什么？</h3>
          <p>试试下面的问题，然后继续追问（比如"那便宜点的呢"）。</p>
          <div class="example-grid">
            <button v-for="ex in examples" :key="ex" class="example-chip" @click="fillExample(ex)">{{ ex }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="message" :class="'msg-' + m.role">
          <!-- 用户气泡 -->
          <template v-if="m.role === 'user'">
            <div class="bubble bubble-user">{{ m.content }}</div>
            <div v-if="m.rewrite" class="rewrite-note">
              🔄 理解为：<strong>{{ m.rewrite.rewritten }}</strong>
            </div>
          </template>

          <!-- 助手消息 -->
          <template v-else>
            <div class="assistant-block">
              <div class="assistant-label">🤖 助手</div>
              <div v-if="m.rewrite" class="rewrite-note-inline">
                🔄 已结合上下文改写：<strong>{{ m.rewrite.original }}</strong> → <strong>{{ m.rewrite.rewritten }}</strong>
              </div>
              <div v-if="m.needClarify" class="alert alert-warn">⚠️ {{ m.clarifyQuestion }}</div>
              <div class="bubble bubble-assistant">
                <!-- 等待话术：仅当前正在等待、且这条消息还没正文时显示 -->
                <span v-if="thinking && i === messages.length - 1 && !m.content" class="thinking">
                  <span class="thinking-dots"><i></i><i></i><i></i></span>
                  {{ thinkPhrase }}
                </span>
                <span v-else class="answer-text">{{ m.content }}</span>
                <span v-if="streaming && i === messages.length - 1 && m.content" class="cursor">▋</span>
              </div>

              <!-- 徽章 -->
              <div v-if="m.content && !streaming" class="msg-badges">
                <span v-if="m.meta && m.meta.mode" class="badge"
                      :class="m.meta.mode === 'llm' ? 'badge-llm' : 'badge-template'">
                  {{ m.meta.mode === 'llm' ? 'LLM 生成' : m.meta.mode === 'search' ? '纯检索' : '模板答案' }}
                </span>
                <span v-if="activeGuard.reason && i === messages.length - 1" class="badge"
                      :class="activeGuard.ok ? 'badge-pass' : 'badge-guard'">
                  {{ activeGuard.ok ? '✓ ' + activeGuard.reason : '⚠ ' + activeGuard.reason }}
                </span>
                <span v-if="activeDegraded && i === messages.length - 1" class="badge badge-guard">已自动降级</span>
                <span v-if="m.meta && m.meta.slots" class="slots-inline">条件：{{ m.meta.slots }}</span>
              </div>

              <!-- 店铺卡片：检索完成即显示（不等 LLM 生成） -->
              <div v-if="m.shops && m.shops.length" class="msg-shops">
                <div class="msg-shops-head">推荐店铺 · {{ m.shops.length }} 家</div>
                <div class="shop-grid">
                  <ShopCard v-for="shop in m.shops" :key="shop.shop_id" :shop="shop" />
                </div>
              </div>

              <!-- 交通参考卡片：与 meta 一起到达，LLM 生成前即可显示 -->
              <TrafficCard v-if="m.traffic && m.traffic.shops && m.traffic.shops.length"
                           :traffic="m.traffic" :shops="m.shops || []" />

              <!-- Trace -->
              <details v-if="m.trace" class="msg-trace">
                <summary>查看 Trace 明细</summary>
                <TracePanel :trace="m.trace" />
              </details>
            </div>
          </template>
        </div>

        <div v-if="error" class="alert alert-error">{{ error }}</div>
      </div>

      <!-- 输入区 -->
      <div class="composer">
        <div class="composer-options">
          <label>返回数 <input type="number" v-model.number="topn" min="1" max="20" class="opt-num" /></label>
          <label class="opt-check"><input type="checkbox" v-model="preferLlmSlots" /> LLM 槽位</label>
          <label class="opt-check" v-if="mode !== 'search'"><input type="checkbox" v-model="useLlm" /> LLM 生成</label>
          <label class="opt-check" v-if="mode === 'stream'"><input type="checkbox" v-model="bufferGuard" /> 先校验后输出</label>
          <label class="opt-check"><input type="checkbox" v-model="includeTrace" /> Trace</label>
          <label class="opt-check" title="检索后、LLM 前旁路富化交通；默认关闭以节省地图配额">
            <input type="checkbox" v-model="enableTraffic" /> 交通
          </label>
        </div>
        <!-- 交通起点：填商圈名 / 用浏览器定位 / IP 粗定位（仅城市级） -->
        <div class="traffic-options" v-if="enableTraffic">
          <label>出发商圈 <input type="text" v-model="trafficArea" class="opt-text"
                 placeholder="如：体育西路（留空则用定位/IP）" /></label>
          <button type="button" class="btn-locate" @click="locate" :disabled="locating">
            {{ locating ? '定位中…' : (clientGps ? '✓ 已定位' : '📍 用我的位置') }}
          </button>
          <label class="opt-check" title="IP 定位仅城市级，精细交通会被拦截"><input type="checkbox" v-model="useTrafficIp" /> IP 粗定位</label>
        </div>
        <div class="composer-row">
          <textarea v-model="query" class="composer-input" rows="2"
                    placeholder="输入你的问题，或继续追问（Enter 发送，Shift+Enter 换行）"
                    @keydown.enter.exact.prevent="run"></textarea>
          <button class="btn-send" @click="run()" v-if="!busy">发送 ➤</button>
          <button class="btn-stop" @click="abort()" v-else>■ 停止</button>
        </div>
        <div class="composer-hint">
          多轮对话会自动结合历史改写你的问题 · 后端 CPU 推理较慢（约 10~25s/轮）
          <span v-if="activeTotalMs">· 上轮耗时 {{ (activeTotalMs / 1000).toFixed(1) }}s</span>
        </div>
      </div>
    </main>
  </div>
</template>
