<template>
  <el-drawer
    :model-value="visible"
    direction="rtl"
    size="780px"
    :title="title"
    @update:model-value="emit('update:visible', $event)"
  >
    <template v-if="turn">
      <!-- 顶部：用户句 + 回复 + 总评 -->
      <section class="block">
        <div class="hdr">
          <el-tag :type="finalTagType" effect="plain">
            综合：{{ finalLabel }}
          </el-tag>
          <el-tag
            v-if="turn.review?.snapshot_status"
            size="small"
            effect="plain"
            type="info"
          >
            快照：{{ snapshotLabel }}
          </el-tag>
          <el-tag
            v-for="code in turn.review?.suggested_root_cause || []"
            :key="code"
            size="small"
            effect="plain"
            class="cause"
          >
            归因 {{ code }} {{ causeName(code) }}
          </el-tag>
        </div>
        <div class="dialog">
          <div class="dialog-line user">
            <span class="role">user</span>{{ turn.input?.user_message }}
          </div>
          <div class="dialog-line asst">
            <span class="role">asst</span>{{ turn.output?.assistant_reply || '（无回复）' }}
            <el-tag v-if="turn.output?.error" type="danger" size="small" effect="plain">
              {{ turn.output.error }}
            </el-tag>
          </div>
        </div>
      </section>

      <!-- L0 结构检查 -->
      <section class="block">
        <h4 class="block-title">L0 · 结构自检（一致性检查）</h4>
        <el-empty
          v-if="!checks.length"
          description="该轮无 prompt_meta，跳过 L0"
          :image-size="40"
        />
        <ul v-else class="rule-list">
          <li v-for="c in checks" :key="c.id">
            <span :class="['mark', c.pass ? 'ok' : c.severity === 'high' ? 'bad' : 'warn']">
              {{ c.pass ? '✓' : c.severity === 'high' ? '❌' : '🟡' }}
            </span>
            <code>{{ c.id }}</code>
            <el-tag size="small" effect="plain" :type="c.severity === 'high' ? 'danger' : 'warning'">
              {{ c.severity }}
            </el-tag>
            <span v-if="c.detail" class="detail">{{ c.detail }}</span>
          </li>
        </ul>
      </section>

      <!-- L1 启发式规则 -->
      <section class="block">
        <h4 class="block-title">L1 · 启发式规则（基于 prompt_meta 和当时记忆池）</h4>
        <ul class="rule-list">
          <li v-for="r in turn.review?.rules || []" :key="r.id">
            <span :class="['mark', ruleMarkClass(r.status)]">
              {{ ruleMarkText(r.status) }}
            </span>
            <code>{{ r.id }}</code>
            <el-tag size="small" effect="plain" :type="ruleTagType(r.status)">
              {{ r.status }}
            </el-tag>
            <span v-if="r.detail" class="detail">{{ r.detail }}</span>
            <span v-if="r.attribution?.length" class="attr">
              → {{ r.attribution.join(', ') }}
            </span>
          </li>
        </ul>
      </section>

      <!-- 时点快照统计 -->
      <section v-if="snapshotStats && Object.keys(snapshotStats).length" class="block">
        <h4 class="block-title">当时记忆池快照（snapshot_at_turn）</h4>
        <div class="snap-grid">
          <div v-for="(v, k) in snapshotStats" :key="k">
            <span class="snap-k">{{ k }}</span>
            <span class="snap-v">{{ String(v) }}</span>
          </div>
        </div>
      </section>

      <!-- 思考链穿透：复用 PromptChainPanel -->
      <section v-if="promptChainData" class="block">
        <h4 class="block-title">思考链穿透</h4>
        <PromptChainPanel
          :prompt-data="promptChainData"
          :default-expand-llm="false"
        />
      </section>

      <!-- 当前记忆库（仅在用户主动加载时） -->
      <section v-if="snapshot" class="block">
        <h4 class="block-title">
          当前（评估时刻）记忆库
          <span class="muted">— 用于「数据是否入库」对照</span>
        </h4>
        <el-collapse>
          <el-collapse-item title="profile" name="p">
            <pre class="json">{{ JSON.stringify(snapshot.profile_json, null, 2) }}</pre>
          </el-collapse-item>
          <el-collapse-item :title="`events (${snapshot.events?.length ?? 0})`" name="e">
            <pre class="json">{{ JSON.stringify(snapshot.events, null, 2) }}</pre>
          </el-collapse-item>
          <el-collapse-item :title="`episodic (${snapshot.episodic?.length ?? 0})`" name="m">
            <pre class="json">{{ JSON.stringify(snapshot.episodic, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </section>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import PromptChainPanel from './PromptChainPanel.vue'
import type { AuditTurn } from '../stores/evalChat'

const props = defineProps<{
  visible: boolean
  turn: AuditTurn | null
  snapshot?: Record<string, any> | null
}>()
const emit = defineEmits<{ 'update:visible': [boolean] }>()

const title = computed(() => {
  if (!props.turn) return 'Turn'
  return `Turn #${props.turn.index} · ${props.turn.turn_id ?? ''}`
})

const checks = computed(() =>
  props.turn?.audit?.derived?.consistency_checks || [],
)

const snapshotStats = computed(() =>
  (props.turn?.audit?.prompt_meta?.snapshot_stats as Record<string, unknown>) || {},
)

const promptChainData = computed(() => {
  const meta = props.turn?.audit?.prompt_meta
  if (!meta) return null
  return {
    version: 'turn_meta_v1',
    memories: [],
    system: meta.system || '',
    route: meta.route,
    activated: meta.activated,
    context_layers: meta.context_layers,
    model: meta.model,
    composed_at: meta.composed_at,
    llm_request: meta.llm_request,
  }
})

const finalLabel = computed(() => {
  const s = props.turn?.review?.final_status
  if (s === 'ok') return '✓ 合理'
  if (s === 'suspicious') return '🟡 可疑'
  if (s === 'bad') return '❌ 异常'
  return '— 跳过'
})
const finalTagType = computed<'success' | 'warning' | 'danger' | 'info'>(() => {
  const s = props.turn?.review?.final_status
  if (s === 'ok') return 'success'
  if (s === 'suspicious') return 'warning'
  if (s === 'bad') return 'danger'
  return 'info'
})

const snapshotLabel = computed(() => {
  const s = props.turn?.review?.snapshot_status
  if (s === 'at_turn') return '当时记忆池'
  if (s === 'context_only') return '兼容旧轮（仅 context_layers）'
  return '无 prompt_meta'
})

function ruleMarkClass(status: string) {
  if (status === 'pass') return 'ok'
  if (status === 'suspicious') return 'warn'
  if (status === 'fail') return 'bad'
  return 'skip'
}
function ruleMarkText(status: string) {
  if (status === 'pass') return '✓'
  if (status === 'suspicious') return '🟡'
  if (status === 'fail') return '❌'
  return '—'
}
function ruleTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'pass') return 'success'
  if (status === 'suspicious') return 'warning'
  if (status === 'fail') return 'danger'
  return 'info'
}

const CAUSE_NAMES: Record<string, string> = {
  A: '数据', B: 'Router', C: 'Context', D: 'Compose', E: 'LLM', F: '系统',
}
function causeName(code: string): string {
  return CAUSE_NAMES[code] || ''
}
</script>

<style scoped>
.block {
  margin-bottom: 16px;
  padding: 12px 14px;
  background: #fafafa;
  border-radius: 6px;
}
.block-title {
  margin: 0 0 8px;
  font-size: 14px;
  color: #303133;
  font-weight: 600;
}
.hdr {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.cause {
  margin-right: 0;
}
.dialog {
  background: #fff;
  border-radius: 4px;
  padding: 8px 10px;
}
.dialog-line {
  padding: 4px 0;
  font-size: 14px;
  line-height: 1.6;
}
.dialog-line .role {
  display: inline-block;
  width: 44px;
  font-size: 12px;
  color: #909399;
  font-weight: 600;
}
.dialog-line.user .role { color: #5470c6; }
.dialog-line.asst .role { color: #91cc75; }

.rule-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.rule-list li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 0;
  font-size: 13px;
}
.rule-list code {
  background: #eef0f3;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
}
.mark { font-weight: 700; }
.mark.ok { color: #67c23a; }
.mark.warn { color: #e6a23c; }
.mark.bad { color: #f56c6c; }
.mark.skip { color: #909399; }
.detail { color: #606266; }
.attr { color: #909399; font-size: 12px; margin-left: 4px; }

.snap-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 16px;
  font-size: 13px;
}
.snap-k { color: #909399; margin-right: 6px; }
.snap-v { color: #303133; font-weight: 500; }

.json {
  font-size: 12px;
  background: #1e1e2f;
  color: #d6deeb;
  padding: 10px;
  border-radius: 4px;
  max-height: 260px;
  overflow: auto;
}
.muted { color: #909399; font-weight: 400; }
</style>
