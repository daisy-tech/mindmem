<template>
  <div class="chat-view">
    <div class="chat-header">
      <h3>{{ currentTitle }}</h3>
      <div class="header-actions">
        <el-button size="small" @click="chatStore.newConversation()">新对话</el-button>
        <el-button size="small" @click="historyVisible = true">历史记录</el-button>
        <el-button size="small" @click="chatStore.newConversation()">清空</el-button>
      </div>
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
        @inspect="openPromptDrawer"
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

    <PromptDrawer
      :visible="promptVisible"
      :prompt-data="activePromptData"
      @close="promptVisible = false"
    />

    <ConversationDrawer
      :visible="historyVisible"
      @close="historyVisible = false"
      @loaded="focusInput"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import type { ElInput } from 'element-plus'
import { useChatStore } from '../stores/chat'
import type { PromptData } from '../stores/chat'
import ChatMessage from '../components/ChatMessage.vue'
import PromptDrawer from '../components/PromptDrawer.vue'
import ConversationDrawer from '../components/ConversationDrawer.vue'

const chatStore = useChatStore()
const input = ref('')
const msgRef = ref<HTMLDivElement>()
const inputRef = ref<InstanceType<typeof ElInput>>()
const promptVisible = ref(false)
const historyVisible = ref(false)
const activePromptData = ref<PromptData | undefined>()

const currentTitle = computed(() => {
  const conv = chatStore.conversations.find(c => c.id === chatStore.conversationId)
  return conv?.title ?? '新对话'
})

function focusInput() {
  nextTick(() => inputRef.value?.focus())
}

function send() {
  if (!input.value.trim() || chatStore.streaming) return
  chatStore.sendMessage(input.value)
  input.value = ''
  focusInput()
}

function openPromptDrawer(data: PromptData) {
  activePromptData.value = data
  promptVisible.value = true
}

watch(
  () => [
    chatStore.messages.length,
    chatStore.messages[chatStore.messages.length - 1]?.content,
  ],
  () => {
    nextTick(() => {
      if (msgRef.value) msgRef.value.scrollTop = msgRef.value.scrollHeight
    })
  },
)

watch(
  () => chatStore.streaming,
  (streaming, wasStreaming) => {
    if (wasStreaming && !streaming) focusInput()
  },
)

onMounted(() => {
  chatStore.fetchConversations()
})
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
.header-actions {
  display: flex;
  gap: 6px;
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
