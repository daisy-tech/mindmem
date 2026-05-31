import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'
import type { PromptData } from './chat'

import { fetchApi } from '../config/api'

export interface EvalRunSummary {
  run_id: string
  run_type?: string
  started_at?: string
  duration_sec?: number
  verdict?: {
    pass?: boolean
    pass_count?: number
    total?: number
    pass_rate?: number
  }
  suite?: { executed_cases?: number }
  error?: string
}

export interface EvalCaseL1 {
  intent_match?: boolean
  expected_intent?: string | string[]
  actual_intent?: string
  keyword_hits?: number
  keyword_total?: number
  boundary_violations?: string[]
  reply_violations?: string[]
  pass?: boolean
}

export interface EvalCaseResult {
  id: string
  bucket?: string
  personality?: string
  message: string
  history?: Array<{ role: string; content: string }>
  expect?: Record<string, unknown>
  candidate?: {
    prompt_meta?: PromptData
    reply?: string | null
    l1?: EvalCaseL1
  }
}

export interface EvalReport {
  run_id: string
  run_type?: string
  started_at?: string
  finished_at?: string
  duration_sec?: number
  persona_ref?: string
  eval_user_id?: string
  triggered_by_user_id?: string
  user_id?: string
  verdict?: EvalRunSummary['verdict']
  models?: { chat?: { name?: string }; run_chat?: boolean }
  suite?: { executed_cases?: number; case_ids?: string[] }
  cases?: EvalCaseResult[]
}

export interface EvalJobStatus {
  running: boolean
  run_id?: string | null
  progress?: number
  total?: number
  current_case?: string | null
  error?: string | null
}

export interface QueryEvaluation {
  pass: boolean
  auto_pass?: boolean
  expect_pass?: boolean | null
  auto_checks?: Array<{
    id: string
    label: string
    pass: boolean
    detail?: string
    informational?: boolean
  }>
  l1?: EvalCaseL1 | null
}

export interface QueryResult {
  prompt_meta: Record<string, unknown>
  reply: string | null
  run_chat: boolean
  personality?: string
  timing_ms?: { total?: number; context?: number; chat?: number }
  tokens?: number | null
  evaluation?: QueryEvaluation
  expect?: Record<string, unknown>
}

export interface DraftSummary {
  draft_id: string
  title?: string
  message?: string
  personality?: string
  run_chat?: boolean
  created_at?: string
  has_reply?: boolean
  error?: boolean
}

export interface EvalDraft {
  draft_id: string
  title: string
  created_at?: string
  updated_at?: string
  input: {
    message: string
    history: Array<{ role: string; content: string }>
    personality: string
    run_chat: boolean
    expect?: Record<string, unknown>
  }
  result: {
    prompt_meta?: Record<string, unknown>
    reply?: string | null
    timing_ms?: QueryResult['timing_ms']
    run_chat?: boolean
    tokens?: number | null
    evaluation?: QueryEvaluation
    expect?: Record<string, unknown>
  }
}

function promptFromMeta(meta: Record<string, unknown> | undefined): PromptData | undefined {
  if (!meta) return undefined
  return {
    version: meta.version as string | undefined,
    memories: (meta.memories as string[]) ?? [],
    system: (meta.system as string) ?? '',
    route: meta.route as PromptData['route'],
    activated: meta.activated as PromptData['activated'],
    context_layers: meta.context_layers as PromptData['context_layers'],
    model: meta.model as string | undefined,
    composed_at: meta.composed_at as string | undefined,
    trigger_message: meta.trigger_message as string | undefined,
    history_turns: meta.history_turns as number | undefined,
    llm_request: meta.llm_request as PromptData['llm_request'],
  }
}

async function parseApiError(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json()
    const detail = data.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join('; ') || fallback
    }
  } catch {
    /* ignore */
  }
  return `${fallback} (HTTP ${res.status})`
}

