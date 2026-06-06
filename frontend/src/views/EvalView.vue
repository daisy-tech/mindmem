<template>
  <div class="eval-view">
    <div class="eval-header">
      <div>
        <h2>评测实验室</h2>
        <p class="subtitle">穿透检查 Memory Router → Context → Prompt → LLM 全链路</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="eval-tabs">
      <el-tab-pane label="批量评测" name="batch">
        <div class="batch-toolbar">
          <el-select v-model="runType" size="default" style="width: 120px">
            <el-option label="Smoke 20" value="smoke" />
            <el-option label="Full 50" value="full" />
          </el-select>
          <el-checkbox v-model="runChat">同时调用 Chat 模型</el-checkbox>
          <el-button
            type="primary"
            :loading="starting || jobStatus.running"
            :disabled="starting || jobStatus.running"
            @click="onRerun"
          >
            {{ buttonLabel }}
          </el-button>
          <el-button :loading="seeding" @click="onSeedPersona">灌库 · 老张</el-button>
          <el-button :loading="personaLoading" @click="onViewPersona">查看老张数据</el-button>
          <el-upload
            :show-file-list="false"
            accept=".json"
            :before-upload="onImport"
          >
            <el-button>导入 report.json</el-button>
          </el-upload>
          <el-button @click="refreshRuns">刷新</el-button>
        </div>

        <el-alert
          v-if="pageError && activeTab === 'batch'"
          type="error"
          :closable="true"
          show-icon
          class="job-alert"
          @close="pageError = null"
        >
          {{ pageError }}
        </el-alert>

        <el-alert
          v-if="jobStatus.running"
          type="info"
          :closable="false"
          show-icon
          class="job-alert"
        >
          正在评测
          <strong v-if="jobStatus.run_id"> {{ jobStatus.run_id }}</strong>
          · case：<strong>{{ jobStatus.current_case || '…' }}</strong>
          （{{ jobStatus.progress ?? 0 }} / {{ jobStatus.total ?? '?' }}）
        </el-alert>
        <el-alert
          v-if="jobStatus.error"
          type="error"
          :closable="true"
          show-icon
          class="job-alert"
        >
          {{ jobStatus.error }}
        </el-alert>

        <p class="batch-hint">
          批量评测使用合成用户 <strong>persona_a_zhang（老张）</strong> 的记忆库。
          首次或 persona 定义更新后，点「灌库 · 老张」一次即可；「单条调试」仍用您登录账号的真实记忆。
        </p>

        <div class="eval-body">
      <aside class="run-list">
        <div class="aside-title">历史报告</div>
        <el-empty v-if="runs.length === 0" description="暂无报告，点「重新评测」" :image-size="48" />
        <div
          v-for="r in runs"
          :key="r.run_id"
          class="run-item"
          :class="{ active: selectedRunId === r.run_id }"
          @click="selectRun(r.run_id)"
        >
          <div class="run-id">{{ r.run_id }}</div>
          <div class="run-meta">
            <el-tag
              v-if="r.verdict?.pass != null"
              :type="r.verdict.pass ? 'success' : 'warning'"
              size="small"
              effect="plain"
            >
              {{ r.verdict.pass ? 'PASS' : 'FAIL' }}
            </el-tag>
            <span v-if="r.verdict?.pass_rate != null">
              {{ Math.round((r.verdict.pass_rate ?? 0) * 100) }}%
            </span>
            <span
              v-if="r.verdict?.strict_pass_rate != null
                && r.verdict.strict_pass_rate !== r.verdict.pass_rate"
              class="strict-rate"
              title="strict 通过率（仅 intent 精确命中算过）"
            >
              · 严格 {{ Math.round((r.verdict.strict_pass_rate ?? 0) * 100) }}%
            </span>
            <span class="run-type">{{ r.run_type }}</span>
          </div>
          <div class="run-time">{{ formatTime(r.started_at) }}</div>
          <el-button
            class="run-download"
            size="small"
            link
            type="primary"
            :loading="downloadingRunId === r.run_id"
            @click.stop="onDownloadRun(r.run_id)"
          >
            下载
          </el-button>
        </div>
      </aside>

      <main class="run-detail" v-loading="loading">
        <template v-if="currentReport">
          <div class="summary-bar">
            <el-tag :type="currentReport.verdict?.pass ? 'success' : 'danger'">
              {{ currentReport.verdict?.pass ? 'PASS' : 'FAIL' }}
            </el-tag>
            <span class="metric">
              <strong>宽松</strong>
              {{ currentReport.verdict?.pass_count }}/{{ currentReport.verdict?.total }}
              <span class="metric-rate">
                ({{ Math.round((currentReport.verdict?.pass_rate ?? 0) * 100) }}%)
              </span>
            </span>
            <span
              v-if="currentReport.verdict?.strict_pass_count != null"
              class="metric"
            >
              <strong>严格</strong>
              {{ currentReport.verdict.strict_pass_count }}/{{ currentReport.verdict?.total }}
              <span class="metric-rate">
                ({{ Math.round((currentReport.verdict.strict_pass_rate ?? 0) * 100) }}%)
              </span>
            </span>
            <span
              v-if="currentReport.verdict?.intent_diverged_count != null
                && currentReport.verdict.intent_diverged_count > 0"
              class="metric diverged"
            >
              intent 偏离 {{ currentReport.verdict.intent_diverged_count }}/{{ currentReport.verdict?.total }}
            </span>
            <span v-if="currentReport.verdict?.threshold != null" class="threshold">
              阈值 {{ Math.round((currentReport.verdict.threshold ?? 0) * 100) }}%
            </span>
            <span v-if="currentReport.models?.run_chat">含 Chat 回复</span>
            <span v-else>仅 route-preview（无 Chat 调用）</span>
            <span>{{ currentReport.duration_sec }}s</span>
            <span v-if="currentReport.persona_ref" class="persona-tag">
              {{ currentReport.persona_ref }}
            </span>
            <el-button
              size="small"
              :loading="downloadingRunId === currentReport.run_id"
              @click="onDownloadRun(currentReport.run_id)"
            >
              下载到本地
            </el-button>
          </div>

          <div class="legend">
            <el-tag type="success" size="small" effect="plain">✓</el-tag>
            <span>严格通过</span>
            <el-tag type="warning" size="small" effect="plain">🟡</el-tag>
            <span>intent 偏离但记忆召回救回（lenient pass）</span>
            <el-tag type="danger" size="small" effect="plain">✗</el-tag>
            <span>失败</span>
          </div>

          <el-table
            :data="currentReport.cases || []"
            stripe
            highlight-current-row
            style="width: 100%"
            @row-click="openCase"
          >
            <el-table-column prop="id" label="Case" width="100" />
            <el-table-column prop="bucket" label="桶" width="130" />
            <el-table-column label="用户句" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.message }}</template>
            </el-table-column>
            <el-table-column label="Intent" width="120">
              <template #default="{ row }">
                {{ row.candidate?.l1?.actual_intent || '—' }}
              </template>
            </el-table-column>
            <el-table-column label="L1" width="80" align="center">
              <template #default="{ row }">
                <el-tag
                  :type="caseTagType(row.candidate?.l1)"
                  size="small"
                  effect="plain"
                >
                  {{ caseTagText(row.candidate?.l1) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" align="center">
              <template #default="{ row }">
                <el-button link type="primary" @click.stop="openCase(row)">穿透</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <el-empty v-else description="选择左侧报告，或点击「重新评测」" />
      </main>
        </div>
      </el-tab-pane>

      <el-tab-pane label="单条调试" name="query">
        <EvalQueryPanel />
      </el-tab-pane>

      <el-tab-pane label="线上聊天记录" name="chat">
        <EvalChatPanel />
      </el-tab-pane>
    </el-tabs>

    <el-drawer
      v-model="caseDrawerVisible"
      :title="activeCase ? `Case ${activeCase.id} · ${activeCase.bucket}` : 'Case 明细'"
      direction="rtl"
      size="720px"
    >
      <template v-if="activeCase">
        <div class="case-nav">
          <el-button size="small" :disabled="!prevCase" @click="goCase(-1)">上一条</el-button>
          <el-button size="small" :disabled="!nextCase" @click="goCase(1)">下一条</el-button>
        </div>

        <div class="case-layout">
          <aside class="case-expect">
            <h4>Case 设定</h4>
            <div class="expect-block">
              <div class="label">触发消息</div>
              <div class="value user-msg">{{ activeCase.message }}</div>
            </div>
            <div v-if="activeCase.history?.length" class="expect-block">
              <div class="label">上下文（{{ activeCase.history.length }} 轮）</div>
              <div
                v-for="(h, i) in activeCase.history"
                :key="i"
                class="hist-line"
                :class="h.role"
              >
                <span class="role">{{ h.role }}</span>{{ h.content }}
              </div>
            </div>
            <div class="expect-block">
              <div class="label">期望</div>
              <pre class="expect-json">{{ JSON.stringify(activeCase.expect, null, 2) }}</pre>
            </div>
            <div v-if="activeCase.candidate?.l1" class="expect-block">
              <div class="label">L1 自动判分</div>
              <ul class="l1-list">
                <li>
                  intent: {{ activeCase.candidate.l1.actual_intent }}
                  {{ activeCase.candidate.l1.intent_match ? '✓' : '✗' }}
                  <span
                    v-if="activeCase.candidate.l1.intent_source"
                    class="muted"
                  >
                    （{{ activeCase.candidate.l1.intent_source }}
                    <template v-if="activeCase.candidate.l1.intent_confidence != null">
                      · {{ Math.round((activeCase.candidate.l1.intent_confidence ?? 0) * 100) }}%
                    </template>）
                  </span>
                </li>
                <li>
                  keywords:
                  {{ activeCase.candidate.l1.keyword_hits }}/{{ activeCase.candidate.l1.keyword_total }}
                </li>
                <li>
                  严格: <strong>{{ activeCase.candidate.l1.pass_strict ? '通过' : '未通过' }}</strong>
                  · 宽松:
                  <strong>{{ activeCase.candidate.l1.pass_lenient ? '通过' : '未通过' }}</strong>
                  <el-tag
                    v-if="activeCase.candidate.l1.saved_by_recall"
                    type="warning"
                    size="small"
                    effect="plain"
                    style="margin-left: 6px"
                  >
                    🟡 召回救回
                  </el-tag>
                </li>
                <li v-if="activeCase.candidate.l1.boundary_violations?.length">
                  边界违规: {{ activeCase.candidate.l1.boundary_violations.join(', ') }}
                </li>
                <li v-if="activeCase.candidate.l1.reply_check_skipped" class="muted">
                  reply 检查已跳过（未调用 Chat）
                </li>
              </ul>
            </div>
            <div v-if="activeCase.candidate?.reply" class="expect-block">
              <div class="label">MemoBot 回复</div>
              <div class="reply-box">{{ activeCase.candidate.reply }}</div>
            </div>
          </aside>

          <section class="case-prompt">
            <PromptChainPanel
              :prompt-data="casePrompt"
              title="思考链 · Prompt 穿透"
              :default-expand-llm="true"
            />
          </section>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="personaDrawerVisible"
      title="persona_a_zhang · 老张（原始数据）"
      direction="rtl"
      size="640px"
    >
      <div v-loading="personaLoading" class="persona-drawer">
        <el-alert
          v-if="personaData && !personaData.seeded"
          type="warning"
          :closable="false"
          show-icon
          class="persona-warn"
        >
          尚未灌库或数据为空，请先点「灌库 · 老张」。
        </el-alert>
        <pre v-if="personaRawText" class="persona-raw">{{ personaRawText }}</pre>
        <el-empty v-else-if="!personaLoading" description="无数据" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import type { UploadRawFile } from 'element-plus'
import PromptChainPanel from '../components/PromptChainPanel.vue'
import EvalQueryPanel from '../components/EvalQueryPanel.vue'
import EvalChatPanel from '../components/EvalChatPanel.vue'
import {
  useEvalStore,
  type EvalCaseL1,
  type EvalCaseResult,
} from '../stores/eval'

function caseTagType(l1?: EvalCaseL1 | null): 'success' | 'warning' | 'danger' {
  if (!l1) return 'danger'
  if (l1.pass_strict) return 'success'
  if (l1.pass_lenient) return 'warning'
  return 'danger'
}

function caseTagText(l1?: EvalCaseL1 | null): string {
  if (!l1) return '✗'
  if (l1.pass_strict) return '✓'
  if (l1.pass_lenient) return '🟡'
  return '✗'
}

const evalStore = useEvalStore()
const { runs, currentReport, jobStatus, loading } = storeToRefs(evalStore)

const activeTab = ref<'batch' | 'query'>('batch')

const runType = ref<'smoke' | 'full'>('smoke')
const runChat = ref(false)
const starting = ref(false)
const seeding = ref(false)
const personaLoading = ref(false)
const personaDrawerVisible = ref(false)
const personaData = ref<Record<string, unknown> | null>(null)
const pageError = ref<string | null>(null)
const selectedRunId = ref<string | null>(null)
const caseDrawerVisible = ref(false)
const activeCase = ref<EvalCaseResult | null>(null)
const downloadingRunId = ref<string | null>(null)

let pollTimer: ReturnType<typeof setInterval> | null = null

const casePrompt = computed(() =>
  activeCase.value ? evalStore.casePromptData(activeCase.value) : undefined,
)

const caseList = computed(() => currentReport.value?.cases ?? [])

const activeIndex = computed(() =>
  activeCase.value
    ? caseList.value.findIndex((c) => c.id === activeCase.value!.id)
    : -1,
)

const prevCase = computed(() =>
  activeIndex.value > 0 ? caseList.value[activeIndex.value - 1] : null,
)
const nextCase = computed(() =>
  activeIndex.value >= 0 && activeIndex.value < caseList.value.length - 1
    ? caseList.value[activeIndex.value + 1]
    : null,
)

function formatTime(iso?: string) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN')
  } catch {
    return iso
  }
}

