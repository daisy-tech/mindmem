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

    const history = messages.value.slice(0, -2).map(m => ({
      role: m.role,
      content: m.content,
    }))

    const params = new URLSearchParams({
      message: content,
      token: auth.token,
      history: JSON.stringify(history),
    })

    const es = new EventSource(`${API_BASE}/api/chat/stream?${params}`)

    es.onmessage = (e) => {
      if (e.data === '[DONE]') {
        es.close()
        streaming.value = false
        messages.value[assistantIdx].ts = nowIso()
        saveConversation()
        return
      }
      try {
        const data = JSON.parse(e.data)
        if (data.type === 'prompt') {
          messages.value[assistantIdx].promptData = promptDataFromPayload(data)
        } else if (data.type === 'error') {
          messages.value[assistantIdx].error = data.error
          if (data.prompt_meta) {
            messages.value[assistantIdx].promptData = promptDataFromPayload(data.prompt_meta)
          }
          if (!messages.value[assistantIdx].content) {
            messages.value[assistantIdx].content = `[错误: ${data.error}]`
          }
        } else if (data.type === 'content' || data.content) {
          messages.value[assistantIdx].content += data.content
        }
      } catch {
        // ignore
      }
    }

    es.onerror = () => {
      es.close()
      streaming.value = false
      messages.value[assistantIdx].ts = nowIso()
      if (!messages.value[assistantIdx].content) {
        messages.value[assistantIdx].content = '连接出错，请重试。'
        messages.value[assistantIdx].error = 'stream_connection_error'
      }
      saveConversation()
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
    newConversation,
    clearChat,
  }
})
