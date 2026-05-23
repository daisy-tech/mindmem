<template>
  <div class="memory-view">
    <el-tabs v-model="activeTab" class="main-tabs">
      <!-- ========== 记忆画廊 Tab ========== -->
      <el-tab-pane label="聊天记忆" name="gallery">
        <div class="tab-toolbar">
          <div class="tag-filter">
            <span class="filter-tag" :class="{ active: activeTag === null }" @click="activeTag = null">
              全部 <em>{{ memoryStore.memories.length }}</em>
            </span>
            <span
              v-for="tag in allTags" :key="tag.name"
              class="filter-tag"
              :class="{ active: activeTag === tag.name }"
              :style="activeTag === tag.name ? { background: tag.color, color: '#fff', borderColor: tag.color } : { borderColor: tag.color, color: tag.color }"
              @click="activeTag = activeTag === tag.name ? null : tag.name"
            >{{ tag.name }} <em>{{ countByTag(tag.name) }}</em></span>
          </div>
          <el-button :loading="memoryStore.loading" @click="memoryStore.fetchMemories()">刷新</el-button>
        </div>

        <el-skeleton :rows="5" animated v-if="memoryStore.loading" />
        <el-empty v-else-if="filteredMemories.length === 0" description="暂无记忆" />
        <div v-else class="timeline">
          <div v-for="group in groupedMemories" :key="group.date" class="timeline-group">
            <div class="date-header">
              <div class="date-dot" />
              <span class="date-label">{{ group.label }}</span>
              <span class="date-count">{{ group.items.length }} 条</span>
            </div>
            <div class="timeline-cards">
              <div v-for="mem in group.items" :key="mem.id" class="memory-card">
                <div class="card-left">
                  <div class="time-dot" />
                  <div class="time-line" />
                </div>
                <el-card class="card-body" shadow="hover">
                  <div class="card-tags">
                    <el-tag v-for="tag in getMemTags(mem.memory)" :key="tag.name" :type="tag.type" size="small" style="margin-right:4px">{{ tag.name }}</el-tag>
                  </div>
                  <div class="memory-content">{{ mem.memory }}</div>
                  <div class="memory-meta">
                    <span class="memory-time">{{ formatTime(mem.created_at) }}</span>
                    <el-button type="danger" size="small" text @click="memoryStore.deleteMemory(mem.id)">删除</el-button>
                  </div>
                </el-card>
              </div>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ========== 用户画像 Tab ========== -->
      <el-tab-pane name="profile">
        <template #label>
          用户画像
        </template>

        <div class="tab-toolbar">
          <span class="profile-updated" v-if="profileStore.profile.last_updated">
            最后更新：{{ formatTime(profileStore.profile.last_updated) }}
          </span>
          <el-button :loading="profileStore.loading" @click="profileStore.fetchProfile()">刷新</el-button>
        </div>

        <el-skeleton :rows="6" animated v-if="profileStore.loading" />
        <div v-else>
          <!-- 画像各维度 -->
          <div v-if="hasProfile">
            <div v-for="section in profileSections" :key="section.key" class="profile-section">
              <div v-if="getSectionFields(section.key).length" class="section-title">
                <el-icon><component :is="section.icon" /></el-icon> {{ section.label }}
              </div>
              <div class="field-grid">
                <div
                  v-for="field in getSectionFields(section.key)" :key="field.path"
                  class="field-card"
                >
                  <div class="field-name">{{ fieldLabel(field.key) }}</div>
                  <div class="field-value">{{ formatValue(field.value) }}</div>
                  <div class="field-footer">
                    <el-progress
                      :percentage="Math.round(field.confidence * 100)"
                      :stroke-width="4"
                      :show-text="false"
                      :color="confColor(field.confidence)"
                      style="width:80px"
                    />
                    <span class="field-conf">{{ Math.round(field.confidence * 100) }}%</span>
                    <el-button
                      type="danger" size="small" text
                      @click="profileStore.deleteField(field.path)"
                    >删除</el-button>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无画像数据，开始聊天后会自动积累" />

          <!-- 自动处理记录折叠面板 -->
          <el-collapse class="conflict-log-collapse" v-model="conflictLogOpen">
            <el-collapse-item name="log">
              <template #title>
                <span class="log-collapse-title">
                  自动处理记录
                  <em v-if="profileStore.conflictLog.length">{{ profileStore.conflictLog.length }} 条</em>
                </span>
              </template>
              <el-button
                size="small" :loading="profileStore.logLoading"
                style="margin-bottom:12px"
                @click="profileStore.fetchConflictLog()"
              >刷新记录</el-button>
              <el-empty v-if="!profileStore.conflictLog.length" description="暂无记录" />
              <div v-else class="conflict-log-list">
                <div v-for="log in profileStore.conflictLog" :key="log.id" class="conflict-log-item">
                  <div class="log-header">
                    <el-tag size="small" :type="actionTagType(log.action)">{{ log.action_label }}</el-tag>
                    <span class="log-field">{{ log.field_label }}</span>
                    <span class="log-time">{{ formatTime(log.created_at) }}</span>
                  </div>
                  <div class="log-values">
                    <span class="log-old" v-if="log.old_value !== ''">{{ formatLogValue(log.old_value) }}</span>
                    <span class="log-arrow" v-if="log.old_value !== ''">→</span>
                    <span class="log-new">{{ formatLogValue(log.new_value) }}</span>
                  </div>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </div>
      </el-tab-pane>

      <!-- ========== 事件记忆 Tab ========== -->
      <el-tab-pane label="事件记忆" name="events">
        <div class="tab-toolbar">
          <div class="tag-filter">
            <span
              class="filter-tag" :class="{ active: activeEventType === null }"
              @click="activeEventType = null"
            >全部 <em>{{ eventStore.events.length }}</em></span>
            <span
              v-for="et in EVENT_TYPES" :key="et.type"
              class="filter-tag"
              :class="{ active: activeEventType === et.type }"
              :style="activeEventType === et.type
                ? { background: et.color, color: '#fff', borderColor: et.color }
                : { borderColor: et.color, color: et.color }"
              @click="activeEventType = activeEventType === et.type ? null : et.type"
            >{{ et.label }} <em>{{ countByType(et.type) }}</em></span>
          </div>
          <el-button :loading="eventStore.loading" @click="eventStore.fetchEvents()">刷新</el-button>
        </div>

        <el-skeleton :rows="5" animated v-if="eventStore.loading" />
        <el-empty v-else-if="filteredEvents.length === 0" description="暂无事件记忆，开始聊天后会自动提取" />
        <div v-else class="event-list">
          <div v-for="ev in filteredEvents" :key="ev.event_id" class="event-card">
            <!-- 左侧：日期轴 -->
            <div class="event-date-col">
              <div class="event-date-dot" :style="{ background: ev.type_color }" />
              <template v-if="ev.occurred_at">
                <div class="event-date-month">{{ formatEventMonth(ev.occurred_at) }}</div>
                <div class="event-date-day">{{ formatEventDay(ev.occurred_at) }}</div>
              </template>
              <div v-else class="event-date-unknown">?</div>
            </div>
            <!-- 右侧：内容卡片 -->
            <el-card class="event-body" shadow="hover">
              <div class="event-header">
                <el-tag
                  size="small"
                  :style="{ background: ev.type_color + '20', color: ev.type_color, borderColor: ev.type_color + '60' }"
                >{{ ev.type_label }}</el-tag>
                <div class="event-importance">
                  <el-progress
                    :percentage="Math.round(ev.importance * 100)"
                    :stroke-width="3"
                    :show-text="false"
                    :color="importanceColor(ev.importance)"
                    style="width:48px"
                  />
                  <span class="importance-label">{{ Math.round(ev.importance * 100) }}%</span>
                </div>
              </div>
              <div class="event-summary">{{ ev.summary }}</div>
              <div class="event-details" v-if="hasDetails(ev.details)">
                <span v-for="(v, k) in ev.details" :key="k" class="detail-chip" v-show="k !== 'mention_count'">
                  {{ k }}: {{ v }}
                </span>
              </div>
              <div class="event-footer">
                <span class="event-meta">
                  提取于 {{ formatDetectedAt(ev.detected_at) }}
                  <span v-if="ev.mention_count > 1"> · 提及 {{ ev.mention_count }} 次</span>
                </span>
                <div class="event-actions">
                  <el-button size="small" text @click="eventStore.archiveEvent(ev.event_id)">归档</el-button>
                  <el-button size="small" text type="danger" @click="eventStore.deleteEvent(ev.event_id)">删除</el-button>
                </div>
              </div>
            </el-card>
          </div>
        </div>
      </el-tab-pane>

      <!-- ========== 社会关系 Tab ========== -->
      <el-tab-pane label="社会关系" name="social">
        <div class="tab-toolbar">
          <span class="profile-updated" v-if="profileStore.profile.last_updated">
            最后更新：{{ formatTime(profileStore.profile.last_updated) }}
          </span>
          <el-button :loading="profileStore.loading" @click="profileStore.fetchProfile()">刷新</el-button>
        </div>
        <SocialGraph
          :relationships="socialRelationships"
          :self-name="selfName"
          :confidence="socialConfidence"
        />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onActivated, watch } from 'vue'
