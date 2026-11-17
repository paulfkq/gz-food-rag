/**
 * 评估系统前端 API 封装。
 * 所有请求走 /api/eval/*，由 Vite proxy 转发到后端 8000（与对话前端同源）。
 */

async function getJSON(path) {
  const resp = await fetch(path)
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
    throw new Error(`${detail} (${resp.status})`)
  }
  return resp.json()
}

async function postJSON(path, body) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
    throw new Error(`${detail} (${resp.status})`)
  }
  return resp.json()
}

export const getEvalConfig = () => getJSON('/api/eval/config')
export const listRuns = () => getJSON('/api/eval/runs')
export const getSummary = (runId) => getJSON(`/api/eval/runs/${encodeURIComponent(runId)}/summary`)
export const getDetail = (runId, config, limit = 50) =>
  getJSON(`/api/eval/runs/${encodeURIComponent(runId)}/detail?config=${encodeURIComponent(config)}&limit=${limit}`)
export const compareRuns = (base, next) =>
  getJSON(`/api/eval/compare?base=${encodeURIComponent(base)}&new=${encodeURIComponent(next)}`)
export const listTasks = () => getJSON('/api/eval/tasks')
export const getTask = (id) => getJSON(`/api/eval/tasks/${encodeURIComponent(id)}`)
export const startRun = (payload) => postJSON('/api/eval/run', payload)
