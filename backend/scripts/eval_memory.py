"""一键评测四层记忆 + 数据质量分析，供人工审阅。
用法（在 ECS 上）：
    docker compose exec backend python3 scripts/eval_memory.py [phone]
不传 phone 默认评测最新登录的用户。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from difflib import SequenceMatcher

# 让脚本不论从哪里启动都能 import app.*
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")

H = "=" * 70
h = "-" * 70


def _hr(title: str):
    print(f"\n{H}\n {title}\n{H}")


def _sub(title: str):
    print(f"\n{h}\n {title}\n{h}")


def pick_user(con: sqlite3.Connection, phone: str | None) -> tuple[str, str]:
    cur = con.cursor()
    if phone:
        row = cur.execute(
            "SELECT id, phone FROM users WHERE phone=?", (phone,)
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT id, phone FROM users ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("找不到用户")
    return row


def dump_conversations(con, uid):
    _hr("【对话记录 conversations】(SQLite, 原始消息)")
    rows = con.execute(
        "SELECT id, title, messages_json, created_at, updated_at "
        "FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (uid,),
    ).fetchall()
    print(f"共 {len(rows)} 个会话\n")
    for cid, title, mj, cat, uat in rows:
        msgs = json.loads(mj or "[]")
        print(f"▸ [{uat}] {title}  ({len(msgs)} 条消息)  id={cid[:8]}")
        for i, m in enumerate(msgs[-6:], start=max(1, len(msgs) - 5)):
            role = m.get("role", "?")
            content = (m.get("content") or "").replace("\n", " ")
            print(f"    {i:2d} [{role}] {content[:120]}")
        print()


def dump_profile(con, uid):
    _hr("【用户画像 user_profiles】(SQLite)")
    row = con.execute(
        "SELECT profile_json, last_updated FROM user_profiles WHERE user_id=?",
        (uid,),
    ).fetchone()
    if not row:
        print("(无画像)")
        return
    p = json.loads(row[0] or "{}")
    print(f"last_updated = {row[1]}")
    prof = p.get("profile", {})
    for section, fields in prof.items():
        print(f"\n  [{section}]")
        for k, v in fields.items():
            if isinstance(v, dict) and "value" in v:
                val = v.get("value")
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                else:
                    val = str(val)
                print(
                    f"    - {k:20s} = {val[:100]}"
                    f"  (conf={v.get('confidence')}, t={v.get('updated_at', '')[:19]})"
                )
            else:
                print(f"    - {k}: {v}")


def dump_relationships(con, uid):
    _hr("【社会关系 social.relationships】")
    row = con.execute(
        "SELECT profile_json FROM user_profiles WHERE user_id=?", (uid,)
    ).fetchone()
    if not row:
        print("(无)")
        return
    rel = (
        json.loads(row[0] or "{}").get("profile", {}).get("social", {}).get("relationships")
    )
    if not rel:
        print("(无 relationships 字段)")
        return
    print(json.dumps(rel, ensure_ascii=False, indent=2))


def dump_events(con, uid):
    _hr("【事件记忆 user_events】(SQLite)")
    rows = con.execute(
        "SELECT event_id, event_type, summary, importance, status, mention_count, "
        "occurred_at, detected_at, last_referenced_at "
        "FROM user_events WHERE user_id=? ORDER BY detected_at DESC",
        (uid,),
    ).fetchall()
    print(f"共 {len(rows)} 条事件\n")
    for eid, etype, summary, imp, status, mc, oat, dat, lat in rows:
        print(
            f"▸ [{etype:14s}] imp={imp:.2f} mc={mc} status={status} "
            f"occurred={oat or '?':10s} detected={dat[:19]}"
        )
        print(f"    {summary}")
        print(f"    id={eid}")


def dump_memories(uid):
    """读 Mem0/Qdrant 情节记忆"""
    _hr("【聊天记忆 memories】(Mem0/Qdrant)")
    try:
        from app.services.mem0_engine import get_mem0  # type: ignore
    except Exception as e:
        print(f"无法加载 mem0 引擎: {e}")
        return []
    try:
        data = get_mem0().get_all(uid)
    except Exception as e:
        print(f"get_all 失败: {e}")
        return []
    items = data.get("results", data.get("memories", []))
    print(f"共 {len(items)} 条情节记忆\n")
    items_sorted = sorted(
        items, key=lambda x: x.get("created_at") or "", reverse=True
    )
    for it in items_sorted:
        cat = (it.get("created_at") or "")[:19]
        uat = (it.get("updated_at") or "")[:19]
        print(f"▸ [{cat}] up={uat}  id={it.get('id', '')[:8]}")
        print(f"    {it.get('memory', '')}")
    return items_sorted


def analyze_redundancy(items):
    _sub("【冗余度分析 (聊天记忆两两相似度 ≥ 0.55 的对)】")
    texts = [(it.get("id", "")[:8], (it.get("memory") or "").strip()) for it in items]
    pairs = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i][1], texts[j][1]
            if not a or not b:
                continue
            score = SequenceMatcher(None, a, b).ratio()
            if score >= 0.55:
                pairs.append((score, texts[i], texts[j]))
    pairs.sort(reverse=True)
    if not pairs:
        print("✅ 未发现明显重复（阈值 0.55）")
        return
    print(f"⚠️ 发现 {len(pairs)} 对疑似重复：\n")
    for score, (ia, ta), (ib, tb) in pairs[:20]:
        print(f"  sim={score:.2f}")
        print(f"    {ia}: {ta}")
        print(f"    {ib}: {tb}\n")


def check_ordering(items):
    _sub("【聊天记忆原始返回顺序检查】")
    if len(items) < 2:
        print("条数不足，跳过")
        return
    raw_cats = [(it.get("created_at") or "")[:19] for it in items]
    is_desc = all(raw_cats[i] >= raw_cats[i + 1] for i in range(len(raw_cats) - 1))
    print(
        "Mem0.get_all 原始顺序："
        + ("✅ 已按 created_at 降序" if is_desc else "❌ 未按降序（前端需自行排序）")
    )
    print("前 3 条 created_at:", raw_cats[:3])


def check_relationships_sync(con, uid):
    _sub("【社会关系 vs 最近对话 同步检查】")
    conv_rows = con.execute(
        "SELECT messages_json FROM conversations WHERE user_id=? "
        "ORDER BY updated_at DESC LIMIT 3",
        (uid,),
    ).fetchall()
    text = " ".join(json.loads(r[0] or "[]").__str__() for r in conv_rows)

    prow = con.execute(
        "SELECT profile_json FROM user_profiles WHERE user_id=?", (uid,)
    ).fetchone()
    rel = (
        json.loads(prow[0] or "{}").get("profile", {}).get("social", {}).get("relationships", {})
    )
    rel_value = rel.get("value", {}) if isinstance(rel, dict) else {}
    rel_keys = list(rel_value.keys()) if isinstance(rel_value, dict) else []

    if not rel_keys:
        print("(画像里还没有任何 relationships)")
    else:
        print(f"画像中已记录 {len(rel_keys)} 个人: {rel_keys}")

    audit = con.execute(
        "SELECT created_at, action, old_value, new_value FROM memory_audit_log "
        "WHERE user_id=? AND dimension_path='social.relationships' "
        "ORDER BY id DESC LIMIT 5",
        (uid,),
    ).fetchall()
    print("\n最近 5 条 social.relationships 审计：")
    if not audit:
        print("  (无)")
    for cat, action, ov, nv in audit:
        print(f"  - {cat} [{action or 'EMPTY'}]")
        print(f"      old: {ov[:100]}")
        print(f"      new: {nv[:100]}")


def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else None
    con = sqlite3.connect(DB_PATH)
    try:
        uid, uphone = pick_user(con, phone)
        print(f"用户 phone={uphone}  uid={uid}\nDB={DB_PATH}")

        dump_conversations(con, uid)
        dump_profile(con, uid)
        dump_relationships(con, uid)
        dump_events(con, uid)
        items = dump_memories(uid)
        analyze_redundancy(items)
        check_ordering(items)
        check_relationships_sync(con, uid)
    finally:
        con.close()


if __name__ == "__main__":
    main()
