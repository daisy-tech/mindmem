"""Memory Router v1.5：硬规则 + 小模型分类 + 策略查表。

Layer 1：硬规则（correction / memory_challenge / 极短问候）
Layer 2：小模型 intent 分类（可降级 v1 关键词）
Layer 3：intent → load_layers / memory_depth / event_policy
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable, Literal

from pydantic import BaseModel, Field

from app.services.personality import (
    DEFAULT_PERSONALITY,
    PERSONALITY_CONFIG,
    MemoryPersonality,
)

logger = logging.getLogger(__name__)


class MemoryUsage:
    EXPLICIT_OK = "explicit_ok"
    BACKGROUND_ONLY = "background_only"
    FOLLOW_UP_ONCE = "follow_up_once"
    AVOID_UNLESS_ASKED = "avoid_unless_asked"


INTENT_PRIORITIES = [
    "correction",
    "memory_challenge",
    "self_summary",
    "knowledge_task",
    "emotional_support",
    "relationship_topic",
    "plan_followup",
    "preference_request",
    "casual",
]

ROUTER_VERSION = "v1.5"

IntentSource = Literal[
    "hard_rule",
    "classifier",
    "inherited",
    "fallback_v1",
    "low_confidence_casual",
]


class ChatTurn(BaseModel):
    role: str
    content: str


class MemoryRouteInput(BaseModel):
    user_id: str
    message: str
    recent_history: list[ChatTurn] = Field(default_factory=list)
    personality: MemoryPersonality = DEFAULT_PERSONALITY
    relationship_keys: list[str] = Field(default_factory=list)
    profile_summary: str | None = None
    previous_intent: str | None = None


class HardRuleResult(BaseModel):
    matched: bool = False
    intent: str | None = None
    rule_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


class MemoryRoute(BaseModel):
    intent: str
    memory_depth: str
    load_layers: list[str]
    query: str
    sensitive_mode: bool = False
    max_explicit_memories: int = 2
    event_policy: str = "none"
    personality: MemoryPersonality = DEFAULT_PERSONALITY
    inferred_subjects: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    intent_confidence: float | None = None
    intent_source: IntentSource | None = None
    low_confidence: bool = False
    router_version: str = ROUTER_VERSION


# ============ Layer 3 策略表 ============

LAYER_PRESETS: dict[str, list[str]] = {
    "casual": ["profile_basic"],
    "self_summary": ["profile", "relationships", "events", "episodic"],
    "memory_challenge": ["profile", "relationships", "events", "episodic"],
    "relationship_topic": ["relationships", "episodic", "events"],
    "emotional_support": ["profile_basic", "episodic", "events"],
    "plan_followup": ["events", "episodic"],
    "preference_request": ["profile", "episodic"],
    "correction": ["profile", "relationships", "episodic"],
    "knowledge_task": ["profile_style"],
}

DEPTH_BY_INTENT: dict[str, str] = {
    "casual": "minimal",
    "self_summary": "broad",
    "memory_challenge": "focused",
    "relationship_topic": "focused",
    "emotional_support": "safe_focused",
    "plan_followup": "event_focused",
    "preference_request": "focused",
    "correction": "focused",
    "knowledge_task": "minimal",
}

EVENT_POLICY_BY_INTENT: dict[str, str] = {
    "casual": "none",
    "self_summary": "summary",
    "memory_challenge": "summary",
    "relationship_topic": "related_only",
    "emotional_support": "background_pain_points",
    "plan_followup": "track_or_follow_up",
    "preference_request": "related_only",
    "correction": "none",
    "knowledge_task": "none",
}

# v1 fallback 关键词（memory_router_v1.py）
KEYWORDS: dict[str, list[str]] = {
    "self_summary": [
        "你了解我", "你记得我", "我身边", "总结一下我",
        "你都知道我什么", "我们聊过什么", "你对我的印象",
    ],
    "memory_challenge": [
        "你不知道", "你忘了", "你不记得", "你应该知道",
        "你不是知道", "你不是有记忆", "你怎么不",
        "我之前说过", "我跟你说过", "我之前跟你提过",
        "你没注意", "你都忘了",
    ],
    "relationship_pronouns": [
        "她", "孩子", "老婆", "妻子", "儿子", "女儿",
        "邻居", "爸爸", "妈妈", "同事", "朋友", "姐姐", "哥哥",
        "弟弟", "妹妹", "亲戚",
    ],
    "emotional": [
        "烦", "累", "压力", "焦虑", "撑不住", "难受",
        "心累", "不开心", "崩溃", "委屈", "郁闷", "丧",
        "心烦", "心慌", "烦躁", "心里堵", "难过",
        "睡不着", "失眠", "睡不好", "醒了", "头疼", "头晕",
        "想哭", "不想动", "撑不下去", "活不下去", "慌",
    ],
    "plan": [
        "明天", "下周", "下个月", "计划", "准备", "打算",
        "要去", "开始", "下下周", "约了", "等会儿", "面试", "HR", "信儿",
        "惦记", "回音",
    ],
    "preference": [
        "推荐", "适合", "怎么学", "怎么安排", "建议",
        "怎么办", "怎么选", "应该怎么",
    ],
    "correction": [
        "你记错", "忘掉", "删除", "不对", "搞错了",
        "你弄错", "纠正", "更正",
        "不是这样的", "不是这样", "不是这个", "你记错了",
    ],
    "knowledge_task": [
        "怎么写", "如何实现", "帮我写", "示例", "怎么配置",
        "命令", "代码", "脚本", "linux", "python", "sql",
        "docker", "api", "报错", "怎么用", "解释",
    ],
    "greeting": [
        "在吗", "在么", "早", "晚安", "你好", "嗨",
        "hi", "hello", "陪我聊", "在不在",
    ],
}

_GREETING_ONLY = frozenset(KEYWORDS["greeting"])

_CORRECTION_HARD_PATTERNS = [
    re.compile(p)
    for p in [
        r"你记错",
        r"记错了",
        r"你弄错",
        r"搞错了",
        r"纠正",
        r"更正",
        r"别再提",
        r"不要提",
        r"忘掉",
        r"删除记忆",
        r"不是这样的",
        r"不是这样",
        r"不是这个",
        r"你记错了",
        r"不是.{1,10}是",
        r"不是.{1,6}岁",
        r"不考虑了",
        r"我不考虑",
    ]
]

_MEMORY_CHALLENGE_HARD = list(KEYWORDS["memory_challenge"])

_NON_GREETING_HINTS = [
    "面试", "HR", "老婆", "妻子", "孩子", "儿子", "女儿",
    "累", "烦", "压力", "记错", "不对", "解释", "怎么",
    "推荐", "计划", "明天", "下周", "公司", "工作",
]


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def _build_query(message: str, history: list[ChatTurn], subjects: list[str]) -> str:
    """构造 mem0 检索 query。

    只带最近 1 条 user 历史，避免长串把当前句关键名词稀释。
    历史更早的承接关系交给 `_infer_subjects` 推断的主语来表达；如果连主语
    都推不出来，那条事实大概率也不属于本轮要召回的范围。
    """
    recent_user = [t.content for t in history[-1:] if t.role == "user"]
    parts: list[str] = [message.strip(), *recent_user, *subjects]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " | ".join(out)


_SELF_PRONOUNS = {"她", "他", "TA", "ta", "Ta", "她们", "他们"}
_FAMILY_HINTS = {
    "老婆": "妻子",
    "妻子": "妻子",
    "媳妇": "妻子",
    "孩子": "儿子",
    "儿子": "儿子",
    "女儿": "女儿",
}


def _infer_subjects(
    message: str,
    history: list[ChatTurn],
    relationship_keys: list[str],
) -> list[str]:
    subjects: list[str] = []
    for key in relationship_keys:
        if key and key in message and key not in subjects:
            subjects.append(key)

    if any(p in message for p in _SELF_PRONOUNS):
        history_text = "\n".join(t.content for t in history[-6:])
        for key in relationship_keys:
            if key in history_text and key not in subjects:
                subjects.append(key)
                break
        if not subjects:
            for hint, target in _FAMILY_HINTS.items():
                if hint in history_text:
                    if target in relationship_keys and target not in subjects:
                        subjects.append(target)
                    break
    return subjects


def _correction_hard_match(msg: str) -> bool:
    return any(p.search(msg) for p in _CORRECTION_HARD_PATTERNS)


def _memory_challenge_hard_match(msg: str) -> bool:
    return _contains_any(msg, _MEMORY_CHALLENGE_HARD)


def _is_pure_greeting(msg: str) -> bool:
    if len(msg) > 12:
        return False
    if _contains_any(msg, _NON_GREETING_HINTS):
        return False
    stripped = msg.strip().replace(" ", "")
    if not stripped:
        return False
    return any(g in msg for g in _GREETING_ONLY) or stripped in {"嗯", "哈", "哈哈", "好", "ok", "OK"}


def apply_hard_rules(msg: str) -> HardRuleResult:
    """Layer 1：硬规则，命中则跳过分类器。"""
    if _correction_hard_match(msg):
        return HardRuleResult(
            matched=True,
            intent="correction",
            rule_id="R1",
            reasons=["硬规则 R1：纠错/删除/别再提 → correction"],
        )
    if _memory_challenge_hard_match(msg):
        return HardRuleResult(
            matched=True,
            intent="memory_challenge",
            rule_id="R2",
            reasons=["硬规则 R2：质问记忆 → memory_challenge"],
        )
    if _is_pure_greeting(msg):
        return HardRuleResult(
            matched=True,
            intent="casual",
            rule_id="R3",
            reasons=["硬规则 R3：极短问候/闲聊 → casual"],
        )
    return HardRuleResult(matched=False)


def _build_route_from_intent(
    intent: str,
    inp: MemoryRouteInput,
    *,
    reasons: list[str],
    intent_source: IntentSource | None,
    intent_confidence: float | None,
    low_confidence: bool,
) -> MemoryRoute:
    msg = (inp.message or "").strip()
    history = inp.recent_history or []
    personality = inp.personality or DEFAULT_PERSONALITY
    cfg = PERSONALITY_CONFIG[personality.value]
    subjects = _infer_subjects(msg, history, inp.relationship_keys or [])

    layers = list(LAYER_PRESETS.get(intent, ["profile_basic"]))
    depth = DEPTH_BY_INTENT.get(intent, "minimal")
    event_policy = EVENT_POLICY_BY_INTENT.get(intent, "none")
    sensitive_mode = intent in {"emotional_support", "correction", "memory_challenge"}

    max_explicit = int(cfg.get("max_explicit_memories", 2))
    if intent == "self_summary":
        max_explicit = max(max_explicit, 5)
    if intent == "memory_challenge":
        max_explicit = max(max_explicit, 2)
    if intent == "casual" and not cfg.get("allow_casual_memory", False):
        max_explicit = 0
    if intent == "knowledge_task":
        max_explicit = 0

    query = _build_query(msg, history, subjects)

    route_result = MemoryRoute(
        intent=intent,
        memory_depth=depth,
        load_layers=layers,
        query=query,
        sensitive_mode=sensitive_mode,
        max_explicit_memories=max_explicit,
        event_policy=event_policy,
        personality=personality,
        inferred_subjects=subjects,
        reasons=reasons,
        intent_confidence=intent_confidence,
        intent_source=intent_source,
        low_confidence=low_confidence,
    )
    logger.info(
        "[router] user=%s intent=%s source=%s conf=%s depth=%s",
        inp.user_id,
        intent,
        intent_source,
        intent_confidence,
        depth,
    )
    return route_result


async def route_async(inp: MemoryRouteInput) -> MemoryRoute:
    """v1.5 主入口（async）。"""
    import os

    msg = (inp.message or "").strip()

    hard = apply_hard_rules(msg)
    if hard.matched and hard.intent:
        return _build_route_from_intent(
            hard.intent,
            inp,
            reasons=list(hard.reasons),
            intent_source="hard_rule",
            intent_confidence=1.0,
            low_confidence=False,
        )

    classifier_enabled = os.getenv("INTENT_CLASSIFIER_ENABLED", "true").lower() == "true"
    if classifier_enabled:
        from app.services.intent_classifier import (
            IntentClassifierInput,
            classify_intent,
            resolve_intent_with_fallback,
        )

        try:
            clf_in = IntentClassifierInput(
                message=msg,
                recent_history=inp.recent_history or [],
                relationship_keys=inp.relationship_keys or [],
                profile_one_liner=inp.profile_summary,
                previous_intent=inp.previous_intent,
            )
            out = await classify_intent(clf_in)
            intent, conf, low_conf, source = resolve_intent_with_fallback(
                out, msg, inp.previous_intent
            )
            map_source: IntentSource = (
                source if source != "low_confidence_casual" else "low_confidence_casual"
            )
            reasons = [
                f"classifier: {out.rationale} (conf={conf:.2f})",
                f"resolved intent={intent} via {source}",
            ]
            return _build_route_from_intent(
                intent,
                inp,
                reasons=reasons,
                intent_source=map_source,
                intent_confidence=conf,
                low_confidence=low_conf,
            )
        except Exception as e:
            logger.warning("[router] classifier failed, fallback v1: %s", e)

    from app.services.memory_router_v1 import route_v1

    return route_v1(inp)


def route(inp: MemoryRouteInput) -> MemoryRoute:
    """同步包装：无 running loop 时跑 async；已在 async 内则降级 v1。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(route_async(inp))

    from app.services.memory_router_v1 import route_v1

    logger.debug("[router] sync route() inside event loop, using v1 fallback")
    return route_v1(inp)
