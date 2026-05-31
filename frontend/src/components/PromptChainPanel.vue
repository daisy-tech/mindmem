<template>
  <div v-if="promptData" class="chain-panel">
    <div v-if="title" class="panel-title">{{ title }}</div>

    <div v-if="promptData.route" class="section">
      <div class="section-title">
        <el-icon><Aim /></el-icon>
        本轮路由
      </div>
      <div class="route-grid">
        <div class="route-item">
          <span class="k">意图</span>
          <span class="v">
            {{ intentLabel(promptData.route.intent) }}
            <span class="depth">· {{ promptData.route.memory_depth }}</span>
            <el-tag
              v-if="promptData.route.router_version"
              size="small"
              type="info"
              effect="plain"
              class="router-ver"
            >
              {{ promptData.route.router_version }}
            </el-tag>
          </span>
        </div>
        <div
          v-if="promptData.route.intent_source || promptData.route.intent_confidence != null"
          class="route-item"
        >
          <span class="k">分类</span>
          <span class="v">
            {{ promptData.route.intent_source || '—' }}
            <template v-if="promptData.route.intent_confidence != null">
              · {{ Math.round((promptData.route.intent_confidence ?? 0) * 100) }}%
            </template>
            <el-tag
              v-if="promptData.route.low_confidence"
              size="small"
              type="warning"
              effect="plain"
            >
              低置信
            </el-tag>
          </span>
        </div>
        <div class="route-item">
          <span class="k">人格</span>
          <span class="v">{{ promptData.route.personality_label || promptData.route.personality }}</span>
        </div>
        <div class="route-item">
          <span class="k">显性引用上限</span>
          <span class="v">{{ promptData.route.max_explicit_memories ?? '-' }} 条</span>
        </div>
        <div v-if="promptData.route.sensitive_mode" class="route-item">
          <span class="k">敏感模式</span>
          <el-tag size="small" type="warning" effect="plain">开启</el-tag>
        </div>
        <div v-if="promptData.route.inferred_subjects?.length" class="route-item">
          <span class="k">推断对象</span>
          <span class="v">{{ promptData.route.inferred_subjects.join('、') }}</span>
        </div>
        <div
          v-if="promptData.route.event_policy && promptData.route.event_policy !== 'none'"
          class="route-item"
        >
          <span class="k">事件策略</span>
          <span class="v">{{ promptData.route.event_policy }}</span>
        </div>
        <div v-if="promptData.route.load_layers?.length" class="route-item">
          <span class="k">加载层</span>
          <span class="v">{{ promptData.route.load_layers.join('、') }}</span>
        </div>
        <div v-if="promptData.route.query" class="route-item">
          <span class="k">检索 query</span>
          <span class="v">{{ promptData.route.query }}</span>
        </div>
      </div>
      <el-collapse v-if="promptData.route.reasons?.length" class="reasons-collapse">
        <el-collapse-item :title="`判断依据（${promptData.route.reasons.length}）`" name="r">
          <ul class="reasons-list">
            <li v-for="(r, i) in promptData.route.reasons" :key="i">{{ r }}</li>
          </ul>
        </el-collapse-item>
      </el-collapse>
    </div>

    <div
      v-if="promptData.model || promptData.composed_at || promptData.trigger_message"
      class="section"
    >
      <div class="section-title">
        <el-icon><Document /></el-icon>
        分析元数据
      </div>
      <div class="route-grid">
        <div v-if="promptData.model" class="route-item">
          <span class="k">模型</span>
          <span class="v">{{ promptData.model }}</span>
        </div>
        <div v-if="promptData.composed_at" class="route-item">
          <span class="k">组装时间</span>
          <span class="v">{{ formatTime(promptData.composed_at) }}</span>
        </div>
        <div v-if="promptData.history_turns != null" class="route-item">
          <span class="k">历史轮次</span>
          <span class="v">{{ promptData.history_turns }}</span>
        </div>
        <div v-if="promptData.trigger_message" class="route-item trigger-msg">
          <span class="k">触发消息</span>
          <span class="v">{{ promptData.trigger_message }}</span>
        </div>
      </div>
    </div>

    <div v-if="hasContextLayers" class="section">
      <div class="section-title">
        <el-icon><Collection /></el-icon>
        分层记忆池
      </div>
      <el-collapse>
        <el-collapse-item
          v-for="layer in contextLayerEntries"
          :key="layer.key"
          :title="`${layer.label}（${layer.items.length}）`"
          :name="layer.key"
        >
          <div v-if="layer.items.length === 0" class="layer-empty">无</div>
          <div v-for="(m, i) in layer.items" :key="i" class="amem">
            <div class="amem-head">
              <el-tag :type="usageTagType(m.usage)" size="small" effect="plain">
                {{ usageLabel(m.usage) }}
              </el-tag>
              <span class="amem-source">{{ sourceLabel(m.source) }}</span>
              <span class="amem-score">score {{ m.score }}</span>
            </div>
            <div class="amem-text">{{ m.text }}</div>
            <div class="amem-reason">{{ m.reason }}</div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <div class="section">
      <div class="section-title">
        <el-icon><Collection /></el-icon>
        激活的记忆
        <el-tag size="small" type="info" style="margin-left:6px">
          {{ (promptData.activated && promptData.activated.length) || promptData.memories.length }} 条
        </el-tag>
      </div>
      <template v-if="promptData.activated && promptData.activated.length">
        <div v-for="(m, i) in promptData.activated" :key="i" class="amem">
          <div class="amem-head">
            <el-tag :type="usageTagType(m.usage)" size="small" effect="plain">
              {{ usageLabel(m.usage) }}
            </el-tag>
            <span class="amem-source">{{ sourceLabel(m.source) }}</span>
          </div>
          <div class="amem-text">{{ m.text }}</div>
          <div class="amem-reason">{{ m.reason }}</div>
        </div>
      </template>
      <template v-else>
        <el-empty
          v-if="promptData.memories.length === 0"
          description="无相关记忆"
          :image-size="48"
        />
        <div v-else class="memory-list">
          <div v-for="(mem, i) in promptData.memories" :key="i" class="memory-item">
            <div class="memory-index">{{ i + 1 }}</div>
            <div class="memory-body">
              <div class="memory-text">{{ mem }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <div class="section">
      <div class="section-title">
        <el-icon><Document /></el-icon>
        完整 System Prompt
      </div>
      <el-collapse>
        <el-collapse-item title="点击展开查看" name="sys">
          <pre class="system-prompt">{{ promptData.system }}</pre>
        </el-collapse-item>
      </el-collapse>
    </div>

    <div v-if="promptData.llm_request?.messages?.length" class="section">
      <div class="section-title">
        <el-icon><Document /></el-icon>
        LLM 请求快照
        <el-tag size="small" type="info" style="margin-left:6px">
          {{ promptData.llm_request.messages.length }} 条
        </el-tag>
      </div>
      <el-collapse v-if="!defaultExpandLlm">
        <el-collapse-item title="完整 messages（system + history + user）" name="llm">
          <template v-for="(msg, i) in promptData.llm_request.messages" :key="i">
            <div class="llm-msg">
              <div class="llm-role">{{ msg.role }}</div>
              <pre class="llm-content">{{ msg.content }}</pre>
            </div>
          </template>
        </el-collapse-item>
      </el-collapse>
      <template v-else>
        <div
          v-for="(msg, i) in promptData.llm_request.messages"
          :key="i"
          class="llm-msg"
        >
          <div class="llm-role">{{ msg.role }}</div>
          <pre class="llm-content">{{ msg.content }}</pre>
        </div>
      </template>
    </div>
  </div>
  <el-empty v-else description="无 Prompt 数据" :image-size="64" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Aim, Collection, Document } from '@element-plus/icons-vue'
