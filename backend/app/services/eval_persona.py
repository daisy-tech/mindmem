"""评测用合成 persona 灌库与解析。

批量评测使用固定 eval 用户（persona_a_zhang / 老张），需手动灌库一次；
单条调试仍使用登录用户真实记忆。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")

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


def _clear_episodic_memories(user_id: str) -> int:
    """清空 Qdrant 中该用户的情节记忆。"""
    try:
        from app.services.mem0_engine import get_mem0

        mem = get_mem0()
        data = mem.get_all(user_id)
        items = data.get("results", data.get("memories", [])) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        deleted = 0
        for it in items:
            mid = it.get("id") or it.get("memory_id")
            if mid:
                mem.delete(mid, user_id)
                deleted += 1
        return deleted
    except Exception as e:
        logger.warning("清空情节记忆失败 user=%s: %s", user_id, e)
        return 0


def _seed_sqlite(user_id: str, fixture: dict[str, Any]) -> None:
    phone = fixture.get("phone") or EVAL_USER_PHONE
    nickname = fixture.get("nickname") or "老张"
    profile = fixture.get("profile") or {"profile": {}}
    events = fixture.get("events") or []
    now = _now_iso()

    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        row = cur.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            row = cur.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row:
            user_id = row[0]
            cur.execute(
                "UPDATE users SET phone=?, nickname=? WHERE id=?",
                (phone, nickname, user_id),
            )
        else:
            cur.execute(
                "INSERT INTO users (id, phone, nickname, created_at) VALUES (?, ?, ?, ?)",
                (user_id, phone, nickname, now),
            )

        profile_json = json.dumps(profile, ensure_ascii=False)
        if cur.execute(
            "SELECT 1 FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone():
            cur.execute(
                "UPDATE user_profiles SET profile_json=?, last_updated=datetime('now') WHERE user_id=?",
                (profile_json, user_id),
            )
        else:
            cur.execute(
                "INSERT INTO user_profiles (user_id, profile_json, last_updated) VALUES (?, ?, datetime('now'))",
                (user_id, profile_json),
            )

        cur.execute("DELETE FROM user_events WHERE user_id=?", (user_id,))
        for ev in events:
            eid = ev.get("event_id") or str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO user_events (
                    event_id, user_id, event_type, summary, details_json, related_json,
                    occurred_at, detected_at, importance, status, mention_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', '[]', ?, ?, ?, 'active', 1, ?, ?)
                """,
                (
                    eid,
                    user_id,
                    ev.get("event_type", "experience"),
                    ev.get("summary", ""),
                    ev.get("occurred_at"),
                    now,
                    float(ev.get("importance", 0.7)),
                    now,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()


def _seed_episodic(user_id: str, fixture: dict[str, Any]) -> int:
    texts = fixture.get("episodic") or []
    if not texts:
        return 0
    from app.services.mem0_engine import get_mem0

    mem = get_mem0()
    added = 0
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        mem.add([{"role": "user", "content": text}], user_id=user_id, infer=False)
        added += 1
    return added


def seed_persona_sync(persona_ref: str = "persona_a_zhang", user_id: str | None = None) -> dict[str, Any]:
    """幂等灌库：SQLite 画像/事件 + Qdrant 情节记忆（先清空再写入）。"""
    fixture = load_persona_fixture(persona_ref)
    uid = user_id or EVAL_USER_ID
    _seed_sqlite(uid, fixture)
    # 重新解析 user_id（可能已存在同 phone 用户）
    con = sqlite3.connect(DB_PATH)
    try:
        phone = fixture.get("phone") or EVAL_USER_PHONE
        row = con.execute(
            "SELECT id FROM users WHERE phone=? OR id=? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1",
            (phone, uid, uid),
        ).fetchone()
        uid = row[0] if row else uid
    finally:
        con.close()

    cleared = _clear_episodic_memories(uid)
    added = _seed_episodic(uid, fixture)
    logger.info(
        "seed persona %s user=%s cleared_episodic=%s added_episodic=%s",
        persona_ref,
        uid,
        cleared,
        added,
    )
    return {
        "ok": True,
        "persona_ref": persona_ref,
        "user_id": uid,
        "phone": fixture.get("phone") or EVAL_USER_PHONE,
        "display_name": fixture.get("display_name"),
        "events": len(fixture.get("events") or []),
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


def _resolve_user_id_sync() -> str | None:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id FROM users WHERE id=?", (EVAL_USER_ID,)).fetchone()
        if row:
            return row[0]
        row = con.execute(
            "SELECT id FROM users WHERE phone=?", (EVAL_USER_PHONE,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def get_persona_data_sync(persona_ref: str = "persona_a_zhang") -> dict[str, Any]:
    """读取 eval 用户当前库内原始数据（画像 / 事件 / 情节记忆）。"""
    uid = _resolve_user_id_sync()
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
    if not uid:
        return out

    con = sqlite3.connect(DB_PATH)
    try:
        urow = con.execute(
            "SELECT id, phone, nickname, created_at FROM users WHERE id=?", (uid,)
        ).fetchone()
        if urow:
            out["user"] = {
                "id": urow[0],
                "phone": urow[1],
                "nickname": urow[2],
                "created_at": urow[3],
            }
        prow = con.execute(
            "SELECT profile_json, last_updated FROM user_profiles WHERE user_id=?",
            (uid,),
        ).fetchone()
        if prow and prow[0]:
            try:
                out["profile_json"] = json.loads(prow[0])
            except json.JSONDecodeError:
                out["profile_json"] = prow[0]
            out["profile_last_updated"] = prow[1]
        events = con.execute(
            """
            SELECT event_id, event_type, summary, occurred_at, importance, status, detected_at
            FROM user_events WHERE user_id=? ORDER BY occurred_at DESC, detected_at DESC
            """,
            (uid,),
        ).fetchall()
        out["events"] = [
            {
                "event_id": r[0],
                "event_type": r[1],
                "summary": r[2],
                "occurred_at": r[3],
                "importance": r[4],
                "status": r[5],
                "detected_at": r[6],
            }
            for r in events
        ]
    finally:
        con.close()

    try:
        from app.services.mem0_engine import get_mem0

        data = get_mem0().get_all(uid)
        items = data.get("results", data.get("memories", [])) if isinstance(data, dict) else data
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
        out.get("profile_json")
        and (out.get("events") or out.get("episodic"))
    )
    return out
