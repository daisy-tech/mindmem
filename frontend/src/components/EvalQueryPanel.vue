<template>
  <div class="query-panel">
    <p class="query-hint">使用当前登录账号的<strong>真实记忆库</strong>，不会切换为合成用户老张。</p>
    <div class="query-layout">
      <!-- 左侧：输入 + 草稿列表 -->
      <aside class="query-input">
        <section class="block">
          <div class="block-head">
            <span class="block-title">对话上下文</span>
            <el-button size="small" link type="primary" @click="addHistoryTurn">
              + 添加一轮
            </el-button>
          </div>
          <el-empty
            v-if="history.length === 0"
            description="无上下文，可直接输入下方消息"
            :image-size="40"
          />
          <div v-for="(h, i) in history" :key="i" class="hist-row">
            <el-select v-model="h.role" size="small" style="width: 88px">
              <el-option label="user" value="user" />
              <el-option label="assistant" value="assistant" />
            </el-select>
            <el-input
              v-model="h.content"
              type="textarea"
              :rows="2"
              placeholder="内容"
              size="small"
            />
            <el-button size="small" link type="danger" @click="history.splice(i, 1)">
              删
            </el-button>
          </div>
        </section>

        <section class="block">
          <div class="block-title required">本轮用户消息</div>
          <el-input
            v-model="message"
            type="textarea"
            :rows="3"
            placeholder="例如：她最近还是很累"
          />
        </section>

        <section class="block">
          <div class="block-title">人格（仅本次，不改设置）</div>
          <el-radio-group v-model="personality" size="small">
            <el-radio-button value="introvert">内向</el-radio-button>
            <el-radio-button value="balanced">中性</el-radio-button>
            <el-radio-button value="extrovert">外向</el-radio-button>
          </el-radio-group>
        </section>

        <section class="block row-check">
          <el-checkbox v-model="runChat">调用 Chat 模型</el-checkbox>
        </section>

        <el-collapse class="expect-collapse">
          <el-collapse-item title="期望判定（可选，用于 PASS/FAIL）" name="expect">
            <div class="expect-form">
              <div class="expect-field">
                <span class="expect-label">期望 intent</span>
                <el-select
                  v-model="expectIntent"
                  clearable
                  placeholder="不判定"
                  size="small"
                  style="width: 100%"
                >
                  <el-option
                    v-for="opt in INTENT_OPTIONS"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
              </div>
              <div class="expect-field">
                <span class="expect-label">记忆关键词（逗号分隔）</span>
                <el-input
                  v-model="expectKeywords"
                  size="small"
                  placeholder="例如：妻子, 带娃, 累"
                />
              </div>
              <div class="expect-field">
                <span class="expect-label">回复禁止词（逗号分隔）</span>
                <el-input
                  v-model="expectForbiddenReply"
                  size="small"
                  placeholder="例如：加油, 听起来你"
                />
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>

        <div class="action-row">
          <el-button type="primary" :loading="running" @click="onRun">运行</el-button>
          <el-button @click="onClear">清空</el-button>
          <el-button
            :disabled="!lastResult"
            :loading="saving"
            @click="openSaveDialog"
          >
            保存草稿
          </el-button>
        </div>

        <section class="block drafts-block">
          <div class="block-head">
            <span class="block-title">已保存草稿</span>
            <el-button size="small" link @click="loadDraftList">刷新</el-button>
          </div>
          <el-empty v-if="drafts.length === 0" description="暂无草稿" :image-size="36" />
          <div
            v-for="d in drafts"
            :key="d.draft_id"
            class="draft-item"
            :class="{ active: activeDraftId === d.draft_id }"
            @click="loadDraft(d.draft_id)"
          >
            <div class="draft-title">{{ d.title || d.message }}</div>
            <div class="draft-meta">
              <span>{{ formatTime(d.created_at) }}</span>
              <el-tag v-if="d.has_reply" size="small" type="success" effect="plain">有回复</el-tag>
              <el-button
                size="small"
                link
                type="danger"
                @click.stop="onDeleteDraft(d.draft_id)"
              >
                删
              </el-button>
            </div>
          </div>
        </section>
      </aside>

      <!-- 右侧：结果 -->
      <main class="query-result" v-loading="running">
        <template v-if="lastResult">
          <!-- 评估结论 -->
          <div v-if="lastResult.evaluation" class="verdict-bar">
            <el-tag
              :type="lastResult.evaluation.pass ? 'success' : 'danger'"
              size="large"
              effect="dark"
            >
              {{ lastResult.evaluation.pass ? 'PASS' : 'FAIL' }}
            </el-tag>
            <span class="verdict-sub">
              自动检查 {{ lastResult.evaluation.auto_pass ? '通过' : '未通过' }}
              <template v-if="lastResult.evaluation.expect_pass != null">
                · 期望判定 {{ lastResult.evaluation.expect_pass ? '通过' : '未通过' }}
              </template>
            </span>
          </div>

          <div v-if="lastResult.evaluation?.auto_checks?.length" class="checks-section">
            <div class="block-title">自动检查</div>
            <div
              v-for="c in lastResult.evaluation.auto_checks"
              :key="c.id"
              class="check-row"
              :class="{ info: c.informational }"
            >
              <el-tag
                :type="c.informational ? 'info' : c.pass ? 'success' : 'danger'"
                size="small"
                effect="plain"
              >
                {{ c.informational ? '—' : c.pass ? '✓' : '✗' }}
              </el-tag>
              <span class="check-label">{{ c.label }}</span>
              <span class="check-detail">{{ c.detail }}</span>
            </div>
          </div>

          <div v-if="lastResult.evaluation?.l1" class="checks-section">
            <div class="block-title">期望判定（L1）</div>
            <div class="l1-grid">
              <div>
                intent：
                <strong>{{ lastResult.evaluation.l1.actual_intent }}</strong>
                {{ lastResult.evaluation.l1.intent_match ? '✓' : '✗' }}
              </div>
              <div>
                关键词：{{ lastResult.evaluation.l1.keyword_hits }}/{{ lastResult.evaluation.l1.keyword_total }}
              </div>
              <div v-if="lastResult.evaluation.l1.boundary_violations?.length">
                边界违规：{{ lastResult.evaluation.l1.boundary_violations.join('、') }}
              </div>
              <div v-if="lastResult.evaluation.l1.reply_violations?.length">
                回复禁词：{{ lastResult.evaluation.l1.reply_violations.join('、') }}
              </div>
              <div v-if="lastResult.evaluation.l1.system_violations?.length">
                Prompt 禁词：{{ lastResult.evaluation.l1.system_violations.join('、') }}
              </div>
            </div>
          </div>

          <div v-if="lastResult && !lastResult.evaluation" class="checks-section">
            <el-alert type="warning" :closable="false" show-icon>
              当前 backend 未返回评估结果，请同步最新代码并重启 backend。
            </el-alert>
          </div>

          <div class="timing-bar">
            <span>总耗时 {{ lastResult.timing_ms?.total ?? '—' }} ms</span>
            <span>Context {{ lastResult.timing_ms?.context ?? '—' }} ms</span>
            <span v-if="lastResult.run_chat">
              Chat {{ lastResult.timing_ms?.chat ?? '—' }} ms
              <template v-if="lastResult.tokens"> · {{ lastResult.tokens }} tokens</template>
            </span>
            <span>人格 {{ personalityLabel }}</span>
          </div>

          <div v-if="lastResult.reply" class="reply-section">
            <div class="block-title">MemoBot 回复</div>
            <div class="reply-box">{{ lastResult.reply }}</div>
          </div>

          <PromptChainPanel
            :prompt-data="promptData"
            title="思考链 · Prompt 穿透"
            :default-expand-llm="true"
          />
        </template>
        <el-empty v-else description="填写消息后点「运行」" :image-size="80" />
      </main>
    </div>

    <el-dialog v-model="saveDialogVisible" title="保存草稿" width="420px">
      <el-input v-model="draftTitle" placeholder="标题（默认取消息摘要）" />
      <template #footer>
        <el-button @click="saveDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="confirmSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import PromptChainPanel from './PromptChainPanel.vue'
