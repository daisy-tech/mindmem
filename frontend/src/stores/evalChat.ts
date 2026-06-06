/**
 * 线上聊天记录评估 store。
 *
 * 关键约束（与产品对齐）：
 * - 不自动评估，只有点「开始评估」时才 GET /api/eval/chat-audit/{id}
 * - 评过的会话缓存在内存，切走再回来不再重复请求
 * - 不调用 LLM，全部走 audit 包 + 服务端 L1 规则
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import { API_BASE } from '../config/api'
import { useAuthStore } from './auth'

// ============ 类型 ============

export interface ConvListItem {
  id: string
  title: string
  updated_at: string
}

export interface AuditMemory {
  source: string
  text: string
  usage: string
  reason?: string
  score?: number
  meta?: Record<string, unknown>
}

export interface AuditContextLayers {
  stable_profile?: AuditMemory[]
  relevant_relationships?: AuditMemory[]
  relevant_events?: AuditMemory[]
  relevant_memories?: AuditMemory[]
  background_only?: AuditMemory[]
}

export interface AuditRoute {
  intent?: string
  intent_source?: string
  intent_confidence?: number | null
  low_confidence?: boolean
  memory_depth?: string
  sensitive_mode?: boolean
  max_explicit_memories?: number
  load_layers?: string[]
  event_policy?: string
  router_version?: string
  reasons?: string[]
}

export interface AuditPromptMeta {
  route?: AuditRoute
  context_layers?: AuditContextLayers
  activated?: AuditMemory[]
  system?: string
  llm_request?: { model: string; messages: Array<{ role: string; content: string }> }
  snapshot_stats?: Record<string, unknown>
  model?: string
  composed_at?: string
}

export interface ConsistencyCheck {
  id: string
  pass: boolean
  severity: 'high' | 'medium' | string
  detail?: string
}

export interface DerivedBlock {
  pool_stats?: Record<string, unknown>
  activation_stats?: Record<string, unknown>
  activation_trace?: Record<string, unknown>
  previous_intent?: string | null
  consistency_checks?: ConsistencyCheck[]
}

export interface AuditTurn {
  turn_id: string | null
  index: number
  input: {
    user_message: string
    history_before?: Array<{ role: string; content: string }>
    history_turns?: number
  }
  output: {
    assistant_reply: string
    error?: string | null
    ts?: string | null
  }
  audit: {
    available: boolean
    missing_reason?: string | null
    prompt_meta?: AuditPromptMeta
    derived?: DerivedBlock
  }
  evaluation?: Record<string, unknown>
  review?: TurnReview
}

export interface L1Rule {
  id: string
  status: 'pass' | 'suspicious' | 'fail' | 'skip'
  severity: string
  detail?: string
  attribution?: string[]
}

export interface TurnReview {
  l0_status: 'pass' | 'warn' | 'fail' | 'skip'
  l0_high_fail: number
  l0_medium_fail: number
  l1_status: 'ok' | 'suspicious' | 'bad' | 'skip'
  rules: L1Rule[]
  suggested_root_cause: string[]
  final_status: 'ok' | 'suspicious' | 'bad' | 'skip'
  snapshot_status: 'at_turn' | 'context_only' | 'unavailable'
  missing_reason?: string
}

export interface AuditPack {
  schema: string
  exported_at: string
  conversation: {
    id: string
    title: string
    user_id: string
    created_at?: string | null
    updated_at?: string | null
    message_count: number
    turn_count: number
  }
  environment?: Record<string, unknown>
  memory_snapshot?: Record<string, unknown> | null
  turns: AuditTurn[]
  summary: {
    turns_total: number
    turns_with_audit: number
    turns_missing_audit: number
    auto_checks: {
      by_check_id: Record<string, { failed: number; total: number }>
      high_severity_failed_count: number
      turns_with_failures: string[]
    }
  }
  review?: ReviewSummary
}

export interface ReviewSummary {
  schema: string
  counters: Record<string, number>
  structure_pass_rate: number
  final_ok_rate: number
  rule_stats: Record<string, Record<string, number>>
  check_stats: Record<string, { failed: number; total: number }>
  root_cause_top: Array<[string, number]>
  evaluable_turns: number
}

// ============ store ============

export const useEvalChatStore = defineStore('evalChat', () => {
  const conversations = ref<ConvListItem[]>([])
  const loadingList = ref(false)
  const listError = ref<string | null>(null)

  const selectedId = ref<string | null>(null)
  const evaluating = ref(false)
  const evalError = ref<string | null>(null)

  /** convId → 已评估的 pack（内存缓存，刷新页面会丢，这是预期行为） */
  const reportCache = ref<Map<string, AuditPack>>(new Map())

  const currentReport = computed<AuditPack | null>(() => {
    if (!selectedId.value) return null
    return reportCache.value.get(selectedId.value) || null
  })

  async function fetchConversations(force = false) {
    if (conversations.value.length && !force) return
    const auth = useAuthStore()
    if (!auth.token) return
    loadingList.value = true
    listError.value = null
    try {
      const res = await fetch(`${API_BASE}/api/conversations`, {
        headers: auth.authHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      conversations.value = await res.json()
    } catch (e) {
      listError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loadingList.value = false
    }
  }

  function select(convId: string) {
    selectedId.value = convId
    evalError.value = null
  }

  async function evaluate(convId: string, opts: { force?: boolean } = {}) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    if (!opts.force && reportCache.value.has(convId)) {
      selectedId.value = convId
      return reportCache.value.get(convId)!
    }
    evaluating.value = true
    evalError.value = null
    try {
      const res = await fetch(
        `${API_BASE}/api/eval/chat-audit/${encodeURIComponent(convId)}`,
        { headers: auth.authHeaders() },
      )
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const j = await res.json()
          if (typeof j.detail === 'string') detail = j.detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      const pack = (await res.json()) as AuditPack
      reportCache.value.set(convId, pack)
      selectedId.value = convId
      return pack
    } catch (e) {
      evalError.value = e instanceof Error ? e.message : String(e)
      throw e
    } finally {
      evaluating.value = false
    }
  }

  function clearCache(convId?: string) {
    if (convId) reportCache.value.delete(convId)
    else reportCache.value.clear()
  }

  function downloadCurrent() {
    const pack = currentReport.value
    if (!pack) return
    const blob = new Blob([JSON.stringify(pack, null, 2)], {
      type: 'application/json;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const ts = new Date()
      .toISOString()
      .replace(/[-:]/g, '')
      .slice(0, 15)
      .replace('T', '_')
    a.href = url
    a.download = `${pack.conversation.id}_${ts}_eval_review.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return {
    conversations,
    loadingList,
    listError,
    selectedId,
    evaluating,
    evalError,
    currentReport,
    fetchConversations,
    select,
    evaluate,
    clearCache,
    downloadCurrent,
  }
})
