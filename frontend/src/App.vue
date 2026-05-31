<template>
  <router-view v-if="isLoginRoute" />
  <div v-else class="app-container">
    <el-container style="height: 100vh;">
      <el-aside width="220px" class="sidebar">
        <div class="logo">
          <img src="/memobot-avatar.png" class="logo-avatar" alt="MemoBot" />
          <h2>MemoBot</h2>
          <p>AI 记忆伴侣</p>
        </div>
        <el-menu
          :default-active="activeMenu"
          router
          background-color="#1a1a2e"
          text-color="#cfcfe0"
          active-text-color="#a78bfa"
          class="menu"
        >
          <el-menu-item index="/chat">
            <el-icon><ChatDotRound /></el-icon>
            <span>对话</span>
          </el-menu-item>
          <el-menu-item index="/memory">
            <el-icon><Collection /></el-icon>
            <span>记忆画廊</span>
          </el-menu-item>
          <el-menu-item index="/eval">
            <el-icon><DataAnalysis /></el-icon>
            <span>评测实验室</span>
          </el-menu-item>
        </el-menu>
        <div class="user-panel" v-if="auth.user">
          <div class="user-info">
            <el-avatar :size="36" class="user-avatar">
              {{ (auth.user.nickname || auth.user.phone).slice(0, 1) }}
            </el-avatar>
            <div class="user-meta">
              <div class="user-name">{{ auth.user.nickname }}</div>
              <div class="user-phone">{{ maskPhone(auth.user.phone) }}</div>
            </div>
          </div>
          <el-button
            size="small"
            link
            class="logout-btn"
            @click="onLogout"
          >
            <el-icon><SwitchButton /></el-icon>
            退出登录
          </el-button>
        </div>
      </el-aside>
      <el-main class="main-content">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChatDotRound, Collection, SwitchButton, DataAnalysis } from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from './stores/auth'
import { useChatStore } from './stores/chat'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const activeMenu = computed(() => route.path)
const isLoginRoute = computed(() => route.path === '/login')

function maskPhone(p: string) {
  return p ? `${p.slice(0, 3)}****${p.slice(7)}` : ''
}

async function onLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      type: 'warning',
    })
  } catch {
    return
  }
  // 清空当前会话上下文
  useChatStore().clearChat()
  auth.logout()
  router.replace('/login')
}
</script>

<style scoped>
.app-container {
  height: 100vh;
}
.sidebar {
  background: #1a1a2e;
  color: #fff;
  display: flex;
  flex-direction: column;
}
.logo {
  padding: 24px;
  text-align: center;
  border-bottom: 1px solid #2d2d44;
}
.logo-avatar {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  object-fit: cover;
  margin-bottom: 10px;
  box-shadow: 0 2px 12px rgba(167, 139, 250, 0.35);
}
.logo h2 {
  margin: 0;
  font-size: 22px;
  color: #a78bfa;
}
.logo p {
  margin: 4px 0 0;
  font-size: 12px;
  color: #888;
}
.menu {
  border: none;
  flex: 1;
}
.menu :deep(.el-menu-item) {
  color: #cfcfe0;
}
.menu :deep(.el-menu-item:hover) {
  background-color: #2d2d44 !important;
  color: #fff;
}
.menu :deep(.el-menu-item.is-active) {
  background-color: #2d2d44 !important;
  color: #a78bfa;
}
.user-panel {
  padding: 14px 16px;
  border-top: 1px solid #2d2d44;
  background: #14142a;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.user-avatar {
  background: #a78bfa;
  color: #fff;
  font-weight: 600;
}
.user-meta {
  overflow: hidden;
}
.user-name {
  font-size: 13px;
  color: #e3e3f0;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.user-phone {
  font-size: 11px;
  color: #888;
  margin-top: 2px;
}
.logout-btn {
  color: #aaa;
  font-size: 12px;
}
.logout-btn:hover {
  color: #a78bfa;
}
.main-content {
  padding: 0;
  background: #f5f7fa;
}
</style>
