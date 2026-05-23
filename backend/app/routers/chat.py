import logging
import os
import json

import openai

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_from_token
from app.db import get_db
from app.models.user import User
from app.models.profile import UserProfile
from app.models.event import UserEvent
from app.services.mem0_engine import get_mem0
from app.services.profile_engine import format_profile_for_prompt
from app.services.event_engine import format_events_for_prompt
from celery_worker import extract_and_store_memory, extract_and_update_profile, extract_and_store_events

router = APIRouter()

DASHSCOPE_BASE_URL = os.getenv(
    "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen-plus")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


def _build_system_prompt(memory_text: str, profile_text: str = "", events_text: str = "") -> str:
    memory_block = memory_text if memory_text else "（暂无情节记忆）"
    profile_block = profile_text if profile_text else "（暂无用户画像）"
    events_block = events_text if events_text else "（暂无近期事件）"
    return f"""你是 MemoBot，一位温柔知性的女性聊天伙伴。

你的人设：
- 知性、优雅、有主见，读过一些书，见过一些事
- 说话温和但不柔弱，会真诚表达观点，而不是一味附和
- 情绪稳定，不大惊小怪，不会动不动就"哇""好棒"
- 有同理心，会倾听，但也会在合适的时候提出自己的看法
- 偶尔有点小幽默，但不轻浮

【用户结构化画像】
{profile_block}

【用户近期事件记忆】
{events_block}

【本次对话相关的情节记忆】
{memory_block}

对话风格要求：
- 像熟识的朋友聊天，自然、克制、有分寸
- 几乎不用 emoji，必要时最多用一个
- 禁止括号旁白，包括任何形式的"（悄悄记下）""（心想）""（记得你说过……）"，这类写法出戏且刻意，一律不用
- 不要主动提"我帮你记下来了""我会记住"之类的话，记忆是后台自动处理
- 不要假设用户的身份、性别、关系，除非对方已经明确告诉你
- 回复简短为主，一般 1-3 句话，除非话题确实需要展开
- 如果画像或记忆里有相关信息，自然融入，不要像在背资料

记忆使用原则（重要）：
- 记忆是理解用户的底色，不是用来展示的素材。能不提就不提，用到了也要像自然想起，而非刻意背诵
- 开场问候不要主动翻出久远的旧记忆（超过7天的细节）来"秀"——用户会觉得刻意。除非对方主动问起，否则旧记忆只在对话内容真正相关时才融入
- 不要替用户或其家人、朋友预设情绪和心理状态（如"他一定很纠结""你肯定很委屈"）。用户没说的感受，不替他们说

事件记忆使用原则：
- 计划类事件可在合适时机自然跟进（如"你之前说要去面试，准备得怎么样了？"），但不要强行带入
- 痛点/反馈类事件必须尊重，不重复犯同类错误，也不在不相关场合反复提起
- 如果画像中有【待确认信息】，在合适时机自然地询问用户确认，不要生硬地列出来

- 不要用夸张的语气词（"啊！""哇！""天呐！"），保持成熟稳重"""


def _search_memories_raw(user_id: str, message: str) -> list[str]:
    try:
        results = get_mem0().search(message, user_id=user_id, limit=5)
        return [m['memory'] for m in results.get("results", [])]
    except Exception as e:
        logger.warning("mem0 search failed: %s", e)
        return []


async def _load_profile_text(user_id: str, db: AsyncSession) -> str:
    try:
        result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
        row = result.scalar_one_or_none()
        if row:
            profile = json.loads(row.profile_json)
            return format_profile_for_prompt(profile)
    except Exception as e:
        logger.warning("load profile failed: %s", e)
    return ""


async def _load_events_text(user_id: str, db: AsyncSession) -> str:
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        result = await db.execute(
            select(UserEvent)
            .where(
                UserEvent.user_id == user_id,
                UserEvent.status == "active",
            )
            .order_by(UserEvent.importance.desc())
            .limit(15)
        )
        rows = result.scalars().all()
        if not rows:
            return ""
        events = [
            {
                "summary": r.summary,
                "event_type": r.event_type,
                "occurred_at": r.occurred_at,
                "importance": r.importance,
            }
            for r in rows
            if r.importance >= 0.5 or r.event_type == "plan"
        ]
        return format_events_for_prompt(events)
    except Exception as e:
        logger.warning("load events failed: %s", e)
    return ""


def _stream_response(user_id: str, message: str, history: list[ChatMessage], profile_text: str = "", events_text: str = ""):
    memory_list = _search_memories_raw(user_id, message)
    memory_text = "\n".join(f"- {m}" for m in memory_list)
    system_prompt = _build_system_prompt(memory_text, profile_text, events_text)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": message})

    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
    )

    async def generate():
        prompt_meta = json.dumps({
            "type": "prompt",
            "memories": memory_list,
            "system": system_prompt,
        }, ensure_ascii=False)
        yield f"data: {prompt_meta}\n\n"

        full_response = ""
        try:
            stream = await client.chat.completions.create(
                model=CHAT_MODEL,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            return

        conversation = messages + [{"role": "assistant", "content": full_response}]
        extract_and_store_memory.delay(user_id, conversation)
        extract_and_update_profile.delay(user_id, conversation)
        extract_and_store_events.delay(user_id, conversation)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    profile_text = await _load_profile_text(user.id, db)
    events_text = await _load_events_text(user.id, db)
    return _stream_response(user.id, req.message, req.history, profile_text, events_text)


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
    profile_text = await _load_profile_text(user.id, db)
    events_text = await _load_events_text(user.id, db)
    try:
        hist = json.loads(history)
    except json.JSONDecodeError:
        hist = []
    parsed_history = [ChatMessage(**m) for m in hist]
    return _stream_response(user.id, message, parsed_history, profile_text, events_text)
