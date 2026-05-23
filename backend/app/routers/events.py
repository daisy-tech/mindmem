import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models.event import UserEvent
from app.models.user import User
from app.services.event_engine import TYPE_LABELS, TYPE_COLORS

router = APIRouter()


def _row_to_dict(e: UserEvent) -> dict:
    return {
        "event_id": e.event_id,
        "event_type": e.event_type,
        "type_label": TYPE_LABELS.get(e.event_type, e.event_type),
        "type_color": TYPE_COLORS.get(e.event_type, "#909399"),
        "summary": e.summary,
        "details": json.loads(e.details_json or "{}"),
        "related_entities": json.loads(e.related_json or "[]"),
        "occurred_at": e.occurred_at,
        "detected_at": e.detected_at,
        "last_referenced_at": e.last_referenced_at,
        "importance": e.importance,
        "status": e.status,
        "mention_count": e.mention_count,
        "created_at": e.created_at,
    }


@router.get("")
async def list_events(
    status: str = Query("active"),
    event_type: str | None = Query(None),
    limit: int = Query(100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(UserEvent)
        .where(UserEvent.user_id == user.id, UserEvent.status == status)
        .order_by(UserEvent.occurred_at.desc().nullslast(), UserEvent.detected_at.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(UserEvent.event_type == event_type)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_row_to_dict(r) for r in rows]


@router.delete("/{event_id}")
async def delete_event(
    event_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(UserEvent).where(
            UserEvent.event_id == event_id,
            UserEvent.user_id == user.id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.patch("/{event_id}/archive")
async def archive_event(
    event_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserEvent).where(
            UserEvent.event_id == event_id,
            UserEvent.user_id == user.id,
        )
    )
    event = result.scalar_one_or_none()
    if event:
        event.status = "archived"
        event.updated_at = datetime.now(timezone.utc).isoformat()
        await db.commit()
    return {"ok": True}
