"""Layer 2：小模型 intent 分类（Memory Router v1.5）。"""
from __future__ import annotations

import json
import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

from app.services.memory_router import ChatTurn

logger = logging.getLogger(__name__)

DASHSCOPE_BASE_URL = os.getenv(
    "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
INTENT_MODEL = os.getenv("INTENT_MODEL", os.getenv("CHAT_MODEL", "qwen-plus"))
INTENT_MODEL_TEMPERATURE = float(os.getenv("INTENT_MODEL_TEMPERATURE", "0"))
INTENT_CLASSIFIER_ENABLED = (
    os.getenv("INTENT_CLASSIFIER_ENABLED", "true").lower() == "true"
)
INTENT_CLASSIFIER_TIMEOUT_SEC = float(os.getenv("INTENT_CLASSIFIER_TIMEOUT_SEC", "8"))

VALID_INTENTS = frozenset(
    {
        "correction",
        "memory_challenge",
        "self_summary",
        "knowledge_task",
        "emotional_support",
        "relationship_topic",
        "plan_followup",
        "preference_request",
        "casual",
    }
)

CLASSIFIER_SYSTEM = """你是 MindMem 的记忆路由分类器，不是聊天助手。
任务：根据【当前用户消息】和【最近对话】，判断本轮应使用的「记忆策略 intent」。

intent 含义（只选一个主 intent）：
- correction：用户在纠正、删除、要求不要再提某记忆
- memory_challenge：用户在质问你是否记得/应该知道
- self_summary：用户在问你对 ta 的整体了解、重要的人、聊过什么
- knowledge_task：用户在问通用知识、工具、代码、天气等，与个人经历无关
- emotional_support：用户在表达压力、焦虑、难受、失眠等情绪，主要需要承接
- relationship_topic：话题围绕某具体的人/关系/家庭事务
- plan_followup：话题围绕计划、待办、面试、HR、时间节点、跟进进度
- preference_request：用户在要推荐、建议、怎么选、怎么安排（依赖个人偏好）
- casual：普通闲聊、问候、无明确记忆策略需求

规则：
1. 只输出 JSON：{"intent":"...", "confidence":0.0-1.0, "rationale":"..."}
2. 不要输出聊天回复
3. 结合上下文理解短句和代词（「她」「那家公司」）
4. 若多种 intent 都可能，选「对记忆加载策略影响最大」的一个
5. confidence 表示你对主 intent 的确信度（0-1 浮点数）

禁止：编造用户未提供的事实。"""


class IntentClassifierInput(BaseModel):
    message: str
    recent_history: list[ChatTurn] = Field(default_factory=list)
    relationship_keys: list[str] = Field(default_factory=list)
    profile_one_liner: str | None = None
    previous_intent: str | None = None


class IntentClassifierOutput(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


def _format_history(history: list[ChatTurn]) -> str:
    lines: list[str] = []
    for t in history[-6:]:
        role = "user" if t.role == "user" else "assistant"
        content = (t.content or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "（无）"


def _build_user_prompt(inp: IntentClassifierInput) -> str:
    parts = [
        "【最近对话】",
        _format_history(inp.recent_history),
        "",
        "【当前用户消息】",
        inp.message.strip(),
    ]
    if inp.relationship_keys:
        parts.extend(["", "【已知关系人】", ", ".join(inp.relationship_keys[:20])])
    if inp.profile_one_liner:
        parts.extend(["", "【用户摘要】", inp.profile_one_liner])
    if inp.previous_intent:
        parts.extend(["", "【上一轮 intent】", inp.previous_intent])
    return "\n".join(parts)


def _parse_classifier_json(raw: str) -> IntentClassifierOutput:
    data = json.loads(raw)
    intent = str(data.get("intent") or "casual").strip()
    if intent not in VALID_INTENTS:
        intent = "casual"
    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(data.get("rationale") or "")[:120]
    return IntentClassifierOutput(intent=intent, confidence=confidence, rationale=rationale)


async def classify_intent(inp: IntentClassifierInput) -> IntentClassifierOutput:
    """调用小模型做 intent 分类。"""
    import openai

    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
        timeout=INTENT_CLASSIFIER_TIMEOUT_SEC,
    )
    resp = await client.chat.completions.create(
        model=INTENT_MODEL,
        temperature=INTENT_MODEL_TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": _build_user_prompt(inp)},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    return _parse_classifier_json(raw)


def resolve_intent_with_fallback(
    out: IntentClassifierOutput,
    message: str,
    previous_intent: str | None,
) -> tuple[str, float, bool, Literal["classifier", "inherited", "low_confidence_casual"]]:
    """根据 confidence 与上下文继承决定最终 intent。"""
    msg = (message or "").strip()
    conf = out.confidence
    intent = out.intent

    if conf >= 0.50:
        return intent, conf, conf < 0.75, "classifier"

    if previous_intent and previous_intent in VALID_INTENTS and len(msg) <= 16:
        return previous_intent, conf, True, "inherited"

    return "casual", conf, True, "low_confidence_casual"
