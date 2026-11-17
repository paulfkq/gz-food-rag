/**
 * 前端 API 层：封装后端 4 个端点的调用。
 *
 * 关键点：SSE 流式为什么不用 EventSource，而是 fetch + ReadableStream？
 *   - 原生 EventSource 只支持 GET，且不能自定义请求体；
 *   - 我们的 /api/answer/stream 是 POST + JSON body（query/topn/buffer_guard...）；
 *   - 所以用 fetch 拿可读流，手动按 SSE 协议解析（\n\n 分帧，event:/data: 分行）。
 *
 * SSE 协议回顾（后端 app.py::_sse 产生）：
 *   event: token\n
 *   data: {"text":"..."}\n
 *   \n
 */

const BASE = ''  // 相对路径，走 Vite proxy / 生产 nginx 反向代理

async function postJSON(path, body, signal) {
  const resp = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* 忽略解析失败 */ }
    throw new Error(`请求失败 ${resp.status}: ${detail}`)
  }
  return resp.json()
}

export async function getHealth(signal) {
  const resp = await fetch(BASE + '/health', { signal })
  if (!resp.ok) throw new Error(`健康检查失败 ${resp.status}`)
  return resp.json()
}

// ---------------- 多轮会话 CRUD ----------------

async function requestJSON(path, options, signal) {
  const resp = await fetch(BASE + path, { ...options, signal })
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
    throw new Error(`请求失败 ${resp.status}: ${detail}`)
  }
  // DELETE 可能返回空体
  const text = await resp.text()
  return text ? JSON.parse(text) : null
}

export function listSessions(signal) {
  return requestJSON('/api/sessions', {}, signal)
}

export function createSession(signal) {
  return requestJSON('/api/sessions', { method: 'POST' }, signal)
}

export function getSession(id, signal) {
  return requestJSON(`/api/sessions/${id}`, {}, signal)
}

export function renameSession(id, title, signal) {
  return requestJSON(`/api/sessions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  }, signal)
}

export function deleteSession(id, signal) {
  return requestJSON(`/api/sessions/${id}`, { method: 'DELETE' }, signal)
}

/**
 * 按下发送的瞬间调用：先落 user 消息 + 锁定会话标题（与回答是否完成解耦）。
 * 这样回答中途切走/中断/刷新，会话名和这条问题都已经在库里了。
 */
export function beginTurn(id, query, signal) {
  return requestJSON(`/api/sessions/${id}/begin-turn`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  }, signal)
}

/** 只检索：返回结构化店铺 + trace */
export function search(body, signal) {
  return postJSON('/api/search', body, signal)
}

/** 端到端非流式：返回完整答案 */
export function answer(body, signal) {
  return postJSON('/api/answer', body, signal)
}

/**
 * SSE 流式答案。
 *
 * @param {object}   body          请求体（query/topn/buffer_guard/use_llm...）
 * @param {object}   handlers      事件回调：{ meta, token, answer, guard, fallback, clarify, done, error }
 * @param {AbortSignal} signal     用于中断请求（用户点"停止"）
 *
 * 设计要点：
 *   1. 流式解析要处理"半帧"：TCP 每次 chunk 不保证正好是一个完整 SSE 帧，
 *      所以用 buffer 累积，按 \n\n 切分，最后一段落回 buffer。
 *   2. fallback 覆盖：buffer_guard=false 时校验失败会推 fallback 事件（replace=true），
 *      调用方（App.vue）收到后应把已显示的流式文本替换为模板答案 —— 这里原样透传。
 *   3. 每个回调都 try/catch 包住？不需要 —— 让异常冒泡到外层统一处理，避免吞错。
 */
export async function answerStream(body, handlers, signal) {
  const resp = await fetch(BASE + '/api/answer/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
    throw new Error(`流式请求失败 ${resp.status}: ${detail}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const dispatch = (event, data) => {
    if (handlers[event]) handlers[event](data)
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 帧以空行分隔；兼容 \r\n\r\n（虽然后端用的是 \n\n）
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      if (!frame.trim()) continue

      let event = 'message'
      const dataLines = []
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
      }
      if (!dataLines.length) continue
      try {
        dispatch(event, JSON.parse(dataLines.join('\n')))
      } catch (e) {
        console.warn('[sse] 解析 data 失败:', dataLines.join('\n'), e)
      }
    }
  }
  // 循环结束（流关闭）。done 事件由后端显式推送，这里不额外补发。
}