import { useEvalStore, type QueryResult, type DraftSummary } from '../stores/eval'

const evalStore = useEvalStore()

const history = ref<Array<{ role: string; content: string }>>([])
const message = ref('')
const personality = ref<'introvert' | 'balanced' | 'extrovert'>('balanced')
const runChat = ref(false)
const running = ref(false)
const saving = ref(false)
const lastResult = ref<QueryResult | null>(null)
const drafts = ref<DraftSummary[]>([])
const activeDraftId = ref<string | null>(null)
const saveDialogVisible = ref(false)
const draftTitle = ref('')

const expectIntent = ref('')
const expectKeywords = ref('')
const expectForbiddenReply = ref('')

const INTENT_OPTIONS = [
  { value: 'casual', label: '闲聊 casual' },
  { value: 'self_summary', label: '自我总结 self_summary' },
  { value: 'relationship_topic', label: '关系话题 relationship_topic' },
  { value: 'emotional_support', label: '情绪支持 emotional_support' },
  { value: 'plan_followup', label: '计划跟进 plan_followup' },
  { value: 'correction', label: '纠错 correction' },
  { value: 'knowledge_task', label: '知识/工具 knowledge_task' },
  { value: 'preference_request', label: '偏好建议 preference_request' },
]

function splitCsv(s: string): string[] {
  return s.split(/[,，]/).map((x) => x.trim()).filter(Boolean)
}