import { User, Briefcase, Star, Clock, Flag, Connection } from '@element-plus/icons-vue'
import { useMemoryStore } from '../stores/memory'
import { useProfileStore } from '../stores/profile'
import { useEventStore } from '../stores/events'
import { extractTags, getAllTags, type MemoryTag } from '../utils/memoryTagger'
import SocialGraph from '../components/SocialGraph.vue'

const memoryStore = useMemoryStore()
const profileStore = useProfileStore()
const eventStore = useEventStore()
const activeTab = ref('gallery')
const activeTag = ref<string | null>(null)
const activeEventType = ref<string | null>(null)
const conflictLogOpen = ref<string[]>([])
const allTags = getAllTags()

// 事件类型定义
const EVENT_TYPES = [
  { type: 'plan',          label: '计划',   color: '#409eff' },
  { type: 'experience',    label: '经历',   color: '#67c23a' },
  { type: 'achievement',   label: '成就',   color: '#e6a23c' },
  { type: 'pain_point',    label: '痛点',   color: '#f56c6c' },
  { type: 'feedback',      label: '反馈',   color: '#909399' },
  { type: 'status_change', label: '状态变更', color: '#9b59b6' },
]

// ───── 记忆画廊逻辑 ─────
function getMemTags(text: string): MemoryTag[] { return extractTags(text) }
function countByTag(tagName: string) {
  return memoryStore.memories.filter(m => extractTags(m.memory).some(t => t.name === tagName)).length
}
const filteredMemories = computed(() => {
  if (!activeTag.value) return memoryStore.memories
  return memoryStore.memories.filter(m => extractTags(m.memory).some(t => t.name === activeTag.value))
})
const groupedMemories = computed(() => {
  const map = new Map<string, typeof filteredMemories.value>()
  for (const mem of filteredMemories.value) {
    const date = mem.created_at.slice(0, 10)
    if (!map.has(date)) map.set(date, [])
    map.get(date)!.push(mem)
  }
  return Array.from(map.entries())
    .sort((a, b) => b[0].localeCompare(a[0]))
    .map(([date, items]) => ({ date, label: formatDateLabel(date), items }))
})
function formatDateLabel(dateStr: string) {
  const d = new Date(dateStr)
  const today = new Date()
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1)
  const fmt = (d: Date) => `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`
  if (dateStr === today.toISOString().slice(0, 10)) return `今天 · ${fmt(d)}`
  if (dateStr === yesterday.toISOString().slice(0, 10)) return `昨天 · ${fmt(d)}`
  return fmt(d)
}
function formatTime(d: string) {
  return new Date(d).toLocaleString('zh-CN')
}

