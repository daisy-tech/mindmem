<template>
  <el-drawer
    :model-value="visible"
    title="历史对话"
    direction="rtl"
    size="320px"
    @close="$emit('close')"
  >
    <div class="drawer-body">
      <el-button type="primary" style="width:100%;margin-bottom:16px" @click="handleNew">
        + 新对话
      </el-button>

      <el-skeleton :rows="4" animated v-if="loading" />
      <el-empty v-else-if="conversations.length === 0" description="暂无历史对话" :image-size="60" />

      <div v-else class="conv-list">
        <div
          v-for="conv in conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: conv.id === chatStore.conversationId }"
          @click="handleLoad(conv.id)"
        >
          <div class="conv-title">{{ conv.title }}</div>
          <div class="conv-meta">
            <span class="conv-time">{{ formatTime(conv.updated_at) }}</span>
            <div class="conv-actions">
              <el-dropdown
                trigger="click"
                @command="(cmd: string) => handleDownload(conv.id, cmd)"
                @click.stop
              >
                <el-button
                  type="primary" size="small" text
                  class="action-btn"
                  :loading="downloadingId === conv.id"
                >
                  下载
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="basic">
                      仅审计包（含 prompt_meta）
                    </el-dropdown-item>
                    <el-dropdown-item command="snapshot">
                      含当前记忆快照（更大）
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button
                type="danger" size="small" text
                class="action-btn"
                @click.stop="handleDelete(conv.id)"
              >删除</el-button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { useChatStore } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; loaded: [] }>()

const chatStore = useChatStore()
const loading = ref(false)
const downloadingId = ref<string | null>(null)
const { conversations } = storeToRefs(chatStore)

async function load() {
  loading.value = true
  await chatStore.fetchConversations()
  loading.value = false
}

watch(() => props.visible, (val) => {
  if (val) load()
})

function handleNew() {
  chatStore.newConversation()
  emit('loaded')
  emit('close')
}

async function handleLoad(id: string) {
  await chatStore.loadConversation(id)
  emit('loaded')
  emit('close')
}

async function handleDelete(id: string) {
  await chatStore.deleteConversation(id)
}

async function handleDownload(id: string, mode: string) {
  downloadingId.value = id
  try {
    const audit = await chatStore.downloadConversationAudit(id, {
      includeSnapshot: mode === 'snapshot',
    })
    const total = audit?.summary?.turns_total ?? 0
    const withAudit = audit?.summary?.turns_with_audit ?? 0
    const highFailed = audit?.summary?.auto_checks?.high_severity_failed_count ?? 0
    ElMessage.success(
      `已导出 ${total} 轮（含 prompt_meta ${withAudit} 轮，high 级告警 ${highFailed} 项）`,
    )
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '导出失败')
  } finally {
    downloadingId.value = null
  }
}

function formatTime(iso: string) {
  const d = new Date(iso)
  const today = new Date()
  const isToday = d.toDateString() === today.toDateString()
  return isToday
    ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.drawer-body {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.conv-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
}

.conv-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
  border: 1px solid transparent;
}

.conv-item:hover {
  background: #f5f7fa;
}

.conv-item.active {
  background: #ecf5ff;
  border-color: #b3d8ff;
}

.conv-title {
  font-size: 14px;
  color: #303133;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conv-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.conv-time {
  font-size: 12px;
  color: #909399;
}

.conv-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}

.conv-item:hover .conv-actions {
  opacity: 1;
}

.conv-item.active .conv-actions {
  opacity: 1;
}

.action-btn {
  padding: 2px 6px;
  height: auto;
  min-height: 0;
}
</style>