function buildExpectPayload(): Record<string, unknown> | undefined {
  const expect: Record<string, unknown> = {}
  if (expectIntent.value) expect.intent = expectIntent.value
  const kw = splitCsv(expectKeywords.value)
  if (kw.length) expect.must_activate_keywords = kw
  const fr = splitCsv(expectForbiddenReply.value)
  if (fr.length) expect.forbidden_phrases_in_reply = fr
  return Object.keys(expect).length ? expect : undefined
}

function applyExpectFromDraft(exp?: Record<string, unknown>) {
  if (!exp) {
    expectIntent.value = ''
    expectKeywords.value = ''
    expectForbiddenReply.value = ''
    return
  }
  expectIntent.value = (exp.intent as string) || ''
  const kw = exp.must_activate_keywords
  expectKeywords.value = Array.isArray(kw) ? kw.join(', ') : ''
  const fr = exp.forbidden_phrases_in_reply
  expectForbiddenReply.value = Array.isArray(fr) ? fr.join(', ') : ''
}

const PERSONALITY_LABELS: Record<string, string> = {
  introvert: '内向型',
  balanced: '中性型',
  extrovert: '外向型',
}

const promptData = computed(() =>
  lastResult.value?.prompt_meta
    ? evalStore.promptFromMeta(lastResult.value.prompt_meta as Record<string, unknown>)
    : undefined,
)

const personalityLabel = computed(
  () => PERSONALITY_LABELS[personality.value] ?? personality.value,
)

function formatTime(iso?: string) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN')
  } catch {
    return iso
  }
}

function addHistoryTurn() {
  const role = history.value.length % 2 === 0 ? 'user' : 'assistant'
  history.value.push({ role, content: '' })
}

async function loadDraftList() {
  try {
    drafts.value = await evalStore.fetchDrafts()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载草稿失败')
  }
}

async function onRun() {
  if (!message.value.trim()) {
    ElMessage.warning('请输入本轮用户消息')
    return
  }
  running.value = true
  try {
    lastResult.value = await evalStore.runQuery({
      message: message.value.trim(),
      history: history.value.filter((h) => h.content.trim()),
      personality: personality.value,
      run_chat: runChat.value,
      expect: buildExpectPayload(),
    })
    activeDraftId.value = null
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '运行失败')
  } finally {
    running.value = false
  }
}

function onClear() {
  history.value = []
  message.value = ''
  lastResult.value = null
  activeDraftId.value = null
  applyExpectFromDraft(undefined)
}

function openSaveDialog() {
  if (!lastResult.value) return
  draftTitle.value = message.value.trim().slice(0, 40)
  saveDialogVisible.value = true
}

