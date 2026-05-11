<template>
  <div class="memory-view">
    <div class="header">
      <h2>记忆画廊</h2>
      <el-button type="primary" @click="refresh">刷新</el-button>
    </div>
    <el-skeleton :rows="5" animated v-if="memoryStore.loading" />
    <div v-else class="memory-list">
      <el-empty v-if="memoryStore.memories.length === 0" description="暂无记忆" />
      <el-card
        v-for="mem in memoryStore.memories"
        :key="mem.id"
        class="memory-card"
        shadow="hover"
      >
        <div class="memory-content">{{ mem.memory }}</div>
        <div class="memory-meta">
          <el-tag size="small">{{ formatDate(mem.created_at) }}</el-tag>
          <el-button
            type="danger"
            size="small"
            text
            @click="memoryStore.deleteMemory(userId, mem.id)"
          >
            删除
          </el-button>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onActivated } from 'vue'
import { useMemoryStore } from '../stores/memory'

const memoryStore = useMemoryStore()
const userId = 'user_001'

function refresh() {
  memoryStore.fetchMemories(userId)
}

function formatDate(d: string) {
  return new Date(d).toLocaleString('zh-CN')
}

onMounted(() => {
  refresh()
})

onActivated(() => {
  refresh()
})
</script>

<style scoped>
.memory-view {
  padding: 24px;
  max-width: 900px;
  margin: 0 auto;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}
.memory-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.memory-card {
  border-radius: 12px;
}
.memory-content {
  font-size: 15px;
  line-height: 1.6;
  margin-bottom: 12px;
}
.memory-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
