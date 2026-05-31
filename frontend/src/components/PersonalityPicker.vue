<template>
  <el-dialog
    :model-value="visible"
    title="选择 MemoBot 的性格"
    width="600px"
    align-center
    @close="$emit('close')"
  >
    <p class="picker-intro">
      性格决定 MemoBot 如何使用你的记忆：要不要主动跟进、相关时引用多少、对敏感话题的边界感等。<br>
      可以随时在聊天页右上角"性格"按钮里修改，对历史记忆不会有任何影响。
    </p>

    <div class="cards">
      <div
        v-for="opt in store.options"
        :key="opt.value"
        class="card"
        :class="{ active: selected === opt.value }"
        @click="selected = opt.value"
      >
        <div class="card-header">
          <span class="card-name">{{ opt.label }}</span>
          <el-tag
            v-if="opt.value === store.defaultPersonality"
            size="small"
            type="info"
            effect="plain"
            style="margin-left:6px"
          >
            推荐
          </el-tag>
          <el-tag
            v-if="opt.value === store.personality"
            size="small"
            type="success"
            effect="plain"
            style="margin-left:6px"
          >
            当前
          </el-tag>
        </div>
        <div class="card-desc">{{ opt.description }}</div>
        <div class="card-meta">
          <div>· 每轮显性引用记忆：最多 {{ opt.max_explicit_memories }} 条</div>
          <div>· 普通问候带旧记忆：{{ opt.allow_casual_memory ? '是' : '否' }}</div>
          <div>· 计划主动跟进：{{ planLabel(opt.plan_followup) }}</div>
          <div>· 痛点策略：{{ painLabel(opt.pain_point_policy) }}</div>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="$emit('close')">稍后再选</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!selected"
        @click="save"
      >
        确定
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { usePersonalityStore } from '../stores/personality'
import type { PersonalityValue } from '../stores/personality'

const props = defineProps<{
  visible: boolean
}>()
const emit = defineEmits<{
  close: []
  saved: [PersonalityValue]
}>()

const store = usePersonalityStore()
const selected = ref<PersonalityValue>(store.personality)
const saving = ref(false)

watch(
  () => props.visible,
  async (v) => {
    if (!v) return
    if (!store.loaded) await store.fetchPersonality()
    selected.value = store.personality
  },
)

function planLabel(p: string) {
  return (
    {
      asked_only: '只在用户问起时',
      once: '可主动跟进一次',
      active_once: '更主动跟进一次',
    } as Record<string, string>
  )[p] ?? p
}

function painLabel(p: string) {
  return (
    {
      background_only: '只作背景，不主动提',
      triggered_only: '相关时作背景承接',
      soft_triggered: '相关时温柔承接，但不展开',
    } as Record<string, string>
  )[p] ?? p
}

async function save() {
  if (!selected.value) return
  saving.value = true
  try {
    await store.setPersonality(selected.value)
    const label =
      store.options.find((o) => o.value === selected.value)?.label ?? selected.value
    ElMessage.success(`已切换到「${label}」`)
    emit('saved', selected.value)
    emit('close')
  } catch (e) {
    ElMessage.error((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.picker-intro {
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  margin: 0 0 16px;
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card {
  border: 1.5px solid #e4e7ed;
  border-radius: 10px;
  padding: 14px 16px;
  cursor: pointer;
  transition: all 0.15s ease;
  background: #fff;
}
.card:hover {
  border-color: #a78bfa;
}
.card.active {
  border-color: #a78bfa;
  background: #f5f1ff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.card-name {
  font-weight: 600;
  color: #303133;
  font-size: 15px;
}
.card-desc {
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  margin-bottom: 8px;
}
.card-meta {
  color: #909399;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
</style>
