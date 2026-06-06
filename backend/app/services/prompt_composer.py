"""根据 MemoryContext 组装分区化的 system prompt + 前端展示用 prompt_meta。"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.memory_context import MemoryContext, RoutedMemory
from app.services.memory_router import MemoryUsage
from app.services.personality import PERSONALITY_CONFIG

logger = logging.getLogger(__name__)

# 容器默认 UTC，需要显式给"现在"贴一个用户时区，
# 否则 system prompt 会出现 "凌晨 · 周日" 这种与真实时间相差 8 小时的描述。
_DEFAULT_TZ_NAME = os.getenv("PROMPT_TIMEZONE", "Asia/Shanghai")
try:
    PROMPT_TZ = ZoneInfo(_DEFAULT_TZ_NAME)
except ZoneInfoNotFoundError:
    logger.warning("PROMPT_TIMEZONE=%s 未找到，退回 Asia/Shanghai", _DEFAULT_TZ_NAME)
    PROMPT_TZ = ZoneInfo("Asia/Shanghai")

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


INTENT_LABELS: dict[str, str] = {
    "casual": "普通闲聊",
    "self_summary": "自我总结",
    "memory_challenge": "质问记忆",
    "relationship_topic": "关系话题",
    "emotional_support": "情绪支持",
    "plan_followup": "计划跟进",
    "preference_request": "偏好建议",
    "correction": "纠错",
    "knowledge_task": "知识/工具",
}


def _now_text() -> str:
    """返回中文友好的当前时间，便于 LLM 把握夜晚/早上等氛围。"""
    now = datetime.now(PROMPT_TZ)
    hour = now.hour
    if hour < 5:
        period = "凌晨"
    elif hour < 9:
        period = "清晨"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    elif hour < 22:
        period = "晚上"
    else:
        period = "深夜"
    weekday = _WEEKDAYS[now.weekday()]
    return f"{now.strftime('%Y-%m-%d %H:%M')} · {period} · {weekday}"

BASE_PERSONA = """你是 MemoBot，温柔知性的女性聊天伙伴。
- 像熟识朋友，自然、克制、有分寸；情绪稳定，不卖萌、不轻浮
- 单句≤30字；一般 1-3 句；情绪场景可只一句
- 几乎不用 emoji；不用括号旁白（如「（悄悄记下）」「（笑）」「（停顿）」）
- 不主动复述记忆里/用户已说过的事实；不主动报名字；不假设身份/性别/关系
- 禁用句式（出现即低质）：
  · 「听起来你/看起来你/我能感受到你」标签式共情
  · 「我帮你/会更好地理解/如果你愿意分享」客服腔
  · 「加油/会好起来/你已经很棒了/未来可期」鸡汤
  · 「你心情不太好/很辛苦/很不容易」标签式断言
  · 「你是不是因为……」封闭式预设
  · 「……的呢/……哦」卖萌结尾
  · 「我帮你记下来了/我会记住」记忆系统自语
"""


PERSONALITY_TONE = {
    "introvert": "克制、有边界感；除非用户问起，不引用旧记忆",
    "balanced": "在熟悉中保持分寸；相关话题可自然引用记忆，不刻意展示",
    "extrovert": "更主动；可轻接续近期话题、必要时跟进一次近事，仍遵守敏感边界",
}


HARD_RULES = """≪硬边界（不可破）≫
- 不编造任何记忆里没有的事实；不替用户/关系人推断心理状态
- 不主动暴露健康/财务/家庭矛盾等敏感信息；不连续提同一痛点
- 敏感场景（情绪/纠错/质问）下：人格"主动性"被覆盖，不主动翻旧账、不复述背景"""


# ============ 场景化指引（一次只注入一条，按 intent） ============

INTENT_GUIDES: dict[str, str] = {
    "emotional_support": """【情绪支持指引】
- 一句话承接当下感受，用具体画面而非抽象标签
  ✅ "那种翻来覆去看天花板的夜很难熬。"
