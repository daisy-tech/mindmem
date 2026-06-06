"""评测用合成 persona 灌库与解析。

批量评测使用固定 eval 用户（persona_a_zhang / 老张），需手动灌库一次；
单条调试仍使用登录用户真实记忆。

灌库链路：
- SQLite（users / user_profiles / user_events）走 AsyncSession + ORM，
  避免与正在跑的 chat 请求互锁。
- 情节记忆走 mem0（同步 client），用 asyncio.to_thread 隔离阻塞。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.event import UserEvent
from app.models.profile import UserProfile
from app.models.user import User

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"

EVAL_USER_ID = os.getenv("EVAL_USER_ID", "a0000001-0000-4000-8000-000000000001")
EVAL_USER_PHONE = os.getenv("EVAL_USER_PHONE", "13800000001")

PERSONA_FIXTURES: dict[str, Path] = {
    "persona_a_zhang": EVAL_DIR / "persona_a_zhang.json",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_persona_fixture(persona_ref: str) -> dict[str, Any]:
    path = PERSONA_FIXTURES.get(persona_ref)
    if not path or not path.exists():
        raise FileNotFoundError(f"未找到 persona 定义: {persona_ref}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ============ Qdrant / mem0（同步，需 to_thread 隔离） ============


def _clear_episodic_memories_sync(user_id: str) -> int:
    """清空 Qdrant 中该用户的情节记忆。"""
    try:
        from app.services.mem0_engine import get_mem0

        mem = get_mem0()
        data = mem.get_all(user_id)
        items = (
            data.get("results", data.get("memories", []))
            if isinstance(data, dict)
            else data
        )
        if not isinstance(items, list):
            items = []
        deleted = 0
        for it in items:
            mid = it.get("id") or it.get("memory_id")
            if mid:
                try:
                    mem.delete(mid, user_id)
                    deleted += 1
                except Exception as e:
                    logger.warning("删除情节记忆失败 id=%s: %s", mid, e)
        return deleted
    except Exception as e:
        logger.warning("清空情节记忆失败 user=%s: %s", user_id, e)
        return 0


def _seed_episodic_sync(user_id: str, texts: list[str]) -> int:
    if not texts:
        return 0
    from app.services.mem0_engine import get_mem0

    mem = get_mem0()
    added = 0
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        try:
            mem.add(
                [{"role": "user", "content": text}], user_id=user_id, infer=False
            )
            added += 1
        except Exception as e:
            logger.warning("写入情节记忆失败 text=%s: %s", text[:30], e)
    return added


# ============ SQLite（异步 ORM） ============


async def _upsert_user(
    db: AsyncSession, user_id: str, phone: str, nickname: str
) -> str:
    """返回最终 user_id（可能已有同 phone 用户复用）。"""
    row = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if row is None:
        row = (
            await db.execute(select(User).where(User.phone == phone))
        ).scalar_one_or_none()

    if row is None:
        row = User(
            id=user_id,
            phone=phone,
            nickname=nickname,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.flush()
        return row.id

    row.phone = phone
    row.nickname = nickname
    await db.flush()
    return row.id


async def _upsert_profile(db: AsyncSession, user_id: str, profile: dict) -> None:
    profile_json = json.dumps(profile, ensure_ascii=False)
    row = (
        await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        row = UserProfile(
            user_id=user_id,
            profile_json=profile_json,
            last_updated=datetime.now(timezone.utc),
        )
        db.add(row)
    else:
        row.profile_json = profile_json
        row.last_updated = datetime.now(timezone.utc)


async def _replace_events(
    db: AsyncSession, user_id: str, events: list[dict]
) -> int:
    await db.execute(delete(UserEvent).where(UserEvent.user_id == user_id))
    now = _now_iso()
    for ev in events:
        db.add(
            UserEvent(
                event_id=ev.get("event_id") or str(uuid.uuid4()),
                user_id=user_id,
                event_type=ev.get("event_type", "experience"),
                summary=ev.get("summary", ""),
                details_json="{}",
                related_json="[]",
                occurred_at=ev.get("occurred_at"),
                detected_at=now,
                importance=float(ev.get("importance", 0.7)),
                status="active",
                mention_count=1,
                created_at=now,
                updated_at=now,
            )
        )
    return len(events)


async def seed_persona(
    persona_ref: str = "persona_a_zhang",
    user_id: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """幂等灌库：SQLite 画像/事件 + Qdrant 情节记忆（先清空再写入）。

    db 可选——为 None 时本函数开自己的 session（适合后台任务/CLI）。
    """
    fixture = load_persona_fixture(persona_ref)
    phone = fixture.get("phone") or EVAL_USER_PHONE
    nickname = fixture.get("nickname") or "老张"
    profile = fixture.get("profile") or {"profile": {}}
    events = fixture.get("events") or []
    episodic = fixture.get("episodic") or []
    target_id = user_id or EVAL_USER_ID

    own_session = db is None
    session_ctx = SessionLocal() if own_session else None
    session = session_ctx if own_session else db
    try:
        if own_session:
            session = await session_ctx.__aenter__()  # type: ignore[union-attr]

        uid = await _upsert_user(session, target_id, phone, nickname)
        await _upsert_profile(session, uid, profile)
        events_seeded = await _replace_events(session, uid, events)
        await session.commit()
    except Exception:
        if own_session and session is not None:
            await session.rollback()
        raise
    finally:
        if own_session and session_ctx is not None:
            await session_ctx.__aexit__(None, None, None)

    cleared = await asyncio.to_thread(_clear_episodic_memories_sync, uid)
    added = await asyncio.to_thread(_seed_episodic_sync, uid, episodic)
    logger.info(
        "seed persona %s user=%s events=%s cleared_episodic=%s added_episodic=%s",
        persona_ref,
        uid,
        events_seeded,
        cleared,
        added,
    )
    return {
        "ok": True,
        "persona_ref": persona_ref,
        "user_id": uid,
        "phone": phone,
        "display_name": fixture.get("display_name"),
        "events": events_seeded,
        "episodic_cleared": cleared,
        "episodic_added": added,
    }


async def resolve_eval_user_id(db: AsyncSession) -> str:
    """返回批量评测使用的固定 user_id。"""
    if EVAL_USER_ID:
        result = await db.execute(select(User).where(User.id == EVAL_USER_ID))
        user = result.scalar_one_or_none()
        if user:
            return user.id
    result = await db.execute(select(User).where(User.phone == EVAL_USER_PHONE))
    user = result.scalar_one_or_none()
    if user:
        return user.id
    return EVAL_USER_ID


# ============ 查看 persona 数据（async + ORM） ============


async def get_persona_data(
    persona_ref: str = "persona_a_zhang",
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """读取 eval 用户当前库内原始数据（画像 / 事件 / 情节记忆）。"""
    own_session = db is None
    session_ctx = SessionLocal() if own_session else None
    session = session_ctx if own_session else db
    try:
        if own_session:
            session = await session_ctx.__aenter__()  # type: ignore[union-attr]

        user = (
            await session.execute(select(User).where(User.id == EVAL_USER_ID))
        ).scalar_one_or_none()
        if user is None:
            user = (
                await session.execute(
                    select(User).where(User.phone == EVAL_USER_PHONE)
                )
            ).scalar_one_or_none()

        uid = user.id if user else None
        out: dict[str, Any] = {
            "persona_ref": persona_ref,
            "user_id": uid,
            "seeded": False,
            "user": None,
            "profile_json": None,
            "profile_last_updated": None,
            "events": [],
            "episodic": [],
        }
        if user is None or not uid:
            return out

        out["user"] = {
            "id": user.id,
            "phone": user.phone,
            "nickname": user.nickname,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }

        profile_row = (
            await session.execute(
                select(UserProfile).where(UserProfile.user_id == uid)
            )
        ).scalar_one_or_none()
        if profile_row and profile_row.profile_json:
            try:
                out["profile_json"] = json.loads(profile_row.profile_json)
            except json.JSONDecodeError:
                out["profile_json"] = profile_row.profile_json
            out["profile_last_updated"] = (
                profile_row.last_updated.isoformat()
                if profile_row.last_updated
                else None
            )

        events = (
            await session.execute(
                select(UserEvent)
                .where(UserEvent.user_id == uid)
                .order_by(
                    UserEvent.occurred_at.desc(),
                    UserEvent.detected_at.desc(),
                )
            )
        ).scalars().all()
        out["events"] = [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "summary": e.summary,
                "occurred_at": e.occurred_at,
                "importance": e.importance,
                "status": e.status,
                "detected_at": e.detected_at,
            }
            for e in events
        ]
    finally:
        if own_session and session_ctx is not None:
            await session_ctx.__aexit__(None, None, None)

    try:
        from app.services.mem0_engine import get_mem0

        data = await asyncio.to_thread(get_mem0().get_all, uid)
        items = (
            data.get("results", data.get("memories", []))
            if isinstance(data, dict)
            else data
        )
        if isinstance(items, list):
            out["episodic"] = [
                {
                    "id": it.get("id"),
                    "memory": it.get("memory"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                }
                for it in items
            ]
    except Exception as e:
        out["episodic_error"] = str(e)

    out["seeded"] = bool(
        out.get("profile_json") and (out.get("events") or out.get("episodic"))
    )
    return out
