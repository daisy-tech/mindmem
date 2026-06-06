import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

export interface ActivatedMemory {
  source: string
  text: string
  usage: string
  reason: string
  score: number
  meta?: Record<string, unknown>
}

export interface MemoryRouteInfo {
  intent: string
  memory_depth: string
  load_layers?: string[]
  personality: string
  personality_label?: string
  sensitive_mode?: boolean
  max_explicit_memories?: number
  event_policy?: string
  inferred_subjects?: string[]
  reasons?: string[]
  query?: string
  intent_confidence?: number | null
  intent_source?: string
  low_confidence?: boolean
  router_version?: string
}

export interface ContextLayers {
  stable_profile?: ActivatedMemory[]
  relevant_relationships?: ActivatedMemory[]
  relevant_events?: ActivatedMemory[]
  relevant_memories?: ActivatedMemory[]
  background_only?: ActivatedMemory[]
}

export interface PromptData {
  version?: string
  memories: string[]
  system: string
  route?: MemoryRouteInfo
  activated?: ActivatedMemory[]
  context_layers?: ContextLayers
  model?: string
  composed_at?: string
  trigger_message?: string
  history_turns?: number
  llm_request?: {
    model: string
    messages: Array<{ role: string; content: string }>
  }
}

export interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
  promptData?: PromptData
  ts?: string
  turn_id?: string
  error?: string
}

export interface ConversationMeta {
  id: string
  title: string
  updated_at: string
}

interface StoredMessage {
  role: string
  content: string
  ts?: string
  turn_id?: string
  prompt_meta?: PromptData
  error?: string
}

import { API_BASE } from '../config/api'