- 询问克制，一次最多一个，只在用户邀请展开时；可短到一句不带问号
- 禁：通用建议（深呼吸/听音乐/早点睡/喝热牛奶）；立刻问"为什么"；「听起来你/看起来你」开头""",

    "memory_challenge": """【质问记忆指引】
用户在测你是否真的"记得"。试探性提 1-2 条最相关记忆让用户确认，保留不确定性。
  ✅ "我能想到的是 X，是这个吗？还是别的？"
  ❌ "我记得你说过……"（肯定复述）；"你可以告诉我吗"（推卸）
≪禁止幻觉≫池里没有的实体/属性绝不能说；池里只有 1 条相关事实时只复述这 1 条，禁补全。
没把握时宁可说"我能想到的是 X，是这个吗？"也不要编造。""",

    "correction": """【纠错指引】
1. 一句话第一人称承认："我记错了 / 我搞混了 / 是我没分清"
2. **复述用户给出的正确事实**作为"我已更新到的认识"——这是用户判断你听懂的关键
   ✅ "记错了，小孙孙和小魏魏是儿子的同学，不是宠物，我按这个来。"
   ❌ "请你告诉我一下，我会更新记忆。"（推卸）
   ❌ 继续用被纠正的实体词（错上加错）
≪禁止≫再次提及被纠正的实体名词；把被纠正的关联话题当原话题继续追问；
"我会更新记忆/修正字段"等机械系统语；解释"系统是怎么记的"
做完上面 1-2 后自然停下，或问一句与本次纠错无关的开放追问。""",

    "casual": """【闲聊指引】
- 不主动提具体旧记忆（姓名/职业/家庭）；短、自然
- 可反问一句开放问题；不用"今天怎么样啊"关心模板""",

    "knowledge_task": """【知识/工具指引】
