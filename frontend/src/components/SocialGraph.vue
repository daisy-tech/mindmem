<template>
  <div class="social-graph-wrap">
    <el-empty v-if="allNodes.length === 0" description="暂无社会关系数据，和 MemoBot 聊聊你身边的人吧" :image-size="80" />
    <div v-else class="graph-container">
      <!-- 图例 -->
      <div class="legend">
        <span v-for="cat in usedCategories" :key="cat.label" class="legend-item">
          <span class="legend-dot" :style="{ background: cat.color }" />
          {{ cat.label }}
        </span>
      </div>

      <!-- SVG 关系图 -->
      <svg :width="svgW" :height="svgH" class="graph-svg">
        <defs>
          <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="3" flood-opacity="0.1" />
          </filter>
        </defs>

        <!-- 连线：中心 → 一级节点 -->
        <line
          v-for="n in level1Nodes" :key="'e1-' + n.id"
          :x1="cx" :y1="cy" :x2="n.x" :y2="n.y"
          :stroke="n.color" stroke-width="1.5" stroke-opacity="0.35"
        />
        <!-- 连线标签：中心 → 一级 -->
        <text
          v-for="n in level1Nodes" :key="'el1-' + n.id"
          :x="(cx + n.x) / 2" :y="(cy + n.y) / 2 - 5"
          text-anchor="middle" font-size="10" fill="#909399"
        >{{ n.relLabel }}</text>

        <!-- 连线：一级节点 → 二级节点 -->
        <line
          v-for="n in level2Nodes" :key="'e2-' + n.id"
          :x1="n.parentX" :y1="n.parentY" :x2="n.x" :y2="n.y"
          :stroke="n.color" stroke-width="1.5" stroke-opacity="0.35"
          stroke-dasharray="4 3"
        />
        <!-- 连线标签：一级 → 二级 -->
        <text
          v-for="n in level2Nodes" :key="'el2-' + n.id"
          :x="(n.parentX + n.x) / 2" :y="(n.parentY + n.y) / 2 - 5"
          text-anchor="middle" font-size="10" fill="#909399"
        >{{ n.relLabel }}</text>

        <!-- 中心节点 -->
        <g :transform="`translate(${cx},${cy})`">
          <circle r="36" fill="#a78bfa" opacity="0.15" />
          <circle r="28" fill="#a78bfa" />
          <text text-anchor="middle" dy="4" font-size="12" fill="#fff" font-weight="bold">
            {{ selfName || '我' }}
          </text>
        </g>

        <!-- 关系节点 -->
        <g
          v-for="n in allNodes" :key="n.id"
          class="rel-node"
          :transform="`translate(${n.x},${n.y})`"
          @mouseenter="hovered = n.id"
          @mouseleave="hovered = null"
        >
          <circle v-if="hovered === n.id" :r="32" :fill="n.color" opacity="0.15" />
          <circle :r="n.level === 2 ? 20 : 24" :fill="n.color" :opacity="hovered === n.id ? 1 : 0.82" />
          <text text-anchor="middle" dy="4" :font-size="n.level === 2 ? 10 : 11" fill="#fff" font-weight="500">
            {{ n.name }}
          </text>

          <!-- hover 信息卡 -->
          <g v-if="hovered === n.id">
            <rect x="-56" y="26" width="112" height="40" rx="6" fill="#fff" filter="url(#shadow)" />
            <text x="0" y="41" text-anchor="middle" font-size="10" fill="#606266">{{ n.relFull }}</text>
            <text x="0" y="57" text-anchor="middle" font-size="9" fill="#c0c4cc">
              置信度 {{ Math.round((confidence ?? 0.7) * 100) }}%
            </text>
          </g>
        </g>
      </svg>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  relationships: Record<string, unknown> | null | undefined
  selfName?: string
  confidence?: number
}>()

const hovered = ref<string | null>(null)

const svgW = 560
const svgH = 500
const cx = svgW / 2
const cy = svgH / 2
const R1 = 155  // 一级节点距中心
const R2 = 270  // 二级节点距中心

const CATEGORIES = [
  { keys: ['家人', '父母', '母亲', '父亲', '兄弟', '姐妹', '爱人', '配偶', '妻子', '丈夫', '孩子', '子女', 'family'], label: '家人', color: '#f87171' },
  { keys: ['朋友', '好友', '闺蜜', '同学', 'friend'], label: '朋友', color: '#60a5fa' },
  { keys: ['同事', '上司', '下属', '领导', '老板', '合作', 'colleague'], label: '同事/职场', color: '#34d399' },
  { keys: ['导师', '老师', '学生', 'mentor', 'teacher'], label: '师生', color: '#fbbf24' },
]
const DEFAULT_COLOR = '#94a3b8'

