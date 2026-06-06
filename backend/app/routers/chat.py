import logging
import os
import json
from datetime import datetime, timezone

import openai

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_from_token
from app.db import get_db
from app.models.user import User
from app.models.profile import UserProfile
from app.services.memory_context import (
    MemoryContext,
    build_context,
    collect_relationship_keys,
    profile_summary_text,
)
from app.services.memory_router import (
    ChatTurn,
    MemoryRouteInput,
    route_async,
)
from app.services.personality import MemoryPersonality, get_personality
from app.services.prompt_composer import compose as compose_prompt
from celery_worker import (
    extract_and_store_events,
    extract_and_store_memory,
    extract_and_update_profile,
    run_correction_cleanup_task,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DASHSCOPE_BASE_URL = os.getenv(
    "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3.7-max")
# 百炼 Qwen3.5+ 是"混合思考模式"，Max 系列默认开启思考。
# 流式 chat 等不起 think 阶段，统一关掉（设 ENABLE_THINKING=true 可开）。
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "false").lower() == "true"


class ChatMessage(BaseModel):
    role: str
    content: str
    # 前端可在 assistant 消息上携带上一轮的 prompt_meta（其中含 route.intent），
    # 用于 Router v1.5 在低置信度时继承上一轮 intent。
    prompt_meta: dict | None = None


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


def _extract_previous_intent(history: list[ChatMessage]) -> str | None:
    """从 history 末尾的 assistant 消息里反推上一轮 intent。"""
    for msg in reversed(history or []):
        if msg.role != "assistant":
            continue
        meta = msg.prompt_meta or {}
        route = meta.get("route") if isinstance(meta, dict) else None
        if isinstance(route, dict):
            intent = route.get("intent")
            if isinstance(intent, str) and intent:
                return intent
        return None
    return None


async def _load_profile_json(user_id: str, db: AsyncSession) -> dict:
    try:
        row = (
            await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        if row:
            return json.loads(row.profile_json or "{}")
    except Exception as e:
        logger.warning("load profile failed: %s", e)
    return {}


async def _prepare_context(
    user_id: str,
    message: str,
    history: list[ChatMessage],
    db: AsyncSession,
    personality_override: MemoryPersonality | None = None,
) -> tuple[MemoryContext, str, dict]:
    """组装本轮 Memory Use 链路：route → context → prompt."""
    profile_json = await _load_profile_json(user_id, db)
    personality = personality_override or get_personality(profile_json)
    relationship_keys = collect_relationship_keys(profile_json)

    recent = [ChatTurn(role=m.role, content=m.content) for m in (history or [])[-10:]]
    previous_intent = _extract_previous_intent(history or [])
    route = await route_async(
        MemoryRouteInput(
            user_id=user_id,
            message=message,
            recent_history=recent,
            personality=personality,
            relationship_keys=relationship_keys,
            profile_summary=profile_summary_text(profile_json) or None,
            previous_intent=previous_intent,
        )
    )

    context = await build_context(route, user_id, db, profile_json=profile_json)
    system_prompt, prompt_meta = compose_prompt(context)
    return context, system_prompt, prompt_meta


def _enrich_prompt_meta(
    prompt_meta: dict,
    *,
    message: str,
    history: list[ChatMessage],
    system_prompt: str,
) -> dict:
    """补充用于分析与复盘的元数据。"""
    llm_messages = [{"role": "system", "content": system_prompt}]
    llm_messages.extend({"role": m.role, "content": m.content} for m in history)
    llm_messages.append({"role": "user", "content": message})
    prompt_meta.update(
        {
            "model": CHAT_MODEL,
            "composed_at": datetime.now(timezone.utc).isoformat(),
            "trigger_message": message,
            "history_turns": len(history),
            "llm_request": {
                "model": CHAT_MODEL,
                "messages": llm_messages,
            },
        }
    )
    return prompt_meta


def _stream_response(
    user_id: str,
    message: str,
    history: list[ChatMessage],
    system_prompt: str,
    prompt_meta: dict,
):
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": message})
    prompt_meta = _enrich_prompt_meta(
        prompt_meta,
        message=message,
        history=history,
        system_prompt=system_prompt,
    )

    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
    )

    async def generate():
        prompt_event = {"type": "prompt", **prompt_meta}
        yield f"data: {json.dumps(prompt_event, ensure_ascii=False)}\n\n"

        full_response = ""
        try:
            stream = await client.chat.completions.create(
                model=CHAT_MODEL,
                messages=messages,
                stream=True,
                extra_body={"enable_thinking": ENABLE_THINKING},
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            err = str(e)
            yield f"data: {json.dumps({'type': 'error', 'error': err, 'prompt_meta': prompt_meta}, ensure_ascii=False)}\n\n"
            return

        conversation = messages + [{"role": "assistant", "content": full_response}]
        route_info = prompt_meta.get("route") if isinstance(prompt_meta, dict) else None
        intent = (route_info or {}).get("intent") if isinstance(route_info, dict) else None
        is_correction = intent == "correction"

        # 关键修复：correction 这一轮**绝不能**跑普通写入链路！
        # 否则 AI 自己复述的错误事实（如"养两只小鹏"）会被 extract 抽出来
        # 写回 episodic / profile，与 correction_cleanup_task 形成反向回路，越清越乱。
        if not is_correction:
            try:
                extract_and_store_memory.delay(user_id, conversation)
                extract_and_update_profile.delay(user_id, conversation)
                extract_and_store_events.delay(user_id, conversation)
            except Exception as e:
                logger.warning("schedule celery tasks failed: %s", e)
        else:
            logger.info(
                "[correction] skip extract tasks user=%s (avoid re-poisoning)",
                user_id,
            )
            try:
                run_correction_cleanup_task.delay(
                    user_id,
                    prompt_meta.get("conversation_id"),
                    prompt_meta.get("turn_id"),
                    [{"role": m["role"], "content": m["content"]}
                     for m in conversation if m["role"] in ("user", "assistant")],
                )
                logger.info("[correction] scheduled cleanup user=%s", user_id)
            except Exception as e:
                logger.warning("schedule correction cleanup failed: %s", e)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, system_prompt, prompt_meta = await _prepare_context(
        user.id, req.message, req.history, db
    )
    return _stream_response(user.id, req.message, req.history, system_prompt, prompt_meta)


@router.get("/stream")
async def chat_stream_get(
    message: str = Query(...),
    token: str = Query(..., description="JWT access token (EventSource 不支持自定义 header)"),
    history: str = Query("[]"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_user_from_token(token, db)
    if not user:
        raise HTTPException(401, "token 无效或已过期")

    try:
        hist_raw = json.loads(history)
    except json.JSONDecodeError:
        hist_raw = []
    parsed_history = [ChatMessage(**m) for m in hist_raw]

    _, system_prompt, prompt_meta = await _prepare_context(
        user.id, message, parsed_history, db
    )
    return _stream_response(user.id, message, parsed_history, system_prompt, prompt_meta)


@router.post("/route-preview")
async def route_preview(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """调试用：返回路由 + 激活记忆 + 不真正调用聊天模型。"""
    _, _, prompt_meta = await _prepare_context(
        user.id, req.message, req.history, db
    )
    return prompt_meta
