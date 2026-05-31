<template>
  <el-drawer
    :model-value="visible"
    title="本次对话的 Prompt 详情"
    direction="rtl"
    size="480px"
    @close="$emit('close')"
  >
    <PromptChainPanel :prompt-data="promptData" />
  </el-drawer>
</template>

<script setup lang="ts">
import PromptChainPanel from './PromptChainPanel.vue'
import type { PromptData } from '../stores/chat'

defineProps<{
  visible: boolean
  promptData?: PromptData
}>()
defineEmits<{ close: [] }>()
</script>

<style scoped>
:deep(.chain-panel) {
  padding: 4px 0;
}
</style>
