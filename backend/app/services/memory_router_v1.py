"""Memory Router v1：关键词打分（v1.5 降级 fallback）。"""
from __future__ import annotations

from collections import defaultdict

from app.services.memory_router import (
    INTENT_PRIORITIES,
    KEYWORDS,
    ChatTurn,
    MemoryRouteInput,
    _build_route_from_intent,
    _contains_any,
    _infer_subjects,
)


def score_intents_v1(
    msg: str,
    history: list[ChatTurn],
    relationship_keys: list[str],
) -> tuple[str, list[str]]:
    """v1 规则打分，返回 (intent, reasons)。"""
    history_text = "\n".join(t.content for t in history[-6:])
    subjects = _infer_subjects(msg, history, relationship_keys)

    scores: dict[str, int] = defaultdict(int)
    reasons: list[str] = []

    if _contains_any(msg, KEYWORDS["correction"]):
        scores["correction"] += 10
        reasons.append("v1: 命中纠错关键词 → correction")

    if _contains_any(msg, KEYWORDS["memory_challenge"]):
        scores["memory_challenge"] += 8
        reasons.append("v1: 命中质问记忆关键词 → memory_challenge")

    if _contains_any(msg, KEYWORDS["self_summary"]):
        scores["self_summary"] += 5
        reasons.append("v1: 命中自我总结关键词 → self_summary")

    if _contains_any(msg, KEYWORDS["knowledge_task"]):
        scores["knowledge_task"] += 4
        reasons.append("v1: 命中工具/知识类关键词 → knowledge_task")

    if _contains_any(msg, KEYWORDS["emotional"]):
        scores["emotional_support"] += 4
        reasons.append("v1: 命中情绪关键词 → emotional_support")

    if len(msg) <= 12 and any(
        k in msg for k in ["睡不着", "失眠", "心烦", "头疼", "好累", "想哭", "撑不住"]
    ):
        scores["emotional_support"] += 6
        reasons.append("v1: 短句情绪/身体不适 → emotional_support")

    if _contains_any(msg, KEYWORDS["plan"]):
        scores["plan_followup"] += 3
        reasons.append("v1: 命中计划关键词 → plan_followup")

    if _contains_any(msg, KEYWORDS["preference"]):
        scores["preference_request"] += 3
        reasons.append("v1: 命中偏好关键词 → preference_request")

    if _contains_any(msg, KEYWORDS["greeting"]) and len(msg) <= 12:
        scores["casual"] += 3
        reasons.append("v1: 短问候 → casual")

    if subjects:
        scores["relationship_topic"] += 4
        reasons.append(f"v1: 命中人物 {subjects} → relationship_topic")

    if _contains_any(msg, KEYWORDS["relationship_pronouns"]):
        scores["relationship_topic"] += 3
        reasons.append("v1: 命中关系代词 → relationship_topic")

    if len(msg) <= 16 and any(p in msg for p in ["她", "他", "这事", "那个", "呢"]):
        if any(k in history_text for k in ["老婆", "妻子", "儿子", "孩子", "女儿"]):
            scores["relationship_topic"] += 5
            reasons.append("v1: 短句+代词+家庭上下文 → relationship_topic")

    if not scores:
        scores["casual"] += 1
        reasons.append("v1: 无信号 → casual")

    max_score = max(scores.values())
    candidates = [k for k, v in scores.items() if v == max_score]
    intent = sorted(
        candidates,
        key=lambda k: INTENT_PRIORITIES.index(k) if k in INTENT_PRIORITIES else 99,
    )[0]
    if scores.get("correction", 0) > 0:
        intent = "correction"

    return intent, reasons


def route_v1(inp: MemoryRouteInput):
    from app.services.memory_router import MemoryRoute  # noqa: F401 — re-export path

    msg = (inp.message or "").strip()
    intent, reasons = score_intents_v1(
        msg, inp.recent_history or [], inp.relationship_keys or []
    )
    return _build_route_from_intent(
        intent,
        inp,
        reasons=reasons,
        intent_source="fallback_v1",
        intent_confidence=None,
        low_confidence=False,
    )