// ───── 用户画像逻辑 ─────
const FIELD_LABELS: Record<string, string> = {
  name: '姓名', nickname: '昵称', age: '年龄', birthday: '生日',
  gender: '性别', location: '所在地', language: '常用语言',
  family_structure: '家庭结构', relationships: '重要关系',
  job_title: '职位', industry: '所在行业', skills: '技能',
  work_pain_points: '工作痛点',
  long_term: '长期兴趣', short_term: '近期关注',
  content_preference: '内容偏好', interaction_style: '交流风格',
  tech_attitude: '技术态度', sensitive_topics: '敏感话题',
  active_hours: '活跃时段', question_depth: '提问深度',
  short_term_goals: '短期目标', long_term_goals: '长期目标',
  current_pains: '当前痛点',
  frequent_topics: '常聊话题', unresolved_issues: '未解决问题',
  user_corrections: '用户纠正记录', explicit_preferences: '明确偏好',
}

function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}
const profileSections = [
  { key: 'basic', label: '基本信息', icon: 'User' },
  { key: 'career', label: '职业', icon: 'Briefcase' },
  { key: 'interests', label: '兴趣偏好', icon: 'Star' },
  { key: 'habits', label: '习惯', icon: 'Clock' },
  { key: 'goals_pains', label: '目标与痛点', icon: 'Flag' },
  { key: 'social', label: '社会关系', icon: 'Connection' },
  { key: 'interaction_history', label: '交互偏好', icon: 'Connection' },
  { key: 'values_attitudes', label: '价值观', icon: 'Star' },
]

