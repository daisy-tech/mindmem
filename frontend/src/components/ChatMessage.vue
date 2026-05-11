<template>
  <div :class="['message-row', message.role]">
    <div class="avatar">
      <el-avatar
        v-if="message.role === 'user'"
        :size="40"
        :icon="User"
      />
      <el-avatar
        v-else
        :size="40"
        src="/memobot-avatar.png"
      />
    </div>
    <div class="bubble">
      <div class="content">{{ message.content }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { User } from '@element-plus/icons-vue'
import type { Message } from '../stores/chat'

defineProps<{
  message: Message
}>()
</script>

<style scoped>
.message-row {
  display: flex;
  gap: 12px;
  margin: 16px 0;
  align-items: flex-start;
}
.message-row.user {
  flex-direction: row-reverse;
}
.avatar {
  flex-shrink: 0;
}
.bubble {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
.message-row.user .bubble {
  background: #a78bfa;
  color: #fff;
}
.content {
  white-space: pre-wrap;
  line-height: 1.6;
}
</style>