async function confirmSave() {
  if (!lastResult.value) return
  saving.value = true
  try {
    const res = await evalStore.saveDraft({
      draft_id: activeDraftId.value ?? undefined,
      title: draftTitle.value.trim() || undefined,
      input: {
        message: message.value.trim(),
        history: history.value.filter((h) => h.content.trim()),
        personality: personality.value,
        run_chat: runChat.value,
        expect: buildExpectPayload() ?? {},
      },
      result: {
        prompt_meta: lastResult.value.prompt_meta,
        reply: lastResult.value.reply,
        timing_ms: lastResult.value.timing_ms,
        run_chat: lastResult.value.run_chat,
        tokens: lastResult.value.tokens,
        evaluation: lastResult.value.evaluation,
        expect: lastResult.value.expect,
      },
    })
    activeDraftId.value = res.draft_id
    saveDialogVisible.value = false
    ElMessage.success(`已保存：${res.title}`)
    await loadDraftList()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function loadDraft(draftId: string) {
  try {
    const d = await evalStore.fetchDraft(draftId)
    activeDraftId.value = d.draft_id
    message.value = d.input?.message ?? ''
    history.value = (d.input?.history ?? []).map((h) => ({ ...h }))
    personality.value = (d.input?.personality as typeof personality.value) ?? 'balanced'
    runChat.value = !!d.input?.run_chat
    applyExpectFromDraft(d.input?.expect as Record<string, unknown> | undefined)
    if (d.result?.prompt_meta) {
      lastResult.value = {
        prompt_meta: d.result.prompt_meta,
        reply: d.result.reply ?? null,
        run_chat: d.result.run_chat ?? false,
        timing_ms: d.result.timing_ms,
        tokens: d.result.tokens,
        personality: d.input?.personality,
        evaluation: d.result.evaluation,
        expect: (d.result.expect ?? d.input?.expect) as Record<string, unknown> | undefined,
      }
    } else {
      lastResult.value = null
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载草稿失败')
  }
}

async function onDeleteDraft(draftId: string) {
  try {
    await ElMessageBox.confirm('确定删除这条草稿？', '提示', { type: 'warning' })
    await evalStore.deleteDraft(draftId)
    if (activeDraftId.value === draftId) activeDraftId.value = null
    await loadDraftList()
    ElMessage.success('已删除')
  } catch {
    /* cancelled */
  }
}

onMounted(loadDraftList)
</script>

<style scoped>
.query-panel {
  padding: 0;
  min-height: 480px;
}
.query-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
.query-layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}
.query-input {
  width: 380px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.query-result {
  flex: 1;
  background: #fff;
  border-radius: 10px;
  padding: 16px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  min-height: 400px;
}
.block {
  background: #fafafa;
  border-radius: 8px;
  padding: 12px;
}
.block-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.block-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.block-title.required::after {
  content: ' *';
  color: #f56c6c;
}
.hist-row {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  margin-bottom: 8px;
}
.hist-row .el-textarea {
  flex: 1;
}
.row-check {
  padding: 8px 12px;
}
.action-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.drafts-block {
  max-height: 240px;
  overflow-y: auto;
}
.draft-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
}
.draft-item:hover {
  background: #f0f0f0;
}
.draft-item.active {
  background: #f3f0ff;
  border-color: #a78bfa;
}
.draft-title {
  font-size: 13px;
  color: #303133;
  line-height: 1.4;
  word-break: break-all;
}
.draft-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: #909399;
}
.verdict-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  padding: 10px 12px;
  background: #fafafa;
  border-radius: 8px;
  border: 1px solid #ebeef5;
}
.verdict-sub {
  font-size: 13px;
  color: #606266;
}
.checks-section {
  margin-bottom: 14px;
  padding: 10px 12px;
  background: #fafafa;
  border-radius: 8px;
  border: 1px solid #ebeef5;
}
.check-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
  line-height: 1.5;
  padding: 4px 0;
}
.check-row.info {
  opacity: 0.85;
}
.check-label {
  font-weight: 500;
  color: #303133;
  min-width: 120px;
}
.check-detail {
  color: #606266;
  flex: 1;
}
.l1-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: #606266;
}
.expect-collapse {
  margin-bottom: 12px;
  border: none;
}
.expect-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.expect-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.expect-label {
  font-size: 12px;
  color: #606266;
}
.timing-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 12px;
  color: #606266;
  margin-bottom: 14px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 6px;
}
.reply-section {
  margin-bottom: 16px;
}
.reply-box {
  font-size: 14px;
  line-height: 1.6;
  background: #f5f7fa;
  padding: 12px;
  border-radius: 8px;
  white-space: pre-wrap;
}
</style>
