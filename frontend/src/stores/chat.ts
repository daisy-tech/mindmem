import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

export interface PromptData {
  memories: string[]
  system: string
}

export interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
  promptData?: PromptData
}

export interface ConversationMeta {
  id: string
  title: string
  updated_at: string
}

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

function generateId(): string {
  return `conv_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
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
    messages.value.push({ role: 'user', content })
    messages.value.push({ role: 'assistant', content: '' })
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
        // 流结束后自动保存
        saveConversation()
        return
      }
      try {
        const data = JSON.parse(e.data)
        if (data.type === 'prompt') {
          messages.value[assistantIdx].promptData = {
            memories: data.memories ?? [],
            system: data.system ?? '',
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
      if (!messages.value[assistantIdx].content) {
        messages.value[assistantIdx].content = '连接出错，请重试。'
      }
    }
  }

  async function saveConversation() {
    const auth = useAuthStore()
    if (!auth.token || messages.value.length === 0) return
    try {
      const res = await fetch(`${API_BASE}/api/conversations`, {
        method: 'POST',
        headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId.value,
          messages: messages.value.map(m => ({ role: m.role, content: m.content })),
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
    messages.value = (data.messages ?? []).map((m: { role: string; content: string }) => ({
      role: m.role,
      content: m.content,
    }))
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
