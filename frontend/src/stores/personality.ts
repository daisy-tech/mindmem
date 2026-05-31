import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

import { API_BASE } from '../config/api'

export type PersonalityValue = 'introvert' | 'balanced' | 'extrovert'

export interface PersonalityOption {
  value: PersonalityValue
  label: string
  description: string
  max_explicit_memories: number
  allow_casual_memory: boolean
  plan_followup: string
  pain_point_policy: string
  question_style: string
}

export interface PersonalityResponse {
  personality: PersonalityValue
  default: PersonalityValue
  options: PersonalityOption[]
  config: Partial<PersonalityOption>
}

export const usePersonalityStore = defineStore('personality', () => {
  const personality = ref<PersonalityValue>('balanced')
  const defaultPersonality = ref<PersonalityValue>('balanced')
  const options = ref<PersonalityOption[]>([])
  const loading = ref(false)
  const loaded = ref(false)

  async function fetchPersonality() {
    const auth = useAuthStore()
    if (!auth.token) return
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/api/profile/personality`, {
        headers: auth.authHeaders(),
      })
      if (res.status === 401) {
        auth.logout()
        return
      }
      if (!res.ok) return
      const data = (await res.json()) as PersonalityResponse
      personality.value = data.personality
      defaultPersonality.value = data.default
      options.value = data.options ?? []
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function setPersonality(value: PersonalityValue) {
    const auth = useAuthStore()
    if (!auth.token) return
    const res = await fetch(`${API_BASE}/api/profile/personality`, {
      method: 'POST',
      headers: { ...auth.authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ personality: value }),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || '保存失败')
    }
    personality.value = value
  }

  function currentOption(): PersonalityOption | null {
    return options.value.find((o) => o.value === personality.value) ?? null
  }

  function reset() {
    personality.value = 'balanced'
    defaultPersonality.value = 'balanced'
    options.value = []
    loaded.value = false
  }

  return {
    personality,
    defaultPersonality,
    options,
    loading,
    loaded,
    fetchPersonality,
    setPersonality,
    currentOption,
    reset,
  }
})
