/**
 * 线上聊天记录评估 store。
 *
 * 关键约束（与产品对齐）：
 * - 评估结果在服务端落盘到 backend/eval/exports/reviews/{user_id}/{conv_id}.json
 *   （NFS 共享，mac/ECS/容器同一份）
 * - 进入页面会先拉「已落盘列表」给左侧列表打✓标记
 * - 点击会话时若已落盘则自动加载（O(1) 秒出），无需用户再点按钮
 * - 用户主动点「重新评估」才 force=true 重跑并覆盖落盘
 * - 全程不调用 LLM
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

/** 服务端 /chat-audit-stored 返回的轻量摘要（列表标记用） */
export interface StoredMeta {
  conversation_id: string
  evaluated_at?: string
  exported_at?: string
  turns_total?: number
  evaluable_turns?: number
  final_ok_rate?: number
  counters: {
    final_ok: number
    final_suspicious: number
    final_bad: number
    turns_skipped: number
  }
}

// ============ store ============

export const useEvalChatStore = defineStore('evalChat', () => {
  const conversations = ref<ConvListItem[]>([])
  const loadingList = ref(false)
  const listError = ref<string | null>(null)

  const selectedId = ref<string | null>(null)
  const evaluating = ref(false)
  const evalError = ref<string | null>(null)

  /** convId → 已加载的 pack（内存缓存，避免列表来回切重复请求服务端） */
  const reportCache = ref<Map<string, AuditPack>>(new Map())

  /** convId → 服务端已落盘的轻量摘要（用于列表✓标 + 三色 mini-bar） */
  const storedMeta = ref<Map<string, StoredMeta>>(new Map())
  const loadingStored = ref(false)

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
      // 拉完列表顺便拉一次已落盘摘要，避免列表绘制时还没有徽章
      await fetchStoredMeta(true)
    } catch (e) {
      listError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loadingList.value = false
    }
  }

  async function fetchStoredMeta(force = false) {
    if (storedMeta.value.size && !force) return
    const auth = useAuthStore()
    if (!auth.token) return
    loadingStored.value = true
    try {
      const res = await fetch(`${API_BASE}/api/eval/chat-audit-stored`, {
        headers: auth.authHeaders(),
      })
      if (!res.ok) return  // 不阻塞主流程；列表还是能用，只是没徽章
      const data = (await res.json()) as { items: StoredMeta[] }
      const next = new Map<string, StoredMeta>()
      for (const m of data.items || []) {
        if (m.conversation_id) next.set(m.conversation_id, m)
      }
      storedMeta.value = next
    } catch {
      /* 静默：徽章是锦上添花，失败不影响评估本身 */
    } finally {
      loadingStored.value = false
    }
  }

  /**
   * 选中某个会话；若服务端已落盘则自动加载，无需用户再点按钮。
   * 未落盘时仅切换 selectedId，由 UI 展示「开始评估」按钮。
   */
  async function select(convId: string) {
    selectedId.value = convId
    evalError.value = null
    if (reportCache.value.has(convId)) return  // 命中内存缓存，直接显示
    if (!storedMeta.value.has(convId)) return  // 未评估过，等用户点按钮
    // 已落盘 → 静默拉取，秒出
    try {
      await evaluate(convId, { force: false, silent: true })
    } catch {
      /* 错误已写入 evalError */
    }
  }

  /**
   * 触发或读取评估。
   * - force=false 命中内存或服务端落盘则直接返回，不重跑
   * - force=true 强制服务端重评估并覆盖落盘
   * - silent=true 时不显示 loading 旋钮（auto-load 场景用）
   */
  async function evaluate(
    convId: string,
    opts: { force?: boolean; silent?: boolean } = {},
  ) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    if (!opts.force && reportCache.value.has(convId)) {
      selectedId.value = convId
      return reportCache.value.get(convId)!
    }
    if (!opts.silent) evaluating.value = true
    evalError.value = null
    try {
      const qs = opts.force ? '?force=true' : ''
      const res = await fetch(
        `${API_BASE}/api/eval/chat-audit/${encodeURIComponent(convId)}${qs}`,
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
      // 服务端写盘成功后顺手更新摘要徽章
      const review = pack.review
      if (review) {
        storedMeta.value.set(convId, {
          conversation_id: convId,
          evaluated_at: (pack as unknown as { evaluated_at?: string }).evaluated_at,
          exported_at: pack.exported_at,
          turns_total: pack.summary?.turns_total,
          evaluable_turns: review.evaluable_turns,
          final_ok_rate: review.final_ok_rate,
          counters: {
            final_ok: Number(review.counters?.final_ok ?? 0),
            final_suspicious: Number(review.counters?.final_suspicious ?? 0),
            final_bad: Number(review.counters?.final_bad ?? 0),
            turns_skipped: Number(review.counters?.turns_skipped ?? 0),
          },
        })
      }
      return pack
    } catch (e) {
      evalError.value = e instanceof Error ? e.message : String(e)
      throw e
    } finally {
      if (!opts.silent) evaluating.value = false
    }
  }

  /** 删除已落盘评估（DELETE 服务端 + 清内存）。 */
  async function deleteStored(convId: string) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const res = await fetch(
      `${API_BASE}/api/eval/chat-audit-stored/${encodeURIComponent(convId)}`,
      { method: 'DELETE', headers: auth.authHeaders() },
    )
    if (res.ok || res.status === 404) {
      storedMeta.value.delete(convId)
      reportCache.value.delete(convId)
      return true
    }
    let detail = `HTTP ${res.status}`
    try {
      const j = await res.json()
      if (typeof j.detail === 'string') detail = j.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
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
    storedMeta,
    loadingStored,
    fetchConversations,
    fetchStoredMeta,
    select,
    evaluate,
    deleteStored,
    clearCache,
    downloadCurrent,
  }
})
