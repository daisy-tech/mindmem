"""MemoBot 记忆人格。

人格控制记忆表达边界、事件主动跟进程度和追问强度，
但不改变事实正确性和敏感信息硬边界。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class MemoryPersonality(str, Enum):
    INTROVERT = "introvert"
    BALANCED = "balanced"
    EXTROVERT = "extrovert"


DEFAULT_PERSONALITY = MemoryPersonality.BALANCED

PERSONALITY_CONFIG: dict[str, dict[str, Any]] = {
    "introvert": {
        "label": "内向型",
        "description": "安静、克制、强边界感，很少主动提旧记忆。",
        "max_explicit_memories": 1,
        "allow_casual_memory": False,
        "plan_followup": "asked_only",
        "pain_point_policy": "background_only",
        "question_style": "low",
    },
    "balanced": {
        "label": "中性型",
        "description": "熟悉但有分寸，相关时自然使用记忆。",
        "max_explicit_memories": 2,
        "allow_casual_memory": False,
        "plan_followup": "once",
        "pain_point_policy": "triggered_only",
        "question_style": "medium",
    },
    "extrovert": {
        "label": "外向型",
        "description": "更主动关心和跟进近况，但仍遵守敏感边界。",
        "max_explicit_memories": 3,
        "allow_casual_memory": True,
        "plan_followup": "active_once",
        "pain_point_policy": "soft_triggered",
        "question_style": "high",
    },
}


def get_config(personality: MemoryPersonality) -> dict[str, Any]:
    return PERSONALITY_CONFIG[personality.value]


# ---------- 在 profile JSON 中的持久化 ----------
# 放在顶层 `_settings`，不污染对外展示的画像字段。

_SETTINGS_KEY = "_settings"
_PERSONALITY_KEY = "memory_personality"


def get_personality(profile: dict | None) -> MemoryPersonality:
    if not isinstance(profile, dict):
        return DEFAULT_PERSONALITY
    try:
        value = profile.get(_SETTINGS_KEY, {}).get(_PERSONALITY_KEY)
        if isinstance(value, str) and value in {p.value for p in MemoryPersonality}:
            return MemoryPersonality(value)
    except Exception:
        pass
    return DEFAULT_PERSONALITY


def set_personality(profile: dict, personality: MemoryPersonality) -> None:
    if not isinstance(profile, dict):
        raise ValueError("profile must be dict")
    profile.setdefault(_SETTINGS_KEY, {})[_PERSONALITY_KEY] = personality.value


def list_personalities() -> list[dict[str, Any]]:
    """供前端展示三个人格选项。"""
    return [
        {"value": k, **v}
        for k, v in PERSONALITY_CONFIG.items()
    ]