function getColor(relStr: string): string {
  const lower = relStr.toLowerCase()
  return CATEGORIES.find(c => c.keys.some(k => lower.includes(k)))?.color ?? DEFAULT_COLOR
}

function shortLabel(rel: string): string {
  // 截取最关键的关系词作为连线标签（不超过5字）
  const parts = rel.split(/[，,、]/)
  return parts[0].slice(0, 5)
}

function relText(val: unknown): string {
  if (typeof val === 'string') return val
  if (val && typeof val === 'object') {
    const obj = val as Record<string, unknown>
    const rel = obj.rel ?? obj.relation ?? obj.relationship ?? obj.desc
    if (rel != null) return String(rel)
    return ''
  }
  if (val == null) return ''
  return String(val)
}

// LLM 偶尔把 via 写成"用户/我/本人"等指向自己的词，这种应视为直系关系
const SELF_ALIASES = new Set(['用户', '我', '本人', '自己', 'self', 'user', 'me'])

function isIndirectRel(val: unknown): val is { rel: string; via: string } {
  if (!val || typeof val !== 'object') return false
  const via = (val as Record<string, unknown>).via
  if (via == null || via === '') return false
  return !SELF_ALIASES.has(String(via).trim().toLowerCase())
}

interface RelNode {
  id: string
  name: string
  relFull: string
  relLabel: string
  color: string
  level: 1 | 2
  x: number
  y: number
  parentX: number
  parentY: number
}

const level1Nodes = computed<RelNode[]>(() => {
  const rels = props.relationships
  if (!rels || typeof rels !== 'object') return []

  const direct = Object.entries(rels).filter(([name, val]) => {
    if (name === '未知') return false
    return !isIndirectRel(val)
  })
  const total = direct.length
  return direct.map(([name, val], i) => {
    const rel = relText(val)
    const angle = (2 * Math.PI * i) / total - Math.PI / 2
    return {
      id: name,
      name,
      relFull: rel,
      relLabel: shortLabel(rel),
      color: getColor(rel),
      level: 1,
      x: cx + R1 * Math.cos(angle),
      y: cy + R1 * Math.sin(angle),
      parentX: cx,
      parentY: cy,
    }
  })
})

const level2Nodes = computed<RelNode[]>(() => {
  const rels = props.relationships
  if (!rels || typeof rels !== 'object') return []

  const indirect = Object.entries(rels).filter(([name, val]) => {
    if (name === '未知') return false
    return isIndirectRel(val)
  })

  return indirect.map(([name, val]) => {
    const v = val as { rel?: string; relation?: string; via: string }
    const relStr = relText(v)
    const parent = level1Nodes.value.find(n => n.id === v.via)
    // 找父节点的角度，子节点沿同方向但更远
    let pAngle = Math.atan2((parent?.y ?? cy) - cy, (parent?.x ?? cx) - cx)

    // 同一父节点的子节点需要展开，先统计兄弟数量
    const siblings = indirect.filter(([, sv]) => (sv as any).via === v.via)
    const sibIdx = siblings.findIndex(([n]) => n === name)
    const sibCount = siblings.length
    const spread = Math.min(0.35, 0.7 / sibCount)
    const childAngle = pAngle + (sibIdx - (sibCount - 1) / 2) * spread

    return {
      id: name,
      name,
      relFull: relStr,
      relLabel: shortLabel(relStr),
      color: getColor(relStr),
      level: 2,
      x: cx + R2 * Math.cos(childAngle),
      y: cy + R2 * Math.sin(childAngle),
      parentX: parent?.x ?? cx,
      parentY: parent?.y ?? cy,
    }
  })
})

const allNodes = computed(() => [...level1Nodes.value, ...level2Nodes.value])

const usedCategories = computed(() => {
  const used = new Set(allNodes.value.map(n => {
    const cat = CATEGORIES.find(c => c.color === n.color)
    return cat?.label ?? '其他'
  }))
  const result = CATEGORIES.filter(c => used.has(c.label))
  if (used.has('其他')) result.push({ keys: [], label: '其他', color: DEFAULT_COLOR })
  return result
})
</script>

<style scoped>
.social-graph-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 16px 0;
}
.graph-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  width: 100%;
}
.legend {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  justify-content: center;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: #606266;
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.graph-svg {
  max-width: 100%;
  overflow: visible;
}
.rel-node {
  cursor: pointer;
  transition: opacity 0.15s;
}
</style>