const hasProfile = computed(() => {
  const p = profileStore.profile.profile
  return p && Object.values(p).some(s => Object.keys(s).length > 0)
})

function getSectionFields(sectionKey: string) {
  const section = profileStore.profile.profile?.[sectionKey] ?? {}
  return Object.entries(section).map(([key, field]) => ({
    key,
    path: `${sectionKey}.${key}`,
    value: (field as { value: unknown }).value,
    confidence: (field as { confidence: number }).confidence ?? 0,
  }))
}

function formatValue(val: unknown): string {
  if (Array.isArray(val)) return val.join('、')
  if (typeof val === 'object' && val !== null) return JSON.stringify(val)
  return String(val ?? '')
}

function confColor(conf: number) {
  if (conf >= 0.8) return '#67c23a'
  if (conf >= 0.5) return '#e6a23c'
  return '#f56c6c'
}

// 冲突日志辅助
function actionTagType(action: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  if (action === 'appended' || action === 'merged') return 'success'
  if (action === 'replaced') return 'warning'
  if (action === 'replaced_lower_conf') return 'danger'
  if (action === 'manual') return 'info'
  return ''
}
function formatLogValue(val: unknown): string {
  if (val === '' || val === null || val === undefined) return '—'
  if (typeof val === 'object') return JSON.stringify(val)
  return String(val)
}

// ───── 事件记忆逻辑 ─────
function countByType(type: string) {
  return eventStore.events.filter(e => e.event_type === type).length
}
const filteredEvents = computed(() => {
  const list = [...eventStore.events].sort((a, b) => {
    const da = a.occurred_at ?? a.detected_at
    const db = b.occurred_at ?? b.detected_at
    return db.localeCompare(da)
  })
  if (!activeEventType.value) return list
  return list.filter(e => e.event_type === activeEventType.value)
})
function formatEventMonth(dateStr: string) {
  const d = new Date(dateStr)
  return `${d.getMonth() + 1}月`
}
function formatEventDay(dateStr: string) {
  return String(new Date(dateStr).getDate()).padStart(2, '0')
}
function formatDetectedAt(dateStr: string) {
  return new Date(dateStr).toLocaleDateString('zh-CN')
}
function importanceColor(imp: number) {
  if (imp >= 0.8) return '#f56c6c'
  if (imp >= 0.6) return '#e6a23c'
  return '#67c23a'
}
function hasDetails(details: Record<string, unknown>) {
  return details && Object.keys(details).filter(k => k !== 'mention_count').length > 0
}

// 切换到事件 Tab 时自动加载
watch(activeTab, (tab) => {
  if (tab === 'events' && eventStore.events.length === 0) {
    eventStore.fetchEvents()
  }
  if (tab === 'profile') {
    profileStore.fetchConflictLog()
  }
})

// ───── 社会关系图逻辑 ─────
const socialRelationships = computed<Record<string, unknown> | null>(() => {
  const rel = profileStore.profile.profile?.['social']?.['relationships']
  if (!rel) return null
  const val = (rel as { value?: unknown }).value
  if (val && typeof val === 'object' && !Array.isArray(val)) {
    return val as Record<string, unknown>
  }
  return null
})

const selfName = computed<string>(() => {
  const name = profileStore.profile.profile?.['basic']?.['name']
  return (name as { value?: string } | undefined)?.value ?? '我'
})

const socialConfidence = computed<number>(() => {
  const rel = profileStore.profile.profile?.['social']?.['relationships']
  return (rel as { confidence?: number } | undefined)?.confidence ?? 0.7
})

onMounted(() => {
  memoryStore.fetchMemories()
  profileStore.fetchProfile()
})
onActivated(() => {
  memoryStore.fetchMemories()
  profileStore.fetchProfile()
})
</script>

<style scoped>
.memory-view { padding: 24px; max-width: 900px; margin: 0 auto; }
.main-tabs :deep(.el-tabs__header) { margin-bottom: 20px; }

.tab-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 8px;
}

