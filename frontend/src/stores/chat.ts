import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
}

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export const useChatStore = defineStore('chat', () => {
  const messages = ref<Message[]>([])
  const streaming = ref(false)
  const userId = ref('user_001')

  async function sendMessage(content: string) {
    if (!content.trim() || streaming.value) return
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
      user_id: userId.value,
      history: JSON.stringify(history),
    })

    const es = new EventSource(`${API_BASE}/api/chat/stream?${params}`)

    es.onmessage = (e) => {
      if (e.data === '[DONE]') {
        es.close()
        streaming.value = false
        return
      }
      try {
        const data = JSON.parse(e.data)
        if (data.content) {
          messages.value[assistantIdx].content += data.content
        }
      } catch {
        // ignore parse errors
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

  function clearChat() {
    messages.value = []
  }

  return {
    messages,
    streaming,
    userId,
    sendMessage,
    clearChat,
  }
})
