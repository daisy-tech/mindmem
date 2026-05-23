export interface MemoryTag {
  name: string
  color: string
  type: 'success' | 'warning' | 'danger' | 'info' | 'primary'
}

const TAG_RULES: Array<{ tag: MemoryTag; keywords: string[] }> = [
  {
    tag: { name: '技术', color: '#409eff', type: 'primary' },
    keywords: [
      '开发', '代码', '模型', '工具', '平台', 'AI', '大模型', 'API',
      '编程', '框架', '算法', '数据', '系统', '接口', '部署', '架构',
      '前端', '后端', '数据库', '服务', 'Vue', 'Python', 'Docker',
      '千问', '豆包', 'DeepSeek', '通义', 'GPT', '聚合', '插件',
    ],
  },
  {
    tag: { name: '学习', color: '#67c23a', type: 'success' },
    keywords: [
      '学习', '研究', '了解', '探索', '尝试', '实验', '测试', '调研',
      '阅读', '笔记', '总结', '理解', '分析', '思考', '方案',
    ],
  },
  {
    tag: { name: '情绪', color: '#e6a23c', type: 'warning' },
    keywords: [
      '开心', '不开心', '难过', '高兴', '烦', '焦虑', '担心', '兴奋',
      '愉快', '沮丧', '压力', '轻松', '满足', '失望', '郁闷', '感动',
      '害怕', '紧张', '期待', '无聊',
    ],
  },
  {
    tag: { name: '生活', color: '#909399', type: 'info' },
    keywords: [
      '下雨', '天气', '出门', '吃饭', '睡觉', '购物', '运动', '散步',
      '旅行', '朋友', '家人', '健康', '休息', '周末', '假期', '外出',
      '天晴', '下雪', '热', '冷',
    ],
  },
  {
    tag: { name: '工作', color: '#f56c6c', type: 'danger' },
    keywords: [
      '工作', '项目', '需求', '会议', '任务', '计划', '目标', '进度',
      '上线', '发布', '迭代', '评审', '方案', '汇报', '客户', '产品',
    ],
  },
]

export function extractTags(text: string): MemoryTag[] {
  const matched: MemoryTag[] = []
  for (const rule of TAG_RULES) {
    if (rule.keywords.some((kw) => text.includes(kw))) {
      matched.push(rule.tag)
    }
  }
  if (matched.length === 0) {
    matched.push({ name: '其他', color: '#c0c4cc', type: 'info' })
  }
  return matched
}

export function getAllTags(): MemoryTag[] {
  return TAG_RULES.map((r) => r.tag)
}