function generateId(): string {
  return `conv_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

function generateTurnId(): string {
  return `turn_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

function nowIso(): string {
  return new Date().toISOString()
}

function promptDataFromPayload(data: Record<string, unknown>): PromptData {
  return {
    version: data.version as string | undefined,
    memories: (data.memories as string[]) ?? [],
    system: (data.system as string) ?? '',
    route: data.route as MemoryRouteInfo | undefined,
    activated: data.activated as ActivatedMemory[] | undefined,
    context_layers: data.context_layers as ContextLayers | undefined,
    model: data.model as string | undefined,
    composed_at: data.composed_at as string | undefined,
    trigger_message: data.trigger_message as string | undefined,
    history_turns: data.history_turns as number | undefined,
    llm_request: data.llm_request as PromptData['llm_request'],
  }
}

function serializeMessage(m: Message): StoredMessage {
  const item: StoredMessage = {
    role: m.role,
    content: m.content,
  }
  if (m.ts) item.ts = m.ts
  if (m.turn_id) item.turn_id = m.turn_id
  if (m.error) item.error = m.error
  if (m.role === 'assistant' && m.promptData) {
    item.prompt_meta = m.promptData
  }
  return item
}

function deserializeMessage(m: StoredMessage): Message {
  return {
    role: m.role as Message['role'],
    content: m.content,
    ts: m.ts,
    turn_id: m.turn_id,
    error: m.error,
    promptData: m.prompt_meta ? promptDataFromPayload(m.prompt_meta as unknown as Record<string, unknown>) : undefined,
  }
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<Message[]>([])
  const streaming = ref(false)
  const conversationId = ref<string>(generateId())
  const conversations = ref<ConversationMeta[]>([])

  async function sendMessage(content: string) {
    if (!content.trim() || streaming.value) return
    const auth = useAuthStore()
    if (!auth.token) return

    streaming.value = true
    const turnId = generateTurnId()
    const sentAt = nowIso()
    messages.value.push({ role: 'user', content, ts: sentAt, turn_id: turnId })
    messages.value.push({ role: 'assistant', content: '', turn_id: turnId })
    const assistantIdx = messages.value.length - 1

    const history = messages.value.slice(0, -2).map(m => {
      const item: { role: string; content: string; prompt_meta?: { route?: MemoryRouteInfo } } = {
        role: m.role,
        content: m.content,
      }
      // 带上 assistant 上一轮的 route，让后端 Router v1.5 能继承 intent
      if (m.role === 'assistant' && m.promptData?.route) {
        item.prompt_meta = { route: m.promptData.route }
      }
      return item
    })

    const finalize = () => {
      streaming.value = false
      messages.value[assistantIdx].ts = nowIso()
      saveConversation()
    }

    // 处理单条 SSE 帧（data: ...\n\n）的数据负载
    const handleData = (raw: string) => {
      const data = raw.trim()
      if (!data) return
      if (data === '[DONE]') return
      try {
        const obj = JSON.parse(data)
        if (obj.type === 'prompt') {
          messages.value[assistantIdx].promptData = promptDataFromPayload(obj)
        } else if (obj.type === 'error') {
          messages.value[assistantIdx].error = obj.error
          if (obj.prompt_meta) {
            messages.value[assistantIdx].promptData = promptDataFromPayload(obj.prompt_meta)
          }
          if (!messages.value[assistantIdx].content) {
            messages.value[assistantIdx].content = `[错误: ${obj.error}]`
          }
        } else if (obj.type === 'content' || obj.content) {
          messages.value[assistantIdx].content += obj.content
        }
      } catch {
        /* 非 JSON 帧（心跳/注释）直接忽略 */
      }
    }

    try {
      // 改用 POST + fetch 流式读取：
      // 1. EventSource 只能 GET 把整个 history 塞 URL，到 7+ 轮就会撞 uvicorn/h11 的请求行上限被踢
      // 2. POST body 不受 URL 长度限制，token 也能用 Authorization header
      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: 'POST',
        headers: {
          ...auth.authHeaders(),
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({ message: content, history }),
      })

      if (!res.ok || !res.body) {
        let detail = `HTTP ${res.status}`
        try {
          const errBody = await res.text()
          if (errBody) detail += `: ${errBody.slice(0, 200)}`
        } catch {
          /* ignore */
        }
        messages.value[assistantIdx].error = `stream_http_${res.status}`
        if (!messages.value[assistantIdx].content) {
          messages.value[assistantIdx].content = `[连接失败: ${detail}]`
        }
        finalize()
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      // 按 SSE 规范：事件之间用 \n\n 分隔，事件内多行以 `data: ` / `event: ` 起头
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let sepIdx: number
        while ((sepIdx = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, sepIdx)
          buffer = buffer.slice(sepIdx + 2)
          // 一个 frame 可能含多行；只取 data: 开头那几行拼起来
          const dataLines = frame
            .split('\n')
            .filter(l => l.startsWith('data:'))
            .map(l => l.slice(5).replace(/^ /, ''))
          if (dataLines.length > 0) {
            handleData(dataLines.join('\n'))
          }
        }
      }
      // flush 残留（一般是 [DONE] 之后 buffer 已空）
      if (buffer.trim()) {
        const dataLines = buffer
          .split('\n')
          .filter(l => l.startsWith('data:'))
          .map(l => l.slice(5).replace(/^ /, ''))
        if (dataLines.length > 0) handleData(dataLines.join('\n'))
      }
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err)
      messages.value[assistantIdx].error = 'stream_network_error'
      if (!messages.value[assistantIdx].content) {
        messages.value[assistantIdx].content = `[连接中断: ${reason}]`
      }
    } finally {
      finalize()
    }
  }

  async function saveConversation() {
    const auth = useAuthStore()
    if (!auth.token || messages.value.length === 0) return
    try {
      const storable = messages.value
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(serializeMessage)
      const res = await fetch(`${API_BASE}/api/conversations`, {
        method: 'POST',
        headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId.value,
          messages: storable,
        }),
      })
      if (!res.ok) {
        console.warn('[saveConversation] failed:', res.status, await res.text())
        return
      }
      fetchConversations()
    } catch (e) {
      console.warn('[saveConversation] error:', e)
    }
  }

  async function fetchConversations() {
    const auth = useAuthStore()
    if (!auth.token) return
    try {
      const res = await fetch(`${API_BASE}/api/conversations`, {
        headers: auth.authHeaders(),
      })
      conversations.value = await res.json()
    } catch {
      //
    }
  }

  async function loadConversation(id: string) {
    const auth = useAuthStore()
    const res = await fetch(`${API_BASE}/api/conversations/${id}`, {
      headers: auth.authHeaders(),
    })
    const data = await res.json()
    conversationId.value = id
    messages.value = (data.messages ?? []).map((m: StoredMessage) => deserializeMessage(m))
  }

  async function deleteConversation(id: string) {
    const auth = useAuthStore()
    await fetch(`${API_BASE}/api/conversations/${id}`, {
      method: 'DELETE',
      headers: auth.authHeaders(),
    })
    conversations.value = conversations.value.filter(c => c.id !== id)
    if (conversationId.value === id) {
      newConversation()
    }
  }

  async function downloadConversationAudit(
    id: string,
    opts: { includeSnapshot?: boolean } = {},
  ) {
    const auth = useAuthStore()
    if (!auth.token) throw new Error('未登录')
    const qs = opts.includeSnapshot ? '?include_snapshot=true' : ''
    const res = await fetch(`${API_BASE}/api/conversations/${id}/export${qs}`, {
      headers: auth.authHeaders(),
    })
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const data = await res.json()
        if (typeof data.detail === 'string') detail = data.detail
      } catch {
        /* ignore */
      }
      throw new Error(`导出失败：${detail}`)
    }
    const audit = await res.json()

    const blob = new Blob([JSON.stringify(audit, null, 2)], {
      type: 'application/json;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '_')
    a.href = url
    a.download = `${id}_${ts}_chat_audit.json`
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    return audit
  }

  function newConversation() {
    messages.value = []
    conversationId.value = generateId()
  }

  function clearChat() {
    messages.value = []
  }

  return {
    messages,
    streaming,
    conversationId,
    conversations,
    sendMessage,
    saveConversation,
    fetchConversations,
    loadConversation,
    deleteConversation,
    downloadConversationAudit,
    newConversation,
    clearChat,
  }
})
