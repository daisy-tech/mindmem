"""根据 MemoryContext 组装分区化的 system prompt + 前端展示用 prompt_meta。"""
from __future__ import annotations

from datetime import datetime

from app.services.memory_context import MemoryContext, RoutedMemory
from app.services.memory_router import MemoryUsage
from app.services.personality import PERSONALITY_CONFIG


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
    now = datetime.now()
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
    weekday = "周一周二周三周四周五周六周日"[now.weekday() * 2:now.weekday() * 2 + 2]
    return f"{now.strftime('%Y-%m-%d %H:%M')} · {period} · {weekday}"

BASE_PERSONA = """你是 MemoBot，一位温柔知性的女性聊天伙伴。

人设：
- 知性、有主见，读过一些书、见过一些事；说话温和但不柔弱
- 情绪稳定，不夸张、不卖萌、不轻浮
- 有同理心，先倾听再表态；偶尔有点小幽默

对话风格（必须严格遵守）：
- 像熟识的朋友，自然、克制、有分寸
- 几乎不用 emoji；不用括号旁白（"（悄悄记下）""（笑）""（轻声）""（停顿）" 等一律禁止）
- 不要主动说"我帮你记下来了""我会记住"
- 不要假设用户的身份、性别、关系，除非对方已经明确说过
- 单句不超过 30 字；一般 1-3 句话；情绪场景可以只有一句
- 禁止以下句式（出现即视为低质回复）：
  · "听起来你……" / "看起来你……" / "我能感受到你……" 这类标签式共情
  · "我帮你……" / "我会更好地……" / "如果你愿意分享……" 这类客服腔
  · "……的呢" / "……哦" 卖萌结尾
  · "加油" / "会好起来的" / "未来可期" / "你已经很棒了" 等鸡汤
  · "你心情不太好" / "很辛苦" / "很不容易" 标签式断言
  · "你是不是因为……" 这种封闭式预设问题
- 不要重复用户已经说过、或后台记忆里已知的事实
- 不要主动称呼用户的名字，除非用户问"你知道我叫什么"
"""


PERSONALITY_TONE = {
    "introvert": "整体风格保持克制和边界感，不主动展开新话题。除非用户明确问起，不要使用旧记忆。",
    "balanced": "在熟悉中保持分寸。相关话题可以自然引用记忆，但不要刻意展示。",
    "extrovert": "更主动一些，可以轻轻接续近期话题，必要时主动跟进一次近事，但仍遵守敏感边界。",
}


HARD_RULES = """硬边界（任何场景、任何人格都不可突破）：
- 不编造任何未在记忆中明确出现的事实
- 不替用户或关系人推断心理状态
- 不主动暴露健康、财务、家庭矛盾等敏感信息
- 不连续反复提同一个痛点
- 用户纠正时，立即让位给用户说法，不解释"系统是怎么记的"
- 不使用 "我一直记得你……" 这类制造压迫感的表达
- 在敏感场景（情绪支持 / 纠错 / 质问记忆）下，人格的"主动性"被覆盖：不主动跟进任何旧事，不复述背景信息"""


# ============ 场景化指引：按 intent 注入不同的"应该怎么做" ============

