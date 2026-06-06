<template>
  <div class="eval-chat-panel">
    <aside class="conv-aside">
      <div class="aside-head">
        <div class="aside-title">我的历史会话</div>
        <el-button size="small" link @click="refresh">刷新</el-button>
      </div>
      <el-alert
        v-if="listError"
        :title="listError"
        type="error"
        :closable="false"
        show-icon
        class="aside-alert"
      />
      <el-skeleton v-if="loadingList && !conversations.length" :rows="6" animated />
      <el-empty
        v-else-if="!conversations.length"
        description="暂无历史会话，先去对话页聊几句"
        :image-size="48"
      />
      <div
        v-for="c in conversations"
        :key="c.id"
        class="conv-item"
        :class="{ active: c.id === selectedId }"
        @click="onSelect(c.id)"
      >
        <div class="conv-title">{{ c.title || '（未命名）' }}</div>
        <div class="conv-meta">
          <span>{{ formatTime(c.updated_at) }}</span>
          <el-tag
            v-if="cacheBadge(c.id)"
            :type="cacheBadge(c.id)!.type"
            size="small"
            effect="plain"
          >
            {{ cacheBadge(c.id)!.text }}
          </el-tag>
        </div>
      </div>
    </aside>

    <main class="report-main">
      <div v-if="!selectedId" class="empty-tip">
        <el-empty description="选择左侧一条会话，点「开始评估」即可分析" />
      </div>

      <template v-else>
        <div class="report-toolbar">
          <h3 class="report-title">
            {{ selectedConv?.title || selectedId }}
            <span class="muted">· {{ selectedConv?.updated_at ? formatTime(selectedConv.updated_at) : '' }}</span>
          </h3>
          <div class="toolbar-actions">
            <el-button
              type="primary"
              :loading="evaluating"
              :disabled="evaluating"
              @click="onEvaluate(false)"
            >
              {{ currentReport ? '重新评估' : '开始评估' }}
            </el-button>
            <el-button
              v-if="currentReport"
              :loading="evaluating"
              @click="onEvaluate(true)"
              title="强制重新拉取，绕过本地缓存"
            >
              强制刷新
            </el-button>
            <el-button v-if="currentReport" @click="onDownload">
              下载评估 JSON
            </el-button>
          </div>
        </div>

        <el-alert
          v-if="evalError"
          :title="`评估失败: ${evalError}`"
          type="error"
          :closable="true"
          show-icon
          class="report-alert"
        />

        <div v-if="!currentReport && !evaluating" class="empty-tip">
          <el-empty
            description="点击「开始评估」拉取该会话的结构分析（不会调用 LLM，不耗 token）"
          />
        </div>

        <div v-if="evaluating && !currentReport" class="empty-tip">
          <el-skeleton :rows="8" animated />
        </div>

        <template v-if="currentReport">
          <div class="summary-bar">
            <span class="metric primary">
              <strong>结构通过率</strong>
              {{ Math.round((currentReport.review?.structure_pass_rate ?? 0) * 100) }}%
              <span class="muted">
                ({{ currentReport.review?.counters.l0_pass ?? 0 }}/{{ currentReport.review?.evaluable_turns ?? 0 }})
              </span>
            </span>
            <span class="metric">
              <strong>体感综合</strong>
              {{ Math.round((currentReport.review?.final_ok_rate ?? 0) * 100) }}%
              <span class="muted">
                ({{ currentReport.review?.counters.final_ok ?? 0 }}/{{ currentReport.review?.evaluable_turns ?? 0 }})
              </span>
            </span>
            <span class="metric" v-if="(currentReport.review?.counters.final_suspicious ?? 0) > 0">
              <strong>可疑</strong>
              <el-tag type="warning" size="small" effect="plain">
                {{ currentReport.review!.counters.final_suspicious }}
              </el-tag>
            </span>
            <span class="metric" v-if="(currentReport.review?.counters.final_bad ?? 0) > 0">
              <strong>失败</strong>
              <el-tag type="danger" size="small" effect="plain">
                {{ currentReport.review!.counters.final_bad }}
              </el-tag>
            </span>
            <span class="metric" v-if="(currentReport.review?.counters.turns_skipped ?? 0) > 0">
              <strong>跳过</strong>
              <el-tag type="info" size="small" effect="plain">
                {{ currentReport.review!.counters.turns_skipped }}
              </el-tag>
              <span class="muted" v-if="(currentReport.review?.counters.turns_unavailable ?? 0) > 0">
                · {{ currentReport.review!.counters.turns_unavailable }} 无 prompt_meta
              </span>
            </span>
            <span class="metric" v-if="currentReport.review?.root_cause_top?.length">
              <strong>高频归因</strong>
              <el-tag
                v-for="[code, n] in currentReport.review!.root_cause_top.slice(0, 3)"
                :key="code"
                size="small"
                effect="plain"
                class="cause-tag"
              >
                {{ code }} × {{ n }}
              </el-tag>
            </span>
          </div>

          <div class="legend">
            <el-tag type="success" size="small" effect="plain">✓ ok</el-tag>
            <span>L0 全绿 + L1 无报警</span>
            <el-tag type="warning" size="small" effect="plain">🟡 suspicious</el-tag>
            <span>L0 medium 失败 或 L1 报告可疑</span>
            <el-tag type="danger" size="small" effect="plain">❌ bad</el-tag>
            <span>L0 high 失败 或 L1 fail</span>
            <el-tag type="info" size="small" effect="plain">— skip</el-tag>
            <span>旧消息无审计数据</span>
          </div>

          <el-table :data="currentReport.turns || []" stripe @row-click="openTurn">
            <el-table-column label="#" prop="index" width="56" align="center" />
            <el-table-column label="用户句" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.input?.user_message }}
              </template>
            </el-table-column>
            <el-table-column label="Intent" width="180">
              <template #default="{ row }">
                <span v-if="getIntent(row)">
                  {{ getIntent(row) }}
                  <span v-if="getIntentConf(row) != null" class="muted small">
                    · {{ Math.round((getIntentConf(row) ?? 0) * 100) }}%
                  </span>
                </span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="L0" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="l0TagType(row)" size="small" effect="plain">
                  {{ l0TagText(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="L1" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="l1TagType(row)" size="small" effect="plain">
                  {{ l1TagText(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="归因" width="160">
              <template #default="{ row }">
                <el-tag
                  v-for="code in row.review?.suggested_root_cause || []"
                  :key="code"
                  size="small"
                  effect="plain"
                  class="cause-tag"
                >
                  {{ code }}
                </el-tag>
                <span v-if="!row.review?.suggested_root_cause?.length" class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center">
              <template #default="{ row }">
                <el-button link type="primary" @click.stop="openTurn(row)">穿透</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </template>
    </main>

    <EvalChatTurnDrawer
      v-model:visible="drawerVisible"
      :turn="activeTurn"
      :snapshot="currentReport?.memory_snapshot ?? null"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { useEvalChatStore, type AuditTurn } from '../stores/evalChat'
import EvalChatTurnDrawer from './EvalChatTurnDrawer.vue'

const store = useEvalChatStore()
const {
  conversations,
  loadingList,
  listError,
  selectedId,
  evaluating,
  evalError,
  currentReport,
} = storeToRefs(store)

const drawerVisible = ref(false)
const activeTurn = ref<AuditTurn | null>(null)

onMounted(() => {
  store.fetchConversations()
})

const selectedConv = computed(() =>
  conversations.value.find((c) => c.id === selectedId.value) || null,
)

async function refresh() {
  await store.fetchConversations(true)
}

function onSelect(id: string) {
  store.select(id)
}

async function onEvaluate(force: boolean) {
  if (!selectedId.value) return
  if (force) store.clearCache(selectedId.value)
  try {
    await store.evaluate(selectedId.value, { force })
  } catch {
    /* error 已写入 evalError */
  }
}

function onDownload() {
  store.downloadCurrent()
  ElMessage.success('已开始下载评估 JSON')
}

function cacheBadge(id: string): { type: 'success' | 'warning' | 'danger' | 'info'; text: string } | null {
  const pack = (store.currentReport && store.selectedId === id)
    ? store.currentReport
    : null
  // 暂只标"当前选中已评过"——避免遍历缓存影响渲染
  if (!pack || pack.conversation.id !== id) return null
  const c = pack.review?.counters
  if (!c) return null
  if (c.final_bad > 0) return { type: 'danger', text: `❌${c.final_bad}` }
  if (c.final_suspicious > 0) return { type: 'warning', text: `🟡${c.final_suspicious}` }
  return { type: 'success', text: '✓' }
}

function formatTime(iso?: string | null) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function getIntent(turn: AuditTurn): string | undefined {
  return turn.audit?.prompt_meta?.route?.intent
}
function getIntentConf(turn: AuditTurn): number | null | undefined {
  return turn.audit?.prompt_meta?.route?.intent_confidence
}

function l0TagType(turn: AuditTurn): 'success' | 'warning' | 'danger' | 'info' {
  const s = turn.review?.l0_status
  if (s === 'pass') return 'success'
  if (s === 'warn') return 'warning'
  if (s === 'fail') return 'danger'
  return 'info'
}
function l0TagText(turn: AuditTurn): string {
  const s = turn.review?.l0_status
  if (s === 'pass') return '✓'
  if (s === 'warn') return '🟡'
  if (s === 'fail') return '❌'
  return '—'
}
function l1TagType(turn: AuditTurn): 'success' | 'warning' | 'danger' | 'info' {
  const s = turn.review?.l1_status
  if (s === 'ok') return 'success'
  if (s === 'suspicious') return 'warning'
  if (s === 'bad') return 'danger'
  return 'info'
}
function l1TagText(turn: AuditTurn): string {
  const s = turn.review?.l1_status
  if (s === 'ok') return '✓'
  if (s === 'suspicious') return '🟡'
  if (s === 'bad') return '❌'
  return '—'
}

function openTurn(row: AuditTurn) {
  activeTurn.value = row
  drawerVisible.value = true
}
</script>

<style scoped>
.eval-chat-panel {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  min-height: 600px;
}

.conv-aside {
  background: #fafafa;
  border-radius: 8px;
  padding: 12px;
  max-height: 80vh;
  overflow-y: auto;
}

.aside-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.aside-title {
  font-weight: 600;
  color: #303133;
}

.aside-alert {
  margin-bottom: 8px;
}

.conv-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: background 0.15s;
}
.conv-item:hover {
  background: #ecf5ff;
}
.conv-item.active {
  background: #e1eafd;
  outline: 1px solid #b3d4ff;
}

.conv-title {
  font-size: 14px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conv-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.report-main {
  background: #fff;
  padding: 16px 20px;
  border-radius: 8px;
  border: 1px solid #ebeef5;
  min-height: 600px;
}

.empty-tip {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 320px;
}

.report-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
.report-title {
  margin: 0;
  font-size: 16px;
  color: #303133;
}
.toolbar-actions {
  display: flex;
  gap: 8px;
}

.report-alert {
  margin-bottom: 12px;
}

.summary-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  padding: 12px 14px;
  background: #f5f7fa;
  border-radius: 6px;
  margin-bottom: 10px;
  font-size: 13px;
}
.metric {
  display: flex;
  align-items: center;
  gap: 6px;
}
.metric strong {
  color: #606266;
  font-weight: 600;
}
.metric.primary strong {
  color: #303133;
}
.muted {
  color: #909399;
}
.small {
  font-size: 12px;
}
.cause-tag {
  margin-right: 4px;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #606266;
  margin: 8px 0 12px;
}
</style>
