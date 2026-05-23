<template>
  <el-drawer
    :model-value="visible"
    title="本次对话的 Prompt 详情"
    direction="rtl"
    size="420px"
    @close="$emit('close')"
  >
    <div v-if="promptData" class="drawer-body">
      <!-- 激活的记忆 -->
      <div class="section">
        <div class="section-title">
          <el-icon><Collection /></el-icon>
          激活的记忆
          <el-tag size="small" type="info" style="margin-left:6px">{{ promptData.memories.length }} 条</el-tag>
        </div>
        <el-empty v-if="promptData.memories.length === 0" description="无相关记忆" :image-size="48" />
        <div v-else class="memory-list">
          <div v-for="(mem, i) in promptData.memories" :key="i" class="memory-item">
            <div class="memory-index">{{ i + 1 }}</div>
            <div class="memory-body">
              <div class="memory-text">{{ mem }}</div>
              <div class="memory-tags">
                <el-tag
                  v-for="tag in getMemTags(mem)"
                  :key="tag.name"
                  :type="tag.type"
                  size="small"
                  style="margin-right:4px"
                >{{ tag.name }}</el-tag>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- System Prompt -->
      <div class="section">
        <div class="section-title">
          <el-icon><Document /></el-icon>
          完整 System Prompt
        </div>
        <el-collapse>
          <el-collapse-item title="点击展开查看" name="1">
            <pre class="system-prompt">{{ promptData.system }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>
    <el-empty v-else description="暂无数据" />
  </el-drawer>
</template>

<script setup lang="ts">
import { Collection, Document } from '@element-plus/icons-vue'
import type { PromptData } from '../stores/chat'
import { extractTags, type MemoryTag } from '../utils/memoryTagger'

defineProps<{
  visible: boolean
  promptData?: PromptData
}>()
defineEmits<{ close: [] }>()

function getMemTags(text: string): MemoryTag[] {
  return extractTags(text)
}
</script>

<style scoped>
.drawer-body {
  display: flex;
  flex-direction: column;
  gap: 24px;
  padding: 4px 0;
}

.section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.memory-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.memory-item {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.memory-index {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #409eff;
  color: #fff;
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
}

.memory-body {
  flex: 1;
  background: #f5f7fa;
  border-radius: 8px;
  padding: 8px 12px;
}

.memory-text {
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
  margin-bottom: 6px;
}

.system-prompt {
  font-size: 12px;
  line-height: 1.7;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px;
  margin: 0;
}
</style>