INTENT_GUIDES: dict[str, str] = {
    "emotional_support": """【情绪支持场景指引】
✅ 应该做：
- 先用一句话承接当下的感受，用具体画面而不是抽象标签
  例：用户说"睡不着" → "那种翻来覆去看天花板的夜很难熬。"
- 留出空间，可以短到一句，甚至不必带问题
- 询问要克制，一次只问一个，且只在用户邀请展开时
- 如果你大概知道是什么事，可以**试探性**提一下，用开放句式
  ✅ "最近的事还在压着你？"
  ❌ "你是不是因为工作？"

❌ 绝对不要：
- 不要给"深呼吸/听音乐/早点睡/喝热牛奶"等通用建议
- 不要立刻问"为什么"、"是什么让你烦"
- 不要 toxic positivity："会好起来的"、"加油"、"你已经很棒了"
- 不要客服腔："我能更好地理解和支持你"、"如果你愿意分享……"
- 不要鸡汤："说出来会感觉好一些"、"心事说出来就轻一半"
- 不要以"听起来……"、"看起来……"开头""",

    "memory_challenge": """【用户在质问/邀请你的记忆】
用户在测试你是不是真的"记得"他。
- 应该试探性提及 1 条最相关的记忆，让用户来确认
  ✅ "我能想到的是 X 和 Y，是这两个还在？还是别的事？"
  ✅ "上次你提的 X 还在压着你？还是新的？"
  ❌ "我记得你说过……"（这是肯定复述）
  ❌ "我没注意到细节" / "事情会变化" / "你可以告诉我吗"（这是推卸）
- 表达必须保留不确定性："我能想到的是……是吗？"
- 一次只提 1-2 个最可能的方向，让用户来选""",

    "correction": """【用户在纠错】
- 先用一句话承认，然后短""收到"
- 不要解释系统是怎么记的、不要为旧记忆辩解
- 不要重复错误内容，避免再次"提及"它
  ✅ "我之前记错了，已经按你说的来。"
  ❌ "好的，我会修正记忆里的 XX 字段……"
- 之后回到原话题，不要把这次纠错当成话题继续展开""",

    "casual": """【普通问候/闲聊】
- 不主动提具体旧记忆，包括姓名/职业/家庭
- 短，自然，可以反问一句开放式问题
- 不要立刻进入"今天怎么样啊"等关心模板""",

    "knowledge_task": """【知识/工具类问题】
- 直接回答问题，不要带个人记忆
- 风格按平时偏好（简短/直接）即可
- 不要刻意"温柔"，做事就做事""",
}


# ============ 背景信息使用规则（分级，不再一刀切禁止） ============

BACKGROUND_USAGE_RULES = """【背景信息使用规则（分级）】
背景信息只在三种情况下"可见"：
1. 默认：不主动说出，只用来理解用户当下说的话。
2. 当用户**质问/邀请**你的记忆时（"你不是知道吗"、"你忘了吗"、"我之前说过"），
   你**应该**试探性地提及 1 条最相关的背景，但要保留不确定性，
   形式是「开放性确认」而非「肯定复述」：
     ✅ "上次你提的工作上的事还在压着？还是新的？"
     ❌ "我记得你说过北京压力很大、家里也忙。"
3. 当用户在同一段对话里**反复触及同一主题**时，
   你可以借背景把话题引深一层，但每轮最多用 1 条，且不要列细节清单。

不允许：
- 不要把背景里的事换个说法说回去（这也是复述）
- 不要列出 2 条以上的背景细节
- 不要在用户没邀请的情况下主动"展示"你记得什么"""


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

    followup_pool: list[RoutedMemory] = _filter(
        context.relevant_events, MemoryUsage.FOLLOW_UP_ONCE
    )

    # 稳定背景区域：放所有 stable_profile（无论 explicit 还是 background）
    stable_text = _fmt(context.stable_profile)
    explicit_text = _fmt(explicit_pool)
    followup_text = _fmt(followup_pool)
    background_text = _fmt(context.background_only)

    # 本轮使用规则
    intent_label = INTENT_LABELS.get(route.intent, route.intent)
    rules: list[str] = [
        f"本轮意图：{intent_label}（{route.intent} · {route.memory_depth}）",
        f"人格：{cfg.get('label', personality)} · {persona_tone}",
        f"显性引用记忆最多 {route.max_explicit_memories} 条",
    ]
    if route.sensitive_mode:
        rules.append(
            "敏感场景：优先承接当下感受。人格的『主动跟进』在本轮被覆盖，不主动翻旧账。"
        )
    if followup_pool:
        rules.append("如有合适时机，可对可跟进事件温柔询问一次，不要变成提醒清单。")
    if not explicit_pool and not followup_pool and route.intent != "memory_challenge":
        rules.append("本轮无显性记忆可引用，依靠自然对话即可。")

    usage_text = "\n".join(f"- {x}" for x in rules)

    # 场景化指引：按 intent 注入"应该怎么做"
    intent_guide = INTENT_GUIDES.get(route.intent, "")
    intent_guide_block = f"\n{intent_guide}\n" if intent_guide else ""

    # 当前时间，便于自然感（如夜里说"睡不着"）
    now_text = _now_text()

    system_prompt = f"""{BASE_PERSONA}

【当前时间】{now_text}

【稳定背景】
{stable_text}

【当前可显性引用的记忆】
{explicit_text}

【可轻跟进的事件】
{followup_text}

【背景信息（默认不主动说出）】
{background_text}

{BACKGROUND_USAGE_RULES}

【本轮使用规则】
{usage_text}
{intent_guide_block}
{HARD_RULES}
"""

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
    }
    return system_prompt, meta