import type { ActivatedMemory, PromptData } from '../stores/chat'

const props = withDefaults(
  defineProps<{
    promptData?: PromptData
    title?: string
    defaultExpandLlm?: boolean
  }>(),
  { defaultExpandLlm: false },
)

const CONTEXT_LAYER_LABELS: Record<string, string> = {
  stable_profile: '稳定画像',
  relevant_relationships: '社会关系',
  relevant_events: '事件记忆',
  relevant_memories: '情节记忆',
  background_only: '背景信息',
}

const contextLayerEntries = computed(() => {
  const layers = props.promptData?.context_layers
  if (!layers) return []
  return Object.entries(CONTEXT_LAYER_LABELS).map(([key, label]) => ({
    key,
    label,
    items: (layers[key as keyof typeof layers] ?? []) as ActivatedMemory[],
  }))
})

const hasContextLayers = computed(() =>
  contextLayerEntries.value.some((layer) => layer.items.length > 0),
)

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString('zh-CN')
  } catch {
    return iso
  }
}

const INTENT_LABELS: Record<string, string> = {
  casual: '闲聊',
  self_summary: '自我总结',
  relationship_topic: '关系话题',
  emotional_support: '情绪支持',
  plan_followup: '计划跟进',
  preference_request: '偏好建议',
  correction: '纠错',
  knowledge_task: '知识/工具',
}

