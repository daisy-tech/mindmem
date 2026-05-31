import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models.profile import UserProfile, MemoryAuditLog
from app.models.user import User
from app.services.personality import (
    DEFAULT_PERSONALITY,
    MemoryPersonality,
    PERSONALITY_CONFIG,
    get_personality,
    list_personalities,
    set_personality,
)
from app.services.profile_engine import _set_nested, _del_nested, _now_iso, _label

router = APIRouter()

ACTION_LABELS = {
    "added":              "新增",
    "appended":           "追加",
    "replaced":           "替换",
    "replaced_lower_conf": "替换（置信度较低）",
    "merged":             "合并",
    "confirmed":          "确认",
    "manual":             "手动编辑",
}


async def _get_or_create_profile(user_id: str, db: AsyncSession) -> UserProfile:
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    row = result.scalar_one_or_none()
    if row is None:
        row = UserProfile(user_id=user_id, profile_json="{}", last_updated=datetime.now(timezone.utc))
        db.add(row)
        await db.commit()
    return row


@router.get("")
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_create_profile(user.id, db)
    return json.loads(row.profile_json)


@router.delete("/field")
async def delete_field(
    path: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_create_profile(user.id, db)
    profile = json.loads(row.profile_json)
    deleted = _del_nested(profile, path)
    if deleted:
        row.profile_json = json.dumps(profile, ensure_ascii=False)
        row.last_updated = datetime.now(timezone.utc)
        await db.commit()
    return {"deleted": deleted, "path": path}


class ForceSetRequest(BaseModel):
    path: str
    value: object


@router.post("/field")
async def force_set_field(
    body: ForceSetRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_create_profile(user.id, db)
    profile = json.loads(row.profile_json)

    old_field = None
    try:
        from app.services.profile_engine import _get_nested
        old_field = _get_nested(profile, body.path)
    except Exception:
        pass

    _set_nested(profile, body.path, {
        "value": body.value,
        "confidence": 1.0,
        "updated_at": _now_iso(),
    })
    row.profile_json = json.dumps(profile, ensure_ascii=False)
    row.last_updated = datetime.now(timezone.utc)

    log = MemoryAuditLog(
        user_id=user.id,
        dimension_path=body.path,
        old_value=json.dumps(old_field.get("value") if old_field else "", ensure_ascii=False),
        new_value=json.dumps(body.value, ensure_ascii=False),
        action="manual",
        session_id="manual",
    )
    db.add(log)
    await db.commit()
    return {"ok": True, "path": body.path, "value": body.value}


@router.get("/conflict-log")
async def get_conflict_log(
    limit: int = Query(50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回最近的自动冲突处理记录（不含 confirmed/added）"""
    result = await db.execute(
        select(MemoryAuditLog)
        .where(
            MemoryAuditLog.user_id == user.id,
            MemoryAuditLog.action.notin_(["confirmed", "added"]),
            MemoryAuditLog.action != "",
        )
        .order_by(desc(MemoryAuditLog.created_at))
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "field": r.dimension_path,
            "field_label": _label(r.dimension_path),
            "action": r.action,
            "action_label": ACTION_LABELS.get(r.action, r.action),
            "old_value": _safe_json(r.old_value),
            "new_value": _safe_json(r.new_value),
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


def _safe_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        return s


# ---------- 记忆人格 ----------


class PersonalityRequest(BaseModel):
    personality: str


@router.get("/personality")
async def get_personality_api(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_create_profile(user.id, db)
    profile = json.loads(row.profile_json or "{}")
    current = get_personality(profile).value
    return {
        "personality": current,
        "default": DEFAULT_PERSONALITY.value,
        "options": list_personalities(),
        "config": PERSONALITY_CONFIG.get(current, {}),
    }


@router.post("/personality")
async def set_personality_api(
    body: PersonalityRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        personality = MemoryPersonality(body.personality)
    except ValueError:
        valid = ", ".join(p.value for p in MemoryPersonality)
        raise HTTPException(400, f"无效的人格类型，仅支持：{valid}")

    row = await _get_or_create_profile(user.id, db)
    profile = json.loads(row.profile_json or "{}")
    set_personality(profile, personality)
    row.profile_json = json.dumps(profile, ensure_ascii=False)
    row.last_updated = datetime.now(timezone.utc)
    await db.commit()
    return {
        "ok": True,
        "personality": personality.value,
        "config": PERSONALITY_CONFIG.get(personality.value, {}),
    }