/* 标签过滤 */
.tag-filter { display: flex; flex-wrap: wrap; gap: 8px; }
.filter-tag {
  cursor: pointer; padding: 4px 12px; border-radius: 20px;
  border: 1.5px solid #dcdfe6; font-size: 13px; color: #606266;
  transition: all 0.2s; user-select: none;
}
.filter-tag:hover { opacity: 0.8; }
.filter-tag.active { background: #409eff; color: #fff; border-color: #409eff; }
.filter-tag em { font-style: normal; font-size: 11px; opacity: 0.75; margin-left: 2px; }

/* 时间线 */
.timeline { position: relative; }
.timeline-group { margin-bottom: 32px; }
.date-header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.date-dot { width: 12px; height: 12px; border-radius: 50%; background: #409eff; flex-shrink: 0; }
.date-label { font-size: 14px; font-weight: 600; color: #303133; }
.date-count { font-size: 12px; color: #909399; }
.timeline-cards { padding-left: 6px; }
.memory-card { display: flex; margin-bottom: 12px; }
.card-left { display: flex; flex-direction: column; align-items: center; width: 28px; flex-shrink: 0; padding-top: 14px; }
.time-dot { width: 8px; height: 8px; border-radius: 50%; background: #c0c4cc; flex-shrink: 0; }
.time-line { flex: 1; width: 2px; background: #e4e7ed; margin-top: 4px; min-height: 20px; }
.card-body { flex: 1; border-radius: 10px; min-width: 0; }
.card-tags { margin-bottom: 8px; }
.memory-content { font-size: 14px; line-height: 1.7; color: #303133; margin-bottom: 10px; }
.memory-meta { display: flex; justify-content: space-between; align-items: center; }
.memory-time { font-size: 12px; color: #909399; }

/* 用户画像 */
.profile-updated { font-size: 12px; color: #909399; }
.clarification-section { margin-bottom: 24px; }
.clarification-item { margin-bottom: 10px; }
.clarification-body { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.clarification-actions { display: flex; gap: 6px; flex-shrink: 0; }
.profile-section { margin-bottom: 24px; }
.section-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 600; color: #303133;
  margin-bottom: 12px;
}
.section-title.warning { color: #e6a23c; }
.field-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.field-card {
  background: #f5f7fa; border-radius: 10px; padding: 12px;
  border: 1px solid #e4e7ed;
}
.field-name { font-size: 11px; color: #909399; margin-bottom: 4px; }
.field-value { font-size: 14px; color: #303133; font-weight: 500; margin-bottom: 8px; word-break: break-all; }
.field-footer { display: flex; align-items: center; gap: 6px; }
.field-conf { font-size: 11px; color: #909399; }

/* 事件记忆 */
.event-list { display: flex; flex-direction: column; gap: 16px; }
.event-card { display: flex; gap: 16px; align-items: flex-start; }
.event-date-col {
  display: flex; flex-direction: column; align-items: center;
  width: 44px; flex-shrink: 0; padding-top: 12px; gap: 2px;
}
.event-date-dot {
  width: 10px; height: 10px; border-radius: 50%; margin-bottom: 4px;
}
.event-date-month { font-size: 11px; color: #909399; line-height: 1.2; }
.event-date-day { font-size: 18px; font-weight: 700; color: #303133; line-height: 1.1; }
.event-date-unknown { font-size: 20px; color: #c0c4cc; }
.event-body { flex: 1; border-radius: 10px; }
.event-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.event-importance { display: flex; align-items: center; gap: 6px; }
.importance-label { font-size: 11px; color: #909399; }
.event-summary { font-size: 14px; color: #303133; line-height: 1.6; margin-bottom: 8px; }
.event-details { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.detail-chip {
  background: #f5f7fa; border: 1px solid #e4e7ed;
  border-radius: 4px; padding: 2px 8px;
  font-size: 12px; color: #606266;
}
.event-footer { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.event-meta { font-size: 12px; color: #c0c4cc; }
.event-actions { display: flex; gap: 4px; }

/* 冲突日志 */
.conflict-log-collapse { margin-top: 32px; border-top: 1px solid #e4e7ed; }
.log-collapse-title { font-size: 13px; color: #606266; }
.log-collapse-title em { font-style: normal; margin-left: 6px; font-size: 12px; color: #909399; }
.conflict-log-list { display: flex; flex-direction: column; gap: 10px; }
.conflict-log-item { background: #fafafa; border: 1px solid #ebeef5; border-radius: 8px; padding: 10px 14px; }
.log-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.log-field { font-size: 13px; font-weight: 500; color: #303133; flex: 1; }
.log-time { font-size: 11px; color: #c0c4cc; }
.log-values { display: flex; align-items: center; gap: 8px; font-size: 13px; flex-wrap: wrap; }
.log-old { color: #f56c6c; text-decoration: line-through; }
.log-arrow { color: #c0c4cc; }
.log-new { color: #67c23a; font-weight: 500; }
</style>