const buttonLabel = computed(() => {
  if (starting.value) return '正在启动…'
  if (jobStatus.value.running) {
    return `评测中 ${jobStatus.value.progress ?? 0}/${jobStatus.value.total ?? '?'}`
  }
  return '重新评测'
})

const personaRawText = computed(() =>
  personaData.value ? JSON.stringify(personaData.value, null, 2) : '',
)

async function refreshRuns() {
  try {
    await evalStore.fetchRuns()
    pageError.value = null
    if (selectedRunId.value) {
      await evalStore.fetchReport(selectedRunId.value)
    }
  } catch (e) {
    pageError.value = e instanceof Error ? e.message : '加载失败'
  }
}

async function selectRun(runId: string) {
  selectedRunId.value = runId
  await evalStore.fetchReport(runId)
}

async function onDownloadRun(runId: string) {
  downloadingRunId.value = runId
  try {
    await evalStore.downloadReport(runId)
    ElMessage.success(`已下载 ${runId}_report.json`)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '下载失败')
  } finally {
    downloadingRunId.value = null
  }
}

async function onSeedPersona() {
  seeding.value = true
  try {
    const res = await evalStore.seedPersona()
    ElMessage.success(`已灌库 ${res.display_name || 'persona_a_zhang'}（${res.episodic_added} 条情节记忆）`)
    if (personaDrawerVisible.value) {
      personaData.value = await evalStore.fetchPersonaData()
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '灌库失败')
  } finally {
    seeding.value = false
  }
}

