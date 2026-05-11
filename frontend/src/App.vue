<template>
  <div class="app-container">
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
        </el-menu>
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
import { useRoute } from 'vue-router'
import { ChatDotRound, Collection } from '@element-plus/icons-vue'

const route = useRoute()
const activeMenu = computed(() => route.path)
</script>

<style scoped>
.app-container {
  height: 100vh;
}
.sidebar {
  background: #1a1a2e;
  color: #fff;
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
.main-content {
  padding: 0;
  background: #f5f7fa;
}
</style>
