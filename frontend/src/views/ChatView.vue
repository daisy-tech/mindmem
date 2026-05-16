<template>
  <div class="chat-view">
    <div class="chat-header">
      <h3>新对话</h3>
      <el-button size="small" @click="chatStore.clearChat()">清空</el-button>
    </div>
    <div class="messages" ref="msgRef">
      <div v-if="chatStore.messages.length === 0" class="empty">
        <img src="/memobot-avatar.png" class="empty-avatar" alt="MemoBot" />
        <h1>MemoBot</h1>
        <p>我是拥有长期记忆的 AI 伴侣，越聊越懂你。</p>
      </div>
      <ChatMessage
        v-for="(msg, i) in chatStore.messages"
        :key="i"
        :message="msg"
      />
    </div>
    <div class="input-area">
      <el-input
        ref="inputRef"
        v-model="input"
        placeholder="输入消息..."
        size="large"
        @keyup.enter="send"
      >
        <template #append>
          <el-button
            type="primary"
            :loading="chatStore.streaming"
            @mousedown.prevent
            @click="send"
          >
            发送
          </el-button>
        </template>
      </el-input>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import type { ElInput } from 'element-plus'
import { useChatStore } from '../stores/chat'
import ChatMessage from '../components/ChatMessage.vue'

const chatStore = useChatStore()
const input = ref('')
const msgRef = ref<HTMLDivElement>()
const inputRef = ref<InstanceType<typeof ElInput>>()

function focusInput() {
  nextTick(() => inputRef.value?.focus())
}

function send() {
  if (!input.value.trim() || chatStore.streaming) return
  chatStore.sendMessage(input.value)
  input.value = ''
  focusInput()
}

watch(
  () => [
    chatStore.messages.length,
    chatStore.messages[chatStore.messages.length - 1]?.content,
  ],
  () => {
    nextTick(() => {
      if (msgRef.value) {
        msgRef.value.scrollTop = msgRef.value.scrollHeight
      }
    })
  },
)

watch(
  () => chatStore.streaming,
  (streaming, wasStreaming) => {
    if (wasStreaming && !streaming) focusInput()
  },
)
</script>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}
.empty {
  text-align: center;
  margin-top: 15vh;
  color: #999;
}
.empty-avatar {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  object-fit: cover;
  box-shadow: 0 4px 20px rgba(167, 139, 250, 0.25);
  margin-bottom: 16px;
}
.input-area {
  padding: 16px 24px;
  background: #fff;
  border-top: 1px solid #e4e7ed;
}
</style>