async function onViewPersona() {
  personaDrawerVisible.value = true
  personaLoading.value = true
  try {
    personaData.value = await evalStore.fetchPersonaData()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '读取失败')
  } finally {
    personaLoading.value = false
  }
}

async function onRerun() {
  starting.value = true
  pageError.value = null
  try {
    await evalStore.startEval(runType.value, runChat.value)
    ElMessage.success('评测已开始')
    await pollOnce()
    startPolling()
  } catch (e) {
    const msg = e instanceof Error ? e.message : '启动失败'
    pageError.value = msg
    ElMessage.error(msg)
  } finally {
    starting.value = false
  }
}

async function pollOnce() {
  try {
    await evalStore.fetchJobStatus()
  } catch (e) {
    pageError.value = e instanceof Error ? e.message : '状态查询失败'
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      await evalStore.fetchJobStatus()
    } catch (e) {
      pageError.value = e instanceof Error ? e.message : '状态查询失败'
      stopPolling()
      return
    }
    if (!jobStatus.value.running) {
      stopPolling()
      await refreshRuns()
      const rid = jobStatus.value.run_id
      if (rid) {
        selectedRunId.value = rid
        try {
          await evalStore.fetchReport(rid)
        } catch (e) {
          pageError.value = e instanceof Error ? e.message : '加载报告失败'
        }
      }
      if (jobStatus.value.error) {
        pageError.value = jobStatus.value.error
        ElMessage.error(jobStatus.value.error)
      } else if (rid) {
        ElMessage.success('评测完成')
      }
    }
  }, 1500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function onImport(file: UploadRawFile) {
  try {
    const runId = await evalStore.importReport(file as unknown as File)
    ElMessage.success(`已导入 ${runId}`)
    selectedRunId.value = runId
    await evalStore.fetchReport(runId)
  } catch {
    ElMessage.error('导入失败')
  }
  return false
}

