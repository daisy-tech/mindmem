import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import { API_BASE } from '../config/api'
const TOKEN_KEY = 'memobot_token'
const USER_KEY = 'memobot_user'

export interface UserInfo {
  id: string
  phone: string
  nickname: string
  created_at: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref<UserInfo | null>(
    localStorage.getItem(USER_KEY)
      ? JSON.parse(localStorage.getItem(USER_KEY) as string)
      : null,
  )

  const isAuthenticated = computed(() => !!token.value && !!user.value)

  function setAuth(t: string, u: UserInfo) {
    token.value = t
    user.value = u
    localStorage.setItem(TOKEN_KEY, t)
    localStorage.setItem(USER_KEY, JSON.stringify(u))
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    // 退出登录时清掉"人格已询问过"的标记，下次新用户进来重新弹
    localStorage.removeItem('memobot_personality_picked')
  }

  async function sendCode(phone: string): Promise<{ dev_code?: string }> {
    const res = await fetch(`${API_BASE}/api/auth/phone/send-code`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '发送失败')
    return data
  }

  async function login(phone: string, code: string) {
    const res = await fetch(`${API_BASE}/api/auth/phone/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, code }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '登录失败')
    setAuth(data.token, data.user)
    return data
  }

  function authHeaders(): Record<string, string> {
    return token.value ? { Authorization: `Bearer ${token.value}` } : {}
  }

  return {
    token,
    user,
    isAuthenticated,
    setAuth,
    logout,
    sendCode,
    login,
    authHeaders,
  }
})