const USAGE_LABELS: Record<string, string> = {
  explicit_ok: '可显性引用',
  background_only: '仅作背景',
  follow_up_once: '可轻跟进',
  avoid_unless_asked: '除非问起否则避免',
}

const SOURCE_LABELS: Record<string, string> = {
  profile: '画像',
  relationship: '社会关系',
  event: '事件',
  episodic: '情节记忆',
}

function intentLabel(i: string) {
  return INTENT_LABELS[i] ?? i
}

function usageLabel(u: string) {
  return USAGE_LABELS[u] ?? u
}

function sourceLabel(s: string) {
  return SOURCE_LABELS[s] ?? s
}

function usageTagType(u: string): 'success' | 'info' | 'warning' | 'danger' | '' {
  switch (u) {
    case 'explicit_ok':
      return 'success'
    case 'follow_up_once':
      return 'warning'
    case 'background_only':
      return 'info'
    case 'avoid_unless_asked':
      return 'danger'
    default:
      return ''
  }
}
</script>

<style scoped>
.chain-panel {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  padding-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
}
.section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.route-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #f8f7ff;
  border: 1px solid #ece9ff;
  border-radius: 8px;
  padding: 10px 14px;
}
.route-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.route-item .k {
  color: #909399;
  width: 90px;
  flex-shrink: 0;
}
.route-item .v {
  color: #303133;
}
.route-item .depth {
  color: #909399;
  margin-left: 4px;
}
.route-item .router-ver {
  margin-left: 6px;
  vertical-align: middle;
}
.reasons-collapse :deep(.el-collapse-item__header) {
  font-size: 12px;
  color: #606266;
  border-bottom: none;
}
.reasons-list {
  font-size: 12px;
  color: #606266;
  line-height: 1.7;
  margin: 0;
  padding-left: 18px;
}
.amem {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.amem + .amem {
  margin-top: 8px;
}
.amem-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: #909399;
}
.amem-text {
  font-size: 13px;
  color: #303133;
  line-height: 1.6;
}
.amem-reason {
  font-size: 11px;
  color: #909399;
}
.memory-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.memory-item {
  display: flex;
  gap: 10px;
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
}
.memory-body {
  flex: 1;
  background: #f5f7fa;
  border-radius: 8px;
  padding: 8px 12px;
}
.system-prompt,
.llm-content {
  font-size: 12px;
  line-height: 1.6;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
  background: #f5f7fa;
  border-radius: 6px;
  padding: 10px;
  margin: 0;
}
.trigger-msg {
  align-items: flex-start;
}
.trigger-msg .v {
  white-space: pre-wrap;
}
.layer-empty {
  font-size: 12px;
  color: #909399;
}
.llm-msg + .llm-msg {
  margin-top: 10px;
}
.llm-role {
  font-size: 11px;
  font-weight: 600;
  color: #409eff;
  margin-bottom: 4px;
}
</style>
