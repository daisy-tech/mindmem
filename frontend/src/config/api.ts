/**
 * API 根地址。
 * - Docker dev（npm run dev）：建议 VITE_API_BASE 留空，走 Vite 代理 /api -> backend:8000
 * - 本地直连：VITE_API_BASE=http://localhost:8000
 */
export const API_BASE = (() => {
  const raw = import.meta.env.VITE_API_BASE
  if (raw !== undefined && String(raw).trim() !== '') {
    return String(raw).replace(/\/$/, '')
  }
  if (import.meta.env.DEV) {
    return ''
  }
  return 'http://localhost:8000'
})()

export async function fetchApi(
  path: string,
  init?: RequestInit,
  timeoutMs = 30000,
): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: ctrl.signal })
  } catch (e) {
    if (e instanceof Error && e.name === 'AbortError') {
      throw new Error(
        `请求超时（${timeoutMs / 1000}s）。若浏览器无法访问 :8000，请将 VITE_API_BASE 留空并重启 frontend。`,
      )
    }
    throw e
  } finally {
    clearTimeout(timer)
  }
}
