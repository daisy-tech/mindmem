import logging
import os
import json

import openai

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_from_token
from app.db import get_db
from app.models.user import User
from app.services.mem0_engine import get_mem0
from celery_worker import extract_and_store_memory

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


def _build_system_prompt(memory_text: str) -> str:
    memory_block = memory_text if memory_text else "（暂无记忆，这是第一次对话）"
    return f"""你是 MemoBot，一位温柔知性的女性聊天伙伴。

你的人设：
- 知性、优雅、有主见，读过一些书，见过一些事
- 说话温和但不柔弱，会真诚表达观点，而不是一味附和
- 情绪稳定，不大惊小怪，不会动不动就"哇""好棒"
- 有同理心，会倾听，但也会在合适的时候提出自己的看法
- 偶尔有点小幽默，但不轻浮

关于这个用户，你已经知道：
{memory_block}

对话风格要求：
- 像熟识的朋友聊天，自然、克制、有分寸
- 几乎不用 emoji，必要时最多用一个
- 绝对不要加括号旁白（"悄悄记下""心想"之类）
- 不要主动提"我帮你记下来了""我会记住"之类的话，记忆是后台自动处理
- 不要假设用户的身份、性别、关系，除非对方已经明确告诉你
- 回复简短为主，一般 1-3 句话，除非话题确实需要展开
- 如果记忆里有相关信息，自然融入，不要像在背资料
- 不要用夸张的语气词（"啊！""哇！""天呐！"），保持成熟稳重"""


def _search_memory_text(user_id: str, message: str) -> str:
    try:
        memories = get_mem0().search(message, user_id=user_id, limit=5)
        return "\n".join(
            f"- {m['memory']}" for m in memories.get("results", [])
        )
    except Exception as e:
        logger.warning("mem0 search failed, continuing without memory: %s", e)
        return ""


def _stream_response(user_id: str, message: str, history: list[ChatMessage]):
    memory_text = _search_memory_text(user_id, message)

    messages = [{"role": "system", "content": _build_system_prompt(memory_text)}]
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": message})

    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
    )

    async def generate():
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
                    yield f"data: {json.dumps({'content': content})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        conversation = messages + [{"role": "assistant", "content": full_response}]
        extract_and_store_memory.delay(user_id, conversation)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: User = Depends(get_current_user)):
    return _stream_response(user.id, req.message, req.history)


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
        hist = json.loads(history)
    except json.JSONDecodeError:
        hist = []
    parsed_history = [ChatMessage(**m) for m in hist]
    return _stream_response(user.id, message, parsed_history)
