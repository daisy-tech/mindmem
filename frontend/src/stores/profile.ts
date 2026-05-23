import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export interface ProfileField {
  value: unknown
  confidence: number
  updated_at: string
}

export interface UserProfile {
  profile?: Record<string, Record<string, ProfileField>>
  last_updated?: string
}

export interface ConflictLog {
  id: number
  field: string
  field_label: string
  action: string
  action_label: string
  old_value: unknown
  new_value: unknown
  created_at: string
}

export const useProfileStore = defineStore('profile', () => {
  const profile = ref<UserProfile>({})
  const loading = ref(false)
  const conflictLog = ref<ConflictLog[]>([])
  const logLoading = ref(false)

  async function fetchProfile() {
    const auth = useAuthStore()
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/api/profile`, {
        headers: auth.authHeaders(),
      })
      if (res.status === 401) { auth.logout(); return }
      profile.value = await res.json()
    } finally {
      loading.value = false
    }
  }

  async function fetchConflictLog() {
    const auth = useAuthStore()
    logLoading.value = true
    try {
      const res = await fetch(`${API_BASE}/api/profile/conflict-log?limit=50`, {
        headers: auth.authHeaders(),
      })
      if (res.status === 401) { auth.logout(); return }
      conflictLog.value = await res.json()
    } finally {
      logLoading.value = false
    }
  }

  async function deleteField(path: string) {
    const auth = useAuthStore()
    await fetch(`${API_BASE}/api/profile/field?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
      headers: auth.authHeaders(),
    })
    await fetchProfile()
  }

  return { profile, loading, conflictLog, logLoading, fetchProfile, fetchConflictLog, deleteField }
})
