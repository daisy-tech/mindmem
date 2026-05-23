import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export interface EventItem {
  event_id: string
  event_type: string
  type_label: string
  type_color: string
  summary: string
  details: Record<string, unknown>
  occurred_at: string | null
  detected_at: string
  importance: number
  status: string
  mention_count: number
}

export const useEventStore = defineStore('events', () => {
  const events = ref<EventItem[]>([])
  const loading = ref(false)

  async function fetchEvents(eventType?: string) {
    const auth = useAuthStore()
    loading.value = true
    try {
      const params = new URLSearchParams({ status: 'active', limit: '200' })
      if (eventType) params.set('event_type', eventType)
      const res = await fetch(`${API_BASE}/api/events?${params}`, {
        headers: auth.authHeaders(),
      })
      if (res.status === 401) { auth.logout(); return }
      events.value = await res.json()
    } finally {
      loading.value = false
    }
  }

  async function deleteEvent(eventId: string) {
    const auth = useAuthStore()
    await fetch(`${API_BASE}/api/events/${eventId}`, {
      method: 'DELETE',
      headers: auth.authHeaders(),
    })
    events.value = events.value.filter(e => e.event_id !== eventId)
  }

  async function archiveEvent(eventId: string) {
    const auth = useAuthStore()
    await fetch(`${API_BASE}/api/events/${eventId}/archive`, {
      method: 'PATCH',
      headers: auth.authHeaders(),
    })
    events.value = events.value.filter(e => e.event_id !== eventId)
  }

  return { events, loading, fetchEvents, deleteEvent, archiveEvent }
})
