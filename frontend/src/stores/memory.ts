import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export interface MemoryItem {
  id: string
  memory: string
  created_at: string
  updated_at?: string
}

export const useMemoryStore = defineStore('memory', () => {
  const memories = ref<MemoryItem[]>([])
  const loading = ref(false)

  async function fetchMemories() {
    const auth = useAuthStore()
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/api/memory`, {
        headers: auth.authHeaders(),
      })
      if (res.status === 401) {
        auth.logout()
        return
      }
      const data = await res.json()
      memories.value = data.results || data.memories || []
    } finally {
      loading.value = false
    }
  }

  async function deleteMemory(memoryId: string) {
    const auth = useAuthStore()
    await fetch(`${API_BASE}/api/memory/${memoryId}`, {
      method: 'DELETE',
      headers: auth.authHeaders(),
    })
    memories.value = memories.value.filter((m) => m.id !== memoryId)
  }

  return {
    memories,
    loading,
    fetchMemories,
    deleteMemory,
  }
})
