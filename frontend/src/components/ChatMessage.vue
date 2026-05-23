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
    <div
      class="bubble"
      :class="{ 'has-prompt': message.role === 'assistant' && message.promptData }"
      @click="message.role === 'assistant' && message.promptData && $emit('inspect', message.promptData)"
    >
      <div class="content">{{ message.content }}</div>
      <div
        v-if="message.role === 'assistant' && message.promptData"
        class="inspect-hint"
      >
        <el-icon><Search /></el-icon> 查看 Prompt
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { User, Search } from '@element-plus/icons-vue'
import type { Message, PromptData } from '../stores/chat'

defineProps<{
  message: Message
}>()
defineEmits<{ inspect: [data: PromptData] }>()
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
.bubble.has-prompt {
  cursor: pointer;
  transition: box-shadow 0.15s;
}
.bubble.has-prompt:hover {
  box-shadow: 0 2px 10px rgba(64, 158, 255, 0.25);
}
.content {
  white-space: pre-wrap;
  line-height: 1.6;
}
.inspect-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  font-size: 11px;
  color: #c0c4cc;
  opacity: 0;
  transition: opacity 0.15s;
}
.bubble.has-prompt:hover .inspect-hint {
  opacity: 1;
  color: #409eff;
}
</style>