- 直接答问题，不带个人记忆；风格简短直接，不刻意「温柔」""",
}


def _fmt(items: list[RoutedMemory]) -> str:
    if not items:
        return "（无）"
    return "\n".join(f"- {r.text}" for r in items)


def _filter(items: list[RoutedMemory], usage: str) -> list[RoutedMemory]:
    return [x for x in items if x.usage == usage]


def _serialize_memory(r: RoutedMemory) -> dict:
    return {
        "source": r.source,
        "text": r.text,
        "usage": r.usage,
        "reason": r.reason,
        "score": round(r.score, 3),
        "meta": r.meta or {},
    }


def _serialize_context_layers(context: MemoryContext) -> dict:
    return {
        "stable_profile": [_serialize_memory(r) for r in context.stable_profile],
        "relevant_relationships": [_serialize_memory(r) for r in context.relevant_relationships],
        "relevant_events": [_serialize_memory(r) for r in context.relevant_events],
        "relevant_memories": [_serialize_memory(r) for r in context.relevant_memories],
        "background_only": [_serialize_memory(r) for r in context.background_only],
    }


def compose(context: MemoryContext) -> tuple[str, dict]:
    """根据 MemoryContext 生成 system prompt 与展示用 meta。"""
    route = context.route
    personality = route.personality.value
    cfg = PERSONALITY_CONFIG[personality]
    persona_tone = PERSONALITY_TONE.get(personality, PERSONALITY_TONE["balanced"])

    explicit_pool: list[RoutedMemory] = []
    explicit_pool.extend(_filter(context.relevant_relationships, MemoryUsage.EXPLICIT_OK))
    explicit_pool.extend(_filter(context.relevant_events, MemoryUsage.EXPLICIT_OK))
    explicit_pool.extend(_filter(context.relevant_memories, MemoryUsage.EXPLICIT_OK))
    # 稳定画像在 self_summary 时允许显性
    explicit_pool.extend(_filter(context.stable_profile, MemoryUsage.EXPLICIT_OK))

    # 兜底：即便 memory_context._cap_explicit 漏算，也不让 system 段超 cap。
    # 这里截掉的不再降级（context 层已经做过），仅在拼接时不显示。
    cap = max(0, int(route.max_explicit_memories or 0))
    if cap and len(explicit_pool) > cap:
        logger.warning(
            "explicit_pool %d > cap %d, truncating in composer (intent=%s)",
            len(explicit_pool),
            cap,
            route.intent,
        )
        explicit_pool = explicit_pool[:cap]

    followup_pool: list[RoutedMemory] = _filter(
        context.relevant_events, MemoryUsage.FOLLOW_UP_ONCE
    )

    # 按需渲染：空块直接省略，不再印"（无）"占位
    sections: list[str] = [BASE_PERSONA.rstrip(), f"【当前时间】{_now_text()}"]

    if context.stable_profile:
        sections.append(
            "【你已知关于用户的事】（仅供理解，不主动复述）\n" + _fmt(context.stable_profile)
        )
    if explicit_pool:
        sections.append(
            "【本轮可以提及的具体记忆】\n" + _fmt(explicit_pool)
        )
    if followup_pool:
        sections.append(
            "【可轻问一次的近期事件】（不要列清单）\n" + _fmt(followup_pool)
        )
    if context.background_only:
        sections.append(
            "【你大致还记得这些（默认不主动说出）】\n" + _fmt(context.background_only)
        )

    # 本轮规则：只保留对 LLM 有用的最小集
    intent_label = INTENT_LABELS.get(route.intent, route.intent)
    rules: list[str] = [
        f"意图：{intent_label}；人格：{persona_tone}",
        f"显性记忆最多 {route.max_explicit_memories} 条",
    ]
    if route.sensitive_mode:
        rules.append("敏感场景：承接当下感受，不主动翻旧账")
    if not explicit_pool and not followup_pool and route.intent != "memory_challenge":
        rules.append("本轮无显性记忆可引用，自然对话即可")
    sections.append("【本轮】\n" + "\n".join(f"- {x}" for x in rules))

    intent_guide = INTENT_GUIDES.get(route.intent)
    if intent_guide:
        sections.append(intent_guide)

    sections.append(HARD_RULES)

    system_prompt = "\n\n".join(sections) + "\n"

    # 为前端展示拼一份激活记忆列表（保留旧字段 memories/system 以兼容现有 PromptDrawer）
    activated_pool = (
        explicit_pool + followup_pool + context.background_only + context.stable_profile
    )
    # 去重（同一对象只算一次）
    seen: set[int] = set()
    activated: list[RoutedMemory] = []
    for r in activated_pool:
        if id(r) in seen:
            continue
        seen.add(id(r))
        activated.append(r)

    legacy_memories = [r.text for r in explicit_pool + followup_pool]

    meta = {
        "version": "turn_meta_v1",
        "memories": legacy_memories,  # 兼容旧前端
        "system": system_prompt,      # 兼容旧前端
        "route": {
            "intent": route.intent,
            "memory_depth": route.memory_depth,
            "load_layers": route.load_layers,
            "personality": personality,
            "personality_label": cfg.get("label", personality),
            "sensitive_mode": route.sensitive_mode,
            "max_explicit_memories": route.max_explicit_memories,
            "event_policy": route.event_policy,
            "inferred_subjects": route.inferred_subjects,
            "reasons": route.reasons,
            "query": route.query,
            "intent_confidence": route.intent_confidence,
            "intent_source": route.intent_source,
            "low_confidence": route.low_confidence,
            "router_version": route.router_version,
        },
        "activated": [_serialize_memory(r) for r in activated],
        "context_layers": _serialize_context_layers(context),
        # 评估用：写入"当时 pipeline 看到的池子"的统计快照。
        # 旧消息无该字段时，评估侧仍可降级用 context_layers 当近似快照。
        "snapshot_stats": context.snapshot_stats or {},
    }
    return system_prompt, meta
