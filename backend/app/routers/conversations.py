import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models.conversation import Conversation
from app.models.user import User

router = APIRouter()


@router.get("")
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation.id, Conversation.title, Conversation.updated_at)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(50)
    )
    rows = result.all()
    return [{"id": r.id, "title": r.title, "updated_at": r.updated_at.isoformat()} for r in rows]


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        return {"messages": []}
    return {"id": conv.id, "title": conv.title, "messages": json.loads(conv.messages_json)}


class SaveRequest(BaseModel):
    conversation_id: str
    messages: list[dict]


@router.post("")
async def save_conversation(
    body: SaveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 标题取第一条用户消息，最多 30 字
    title = "新对话"
    for m in body.messages:
        if m.get("role") == "user" and m.get("content"):
            title = m["content"][:30]
            break

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == body.conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()

    # 过滤掉 system 消息，只存 user/assistant
    storable = [m for m in body.messages if m.get("role") in ("user", "assistant")]

    if conv is None:
        conv = Conversation(
            id=body.conversation_id,
            user_id=user.id,
            title=title,
            messages_json=json.dumps(storable, ensure_ascii=False),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(conv)
    else:
        conv.title = title
        conv.messages_json = json.dumps(storable, ensure_ascii=False)
        conv.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"ok": True, "id": conv.id, "title": conv.title}


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    await db.commit()
    return {"ok": True}
