import { defineStore } from 'pinia'
import { ref } from 'vue'

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

  async function fetchMemories(userId: string) {
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/api/memory/${userId}`)
      const data = await res.json()
      memories.value = data.results || data.memories || []
    } finally {
      loading.value = false
    }
  }

  async function deleteMemory(userId: string, memoryId: string) {
    await fetch(`${API_BASE}/api/memory/${userId}/${memoryId}`, {
      method: 'DELETE',
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