export const useEvalStore = defineStore('eval', () => {
  const runs = ref<EvalRunSummary[]>([])
  const currentReport = ref<EvalReport | null>(null)
  const jobStatus = ref<EvalJobStatus>({ running: false })
  const loading = ref(false)
  const lastError = ref<string | null>(null)
  const apiAvailable = ref<boolean | null>(null)

  async function fetchRuns() {
    const auth = useAuthStore()
    if (!auth.token) return
    const res = await fetchApi('/api/eval/runs', {
      headers: auth.authHeaders(),
    })
    if (res.status === 404) {
      apiAvailable.value = false
      throw new Error(
        '评测 API 不存在 (404)。请重启/重新部署 backend，确保已包含 /api/eval 路由。',
      )
    }
    if (!res.ok) {
      throw new Error(await parseApiError(res, '加载报告列表失败'))
    }
    apiAvailable.value = true
    lastError.value = null
    const data = await res.json()
    runs.value = data.runs ?? []
  }

  async function fetchReport(runId: string) {
    const auth = useAuthStore()
    if (!auth.token) return
    loading.value = true
    try {
      const res = await fetchApi(`/api/eval/runs/${encodeURIComponent(runId)}`, {
        headers: auth.authHeaders(),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, '加载报告失败'))
      }
      currentReport.value = (await res.json()) as EvalReport
    } finally {
      loading.value = false
    }
  }

  async function fetchJobStatus(): Promise<EvalJobStatus> {
    const auth = useAuthStore()
    if (!auth.token) return jobStatus.value
    const res = await fetchApi('/api/eval/status', {
      headers: auth.authHeaders(),
    })
    if (res.status === 404) {
      apiAvailable.value = false
      throw new Error('评测 API 不存在 (404)，请重新部署 backend')
    }
    if (!res.ok) {
      throw new Error(await parseApiError(res, '获取评测状态失败'))
    }
    apiAvailable.value = true
    const data = (await res.json()) as EvalJobStatus
    jobStatus.value = data
    return data
  }

  async function startEval(runType: 'smoke' | 'full', runChat: boolean) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    lastError.value = null

    const res = await fetchApi('/api/eval/runs', {
      method: 'POST',
      headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_type: runType, run_chat: runChat }),
    })

    if (res.status === 404) {
      apiAvailable.value = false
      throw new Error(
        '评测 API 不存在 (404)。当前 backend 可能未部署评测模块，请重启 docker 或重新部署后再试。',
      )
    }
    if (res.status === 409) {
      throw new Error(await parseApiError(res, '已有任务在运行'))
    }
    if (!res.ok) {
      throw new Error(await parseApiError(res, '启动评测失败'))
    }

    apiAvailable.value = true
    const data = await res.json()
    // 立刻更新本地状态，不等待第一次轮询
    jobStatus.value = {
      running: true,
      run_id: data.run_id ?? null,
      progress: 0,
      total: data.total ?? 0,
      current_case: '准备中…',
      error: null,
    }
    await fetchJobStatus()
    return data
  }

  function triggerJsonDownload(data: unknown, filename: string) {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  async function downloadReport(runId: string) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')

    let report: EvalReport
    if (currentReport.value?.run_id === runId) {
      report = currentReport.value
    } else {
      const res = await fetchApi(`/api/eval/runs/${encodeURIComponent(runId)}`, {
        headers: auth.authHeaders(),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, '加载报告失败'))
      }
      report = (await res.json()) as EvalReport
    }

    triggerJsonDownload(report, `${runId}_report.json`)
    return report
  }

  async function fetchPersonaData(): Promise<Record<string, unknown>> {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetchApi('/api/eval/persona/data', {
      headers: auth.authHeaders(),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '读取失败'))
    return (await res.json()) as Record<string, unknown>
  }

  async function seedPersona(): Promise<{
    ok: boolean
    persona_ref: string
    user_id: string
    display_name?: string
    episodic_added?: number
  }> {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetchApi('/api/eval/persona/seed', {
      method: 'POST',
      headers: auth.authHeaders(),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '灌库失败'))
    return await res.json()
  }

  async function importReport(file: File) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const text = await file.text()
    let report: Record<string, unknown>
    try {
      report = JSON.parse(text)
    } catch {
      throw new Error('无效 JSON 文件')
    }
    const res = await fetchApi('/api/eval/runs/import', {
      method: 'POST',
      headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ report }),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '导入失败'))
    const data = await res.json()
    await fetchRuns()
    return data.run_id as string
  }

  function casePromptData(c: EvalCaseResult): PromptData | undefined {
    return promptFromMeta(c.candidate?.prompt_meta as unknown as Record<string, unknown>)
  }

  async function runQuery(body: {
    message: string
    history: Array<{ role: string; content: string }>
    personality: string
    run_chat: boolean
    expect?: Record<string, unknown>
  }): Promise<QueryResult> {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const payload: Record<string, unknown> = {
      message: body.message,
      history: body.history,
      personality: body.personality,
      run_chat: body.run_chat,
    }
    if (body.expect && Object.keys(body.expect).length > 0) {
      payload.expect = body.expect
    }
    const res = await fetchApi('/api/eval/query', {
      method: 'POST',
      headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '运行失败'))
    return (await res.json()) as QueryResult
  }

  async function fetchDrafts(): Promise<DraftSummary[]> {
    const auth = useAuthStore()
    if (!auth.token) return []
    const res = await fetchApi('/api/eval/drafts', { headers: auth.authHeaders() })
    if (!res.ok) throw new Error(await parseApiError(res, '加载草稿失败'))
    const data = await res.json()
    return data.drafts ?? []
  }

  async function fetchDraft(draftId: string): Promise<EvalDraft> {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetchApi(`/api/eval/drafts/${encodeURIComponent(draftId)}`, {
      headers: auth.authHeaders(),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '加载草稿失败'))
    return (await res.json()) as EvalDraft
  }

  async function saveDraft(payload: {
    draft_id?: string
    title?: string
    input: Record<string, unknown>
    result: Record<string, unknown>
  }) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetchApi('/api/eval/drafts', {
      method: 'POST',
      headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '保存失败'))
    return (await res.json()) as { ok: boolean; draft_id: string; title: string }
  }

  async function deleteDraft(draftId: string) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetchApi(`/api/eval/drafts/${encodeURIComponent(draftId)}`, {
      method: 'DELETE',
      headers: auth.authHeaders(),
    })
    if (!res.ok) throw new Error(await parseApiError(res, '删除失败'))
  }

  return {
    runs,
    currentReport,
    jobStatus,
    loading,
    lastError,
    apiAvailable,
    fetchRuns,
    fetchReport,
    fetchJobStatus,
    startEval,
    seedPersona,
    fetchPersonaData,
    importReport,
    downloadReport,
    casePromptData,
    promptFromMeta,
    runQuery,
    fetchDrafts,
    fetchDraft,
    saveDraft,
    deleteDraft,
  }
})
