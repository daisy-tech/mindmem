import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import MemoryView from './views/MemoryView.vue'
import EvalView from './views/EvalView.vue'
import LoginView from './views/LoginView.vue'
import { useAuthStore } from './stores/auth'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/login', component: LoginView, meta: { public: true } },
  { path: '/chat', component: ChatView },
  { path: '/memory', component: MemoryView },
  { path: '/eval', component: EvalView },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { path: '/login' }
  }
  if (to.path === '/login' && auth.isAuthenticated) {
    return { path: '/chat' }
  }
})

export default router
