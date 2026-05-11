import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import MemoryView from './views/MemoryView.vue'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', component: ChatView },
  { path: '/memory', component: MemoryView },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