function openCase(row: EvalCaseResult) {
  activeCase.value = row
  caseDrawerVisible.value = true
}

function goCase(delta: number) {
  const idx = activeIndex.value + delta
  if (idx >= 0 && idx < caseList.value.length) {
    activeCase.value = caseList.value[idx]
  }
}

onMounted(async () => {
  await refreshRuns()
  try {
    await evalStore.fetchJobStatus()
    if (jobStatus.value.running) startPolling()
  } catch (e) {
    pageError.value = e instanceof Error ? e.message : '无法连接评测服务'
  }
})

onUnmounted(stopPolling)

watch(caseDrawerVisible, (v) => {
  if (!v) activeCase.value = null
})
</script>

<style scoped>
.eval-view {
  padding: 24px;
  max-width: 1400px;
  margin: 0 auto;
  min-height: 100%;
}
.eval-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.eval-header h2 {
  margin: 0 0 4px;
  font-size: 22px;
}
.subtitle {
  margin: 0;
  font-size: 13px;
  color: #909399;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.eval-tabs :deep(.el-tabs__header) {
  margin-bottom: 16px;
}
.batch-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.batch-hint {
  margin: 0 0 16px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
.persona-tag {
  font-size: 12px;
  color: #606266;
  background: #f0f2f5;
  padding: 2px 8px;
  border-radius: 4px;
}
.job-alert {
  margin-bottom: 16px;
}
.eval-body {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}
.run-list {
  width: 260px;
  flex-shrink: 0;
  background: #fff;
  border-radius: 10px;
  padding: 12px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  max-height: calc(100vh - 180px);
  overflow-y: auto;
}
.aside-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 10px;
}
.run-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 6px;
  border: 1px solid transparent;
}
.run-item:hover {
  background: #f5f7fa;
}
.run-item.active {
  background: #f3f0ff;
  border-color: #a78bfa;
}
.run-id {
  font-size: 12px;
  font-weight: 600;
  color: #303133;
  word-break: break-all;
}
.run-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: 12px;
  color: #606266;
}
.run-type {
  color: #909399;
}
.run-time {
  font-size: 11px;
  color: #909399;
  margin-top: 2px;
}
.run-download {
  margin-top: 4px;
  padding: 0;
  height: auto;
}
.run-detail {
  flex: 1;
  background: #fff;
  border-radius: 10px;
  padding: 16px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  min-height: 400px;
}
.summary-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;
  font-size: 13px;
  color: #606266;
  flex-wrap: wrap;
}
.summary-bar .metric strong {
  color: #303133;
  margin-right: 4px;
}
.summary-bar .metric-rate {
  color: #909399;
  margin-left: 2px;
}
.summary-bar .metric.diverged {
  color: #e6a23c;
}
.summary-bar .threshold {
  font-size: 12px;
  color: #909399;
}
.legend {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  font-size: 12px;
  color: #909399;
  flex-wrap: wrap;
}
.strict-rate {
  font-size: 11px;
  color: #909399;
}
.l1-list .muted {
  color: #909399;
  font-size: 11px;
}
.case-nav {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.case-layout {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.case-expect {
  background: #fafafa;
  border-radius: 8px;
  padding: 14px;
}
.case-expect h4 {
  margin: 0 0 12px;
  font-size: 14px;
}
.expect-block {
  margin-bottom: 12px;
}
.expect-block .label {
  font-size: 11px;
  color: #909399;
  margin-bottom: 4px;
}
.user-msg {
  font-size: 14px;
  color: #303133;
  font-weight: 500;
}
.hist-line {
  font-size: 12px;
  line-height: 1.5;
  margin-bottom: 4px;
  color: #606266;
}
.hist-line .role {
  display: inline-block;
  width: 36px;
  font-weight: 600;
  color: #409eff;
}
.hist-line.user .role {
  color: #a78bfa;
}
.expect-json {
  font-size: 11px;
  background: #fff;
  padding: 8px;
  border-radius: 6px;
  margin: 0;
  overflow: auto;
}
.l1-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: #606266;
}
.reply-box {
  font-size: 13px;
  line-height: 1.6;
  background: #fff;
  padding: 10px;
  border-radius: 6px;
  white-space: pre-wrap;
}
.persona-drawer {
  min-height: 200px;
}
.persona-warn {
  margin-bottom: 12px;
}
.persona-raw {
  margin: 0;
  font-size: 11px;
  line-height: 1.45;
  background: #f5f7fa;
  padding: 12px;
  border-radius: 8px;
  overflow: auto;
  max-height: calc(100vh - 120px);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
