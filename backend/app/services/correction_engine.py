"""纠错清理：用户在对话里 intent=correction 时，主动 LLM 判别并软删/改三层记忆。

入口：``run_correction_cleanup(user_id, conversation_id, turn_id, messages)``

数据流：
1. 从 messages 末尾取最近 6 轮，定位"被纠正的 assistant 回复" + "用户纠正"
2. 三层定点检索：episodic / events / profile （结构化字段）
3. 一次 LLM 调用，输出每条候选的 action（keep / deprecate / update）+ confidence
4. 仅 confidence >= CONFIDENCE_THRESHOLD 才执行；其余仅写 memory_deprecations(action='audit_only')
5. 三层软处理：
   - episodic：写 deprecation 表（build_context 自动过滤），可选 update 时新写一条
   - event：UPDATE user_events SET status='deprecated'
   - profile：在 JSON 里清空字段，同时把"被纠正的旧值"追加进 interaction_history.user_corrections

全程不抛异常到上游 —— 失败只 logger.warning，避免影响对话主链路。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = float(os.getenv("CORRECTION_CONFIDENCE_THRESHOLD", "0.7"))
CORRECTION_MODEL = os.getenv(
    "CORRECTION_MODEL", os.getenv("EXTRACT_MODEL", "qwen3.7-plus")
)

# 限定候选量级，避免 prompt 太长
MAX_EPISODIC = 6
MAX_EVENTS = 5
MAX_PROFILE_FIELDS = 6


CORRECTION_SYSTEM_PROMPT = """\
你是一个【记忆维护助手】。用户刚刚在对话里【纠正了】MemoBot 的某个说法。
你的任务：从下列「候选旧记忆」中找出与用户纠正【直接矛盾】的条目，
判断如何处理。**不要泛化、不要清扫所有看起来沾边的旧记忆。**

输出严格 JSON 对象：
{
  "actions": [
    {
      "ref": "<候选条目的 ref>",
      "action": "keep" | "deprecate" | "update",
      "confidence": 0.0-1.0,
      "reason": "≤30字简述为什么",
      "new_text": "仅 action=update 时必填，给出修正后的简洁中文事实（≤50字，不带主语）"
    }
  ],
  "banned_entities": [
    "<被本次纠正彻底否定的实体词，未来写入记忆时必须过滤的词，每个≤8字>"
  ]
}

actions 规则：
- "keep"：与本次纠正无关，或不能明确判断 → keep
- "deprecate"：旧记忆与用户纠正明确矛盾，但用户没给出新的等价表达 → deprecate
- "update"：旧记忆部分对，新表达能整段替换它 → update
- confidence ≤ 0.6 时优先返回 keep
- 一次纠正通常只影响 0-3 条；不要为了凑数把无关的也算上

banned_entities 规则（**核心**）：
- 仅放"用户已**明确否定**的虚构/错误**名词**"，不要放正确实体、不要放副词形容词
- 这些词在未来新增记忆时会被**字面过滤**，所以宁缺毋滥（仅 0-3 个）
- 若不确定就留空数组

示例 1：
  用户纠正："小孙孙是儿子的同学，不是宠物；我家也没养什么小鹏"
  被纠正回复："你们家是不是养了两只小鹏：小孙孙、小魏魏"
  候选：
    [E1] episodic: 儿子9岁半，养两只小鹏：小孙孙、小魏魏喜好打闹
    [V1] event: [experience] 用户家养了两只小鹏
    [P1] profile.social.relationships: {"小孙孙": "宠物"}
  输出：
  {
    "actions": [
      {"ref":"E1","action":"update","confidence":0.95,
       "new_text":"儿子9岁半，有两个同学小孙孙和小魏魏","reason":"小鹏=同学而非宠物"},
      {"ref":"V1","action":"deprecate","confidence":0.9,"reason":"养小鹏事件本身有误"},
      {"ref":"P1","action":"update","confidence":0.85,
       "new_text":"小孙孙：儿子的同学","reason":"关系属性纠正"}
    ],
    "banned_entities": ["小鹏"]
  }

示例 2：
  用户纠正："不是这样的，我没说过我吃过涮羊肉"
  候选：[E1] episodic: 用户经常吃涮羊肉
  输出：
  {"actions":[{"ref":"E1","action":"deprecate","confidence":0.85,"reason":"用户否认"}],
   "banned_entities":["涮羊肉"]}

不要解释、不要 Markdown、只输出 JSON 对象。
"""


# 用户纠错语里的常见"无意义停用词"，从中提取 entity 时跳过
_STOP_TOKENS = {
    "用户", "MemoBot", "你不是", "没注意", "不是这样", "你记错",
    "你是不是", "你怎么", "什么", "哪里", "哪儿", "为什么",
    "请你", "告诉我", "更新", "记忆", "对吧", "好不好",
    "我们家", "我家", "我儿子", "我妻子",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slice_recent_turns(messages: list[dict], max_pairs: int = 3) -> list[dict]:
    """取末尾 N 对 user/assistant（含最后这条 user 纠正）。"""
    flat = [m for m in messages if m.get("role") in ("user", "assistant")]
    return flat[-(max_pairs * 2 + 1):]


def _extract_correction_target(
    messages: list[dict],
) -> tuple[str, str]:
    """返回 (user_correction_text, assistant_target_text)。"""
    flat = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not flat:
        return "", ""
    user_text = ""
    if flat[-1].get("role") == "user":
        user_text = flat[-1].get("content") or ""
    # 最近一条 assistant
    asst_text = ""
    for m in reversed(flat[:-1]) if user_text else reversed(flat):
        if m.get("role") == "assistant":
            asst_text = m.get("content") or ""
            break
    return user_text, asst_text


# ============ 三层候选检索 ============




def _extract_query_tokens(query: str) -> list[str]:
    """从 query 中提取候选 token，用于 mem0/event LIKE 检索。

    中文不分词，对长段切 2-3 字 grams；英数 ≥3 字直接当 token。
    停用词过滤含子串过滤（避免「我家」「告诉我」等噪音被拆分后漏过）。
    """
    import re

    segs = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]{3,}", query or "")
    cands: list[str] = []
    for seg in segs:
        if not re.match(r"[\u4e00-\u9fff]", seg):
            cands.append(seg)
            continue
        if len(seg) <= 3:
            cands.append(seg)
            continue
        # 长中文段：切 3-gram + 2-gram
        for n in (3, 2):
            for i in range(len(seg) - n + 1):
                cands.append(seg[i:i + n])

    seen: set[str] = set()
    out: list[str] = []
    for t in cands:
        if not t or t in seen:
            continue
        if t in _STOP_TOKENS:
            continue
        # 子串命中停用词也跳过（如「我家最近」含「我家」）
        if any(stop in t or t in stop for stop in _STOP_TOKENS if len(stop) >= 2):
            continue
        seen.add(t)
        out.append(t)
    return out


def _search_event_candidates(
    conn: sqlite3.Connection, user_id: str, query: str
) -> list[dict]:
    """用 query 中的 token 做 LIKE 预筛。
    query 已经在调用方拼好 = assistant_reply + user_correction。
    """
    tokens = _extract_query_tokens(query)[:8]
    if not tokens:
        return []

    rows: list[tuple] = []
    seen: set[str] = set()
    for tok in tokens:
        cur = conn.execute(
            "SELECT event_id, event_type, summary FROM user_events "
            "WHERE user_id=? AND status='active' AND summary LIKE ? LIMIT ?",
            (user_id, f"%{tok}%", MAX_EVENTS),
        )
        for ev_id, ev_type, summary in cur.fetchall():
            if ev_id in seen:
                continue
            seen.add(ev_id)
            rows.append((ev_id, ev_type, summary))
            if len(rows) >= MAX_EVENTS:
                break
        if len(rows) >= MAX_EVENTS:
            break
    return [
        {
            "ref": f"V{i+1}",
            "source": "event",
            "id": ev_id,
            "text": f"[{ev_type}] {summary}",
        }
        for i, (ev_id, ev_type, summary) in enumerate(rows)
    ]


def _search_episodic_candidates_multi(
    engine, user_id: str, query: str
) -> list[dict]:
    """除了用整句搜，还用 query 中的每个 token 单独 search，提升召回。

    这是 T5/T7 失败的根因之一：整句"看来你还是不记得啊，我们家最近养了什么"
    embedding 命中率低，但单 token "小鹏" / "养" 几乎一定能召回。
    """
    if not query:
        return []
    seen: dict[str, dict] = {}
    # 1. 整句搜
    try:
        res = engine.search(query, user_id=user_id, limit=MAX_EPISODIC)
        for it in (res or {}).get("results", []) or []:
            mem_id = str(it.get("id"))
            if mem_id not in seen and it.get("memory"):
                seen[mem_id] = {
                    "source": "episodic",
                    "id": mem_id,
                    "text": (it.get("memory") or "").strip(),
                }
    except Exception as e:
        logger.warning("[correction] mem0 sentence search failed: %s", e)
    # 2. 每个 token 单独搜（重要：能召回字面命中）
    for tok in _extract_query_tokens(query)[:5]:
        if len(seen) >= MAX_EPISODIC * 2:
            break
        try:
            res = engine.search(tok, user_id=user_id, limit=3)
            for it in (res or {}).get("results", []) or []:
                mem_id = str(it.get("id"))
                if mem_id not in seen and it.get("memory"):
                    seen[mem_id] = {
                        "source": "episodic",
                        "id": mem_id,
                        "text": (it.get("memory") or "").strip(),
                    }
        except Exception as e:
            logger.debug("[correction] mem0 token search failed (%s): %s", tok, e)

    items = list(seen.values())[:MAX_EPISODIC]
    for i, it in enumerate(items):
        it["ref"] = f"E{i+1}"
    return items


def _flatten_profile_fields(profile: dict) -> list[dict]:
    """把可能与纠错相关的扁平字段都列出来，最多 MAX_PROFILE_FIELDS。"""
    out: list[dict] = []
    if not isinstance(profile, dict):
        return out
    p = profile.get("profile") or {}
    # 优先关系层（最常被纠正）
    rel = (p.get("social") or {}).get("relationships")
    if isinstance(rel, dict) and isinstance(rel.get("value"), dict):
        for name, v in rel["value"].items():
            out.append({
                "source": "profile",
                "id": f"profile.social.relationships.{name}",
                "text": f"{name}: {json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v}",
            })
            if len(out) >= MAX_PROFILE_FIELDS:
                break
    # 再扫 basic / career
    for section_name in ("basic", "career", "interests", "goals_pains"):
        if len(out) >= MAX_PROFILE_FIELDS:
            break
        section = p.get(section_name) or {}
        if not isinstance(section, dict):
            continue
        for k, v in section.items():
            if not isinstance(v, dict) or v.get("value") is None:
                continue
            out.append({
                "source": "profile",
                "id": f"profile.{section_name}.{k}",
                "text": f"{section_name}.{k}: {v.get('value')}",
            })
            if len(out) >= MAX_PROFILE_FIELDS:
                break
    # 加 ref
    for i, item in enumerate(out):
        item["ref"] = f"P{i+1}"
    return out


# ============ LLM 判别 ============


def _llm_judge(
    user_correction: str,
    asst_target: str,
    candidates: list[dict],
) -> tuple[list[dict], list[str]]:
    """返回 (actions, banned_entities)。"""
    if not candidates:
        return [], []
    try:
        import openai

        client = openai.OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv(
                "OPENAI_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        )
        cand_lines = "\n".join(
            f"  [{c['ref']}] {c['source']}: {c['text'][:120]}"
            for c in candidates
        )
        user_block = (
            f"用户纠正：{user_correction}\n"
            f"被纠正回复：{asst_target}\n\n"
            f"候选旧记忆：\n{cand_lines}"
        )
        resp = client.chat.completions.create(
            model=CORRECTION_MODEL,
            messages=[
                {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_block},
            ],
            temperature=0,
            extra_body={"enable_thinking": False},
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        actions: list[dict] = []
        banned: list[str] = []
        # 兼容两种格式：dict / 老数组
        if isinstance(parsed, dict):
            actions = parsed.get("actions", []) or []
            banned = parsed.get("banned_entities", []) or []
        elif isinstance(parsed, list):
            actions = parsed
        ref_set = {c["ref"] for c in candidates}
        actions = [
            x for x in actions
            if isinstance(x, dict) and x.get("ref") in ref_set
        ]
        # banned 清洗
        banned = [
            str(b).strip() for b in banned
            if isinstance(b, str) and 1 <= len(str(b).strip()) <= 8
        ]
        return actions, banned
    except Exception as e:
        logger.warning("[correction] llm judge failed: %s", e)
        return [], []


# ============ 三层软处理 ============


def _apply_episodic(
    engine,
    user_id: str,
    cand: dict,
    decision: dict,
    conn: sqlite3.Connection,
    ctx_meta: dict,
):
    mem_id = cand["id"]
    action = decision["action"]
    if action == "update" and decision.get("new_text"):
        try:
            engine.update(mem_id, decision["new_text"])
        except Exception as e:
            logger.warning("[correction] mem0 update failed: %s", e)
            action = "deprecate"  # update 失败降级为 deprecate
    # 无论 update / deprecate 都记 deprecation（update 时 ref_id 仍是原 mem_id，build_context 不会过滤更新成功的）
    # 关键设计：update 后 mem0 内已替换，无需过滤；只在 deprecate 时才过滤
    if action == "deprecate":
        conn.execute(
            "INSERT INTO memory_deprecations "
            "(user_id, source, ref_id, original_text, reason, "
            "correction_conversation_id, correction_turn_id, "
            "llm_confidence, action, new_text, deprecated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, "episodic", mem_id, cand["text"][:500],
                decision.get("reason", "")[:200],
                ctx_meta.get("conversation_id"), ctx_meta.get("turn_id"),
                float(decision.get("confidence", 0)),
                "deprecate", None, _now_iso(),
            ),
        )
    elif action == "update":
        conn.execute(
            "INSERT INTO memory_deprecations "
            "(user_id, source, ref_id, original_text, reason, "
            "correction_conversation_id, correction_turn_id, "
            "llm_confidence, action, new_text, deprecated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, "episodic", mem_id, cand["text"][:500],
                decision.get("reason", "")[:200],
                ctx_meta.get("conversation_id"), ctx_meta.get("turn_id"),
                float(decision.get("confidence", 0)),
                "update", decision.get("new_text", "")[:500], _now_iso(),
            ),
        )


def _apply_event(
    conn: sqlite3.Connection,
    user_id: str,
    cand: dict,
    decision: dict,
    ctx_meta: dict,
):
    event_id = cand["id"]
    action = decision["action"]
    if action == "deprecate":
        conn.execute(
            "UPDATE user_events SET status='deprecated', updated_at=? "
            "WHERE event_id=? AND user_id=?",
            (_now_iso(), event_id, user_id),
        )
    elif action == "update" and decision.get("new_text"):
        # update 时改写 summary
        conn.execute(
            "UPDATE user_events SET summary=?, updated_at=? "
            "WHERE event_id=? AND user_id=?",
            (decision["new_text"][:200], _now_iso(), event_id, user_id),
        )
    conn.execute(
        "INSERT INTO memory_deprecations "
        "(user_id, source, ref_id, original_text, reason, "
        "correction_conversation_id, correction_turn_id, "
        "llm_confidence, action, new_text, deprecated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, "event", event_id, cand["text"][:500],
            decision.get("reason", "")[:200],
            ctx_meta.get("conversation_id"), ctx_meta.get("turn_id"),
            float(decision.get("confidence", 0)),
            action, decision.get("new_text"), _now_iso(),
        ),
    )


def _apply_profile(
    conn: sqlite3.Connection,
    user_id: str,
    cand: dict,
    decision: dict,
    ctx_meta: dict,
):
    """profile 软处理：
    - relationship.<name>：直接从 social.relationships.value 中删除 / 改写
    - 其它字段：清空 value（保留结构）；如 update 给了 new_text 则覆盖
    审计：写 interaction_history.user_corrections（追加旧值）
    """
    path = cand["id"]  # e.g. profile.social.relationships.小孙孙
    action = decision["action"]
    cur = conn.cursor()
    cur.execute(
        "SELECT profile_json FROM user_profiles WHERE user_id=?",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return
    try:
        profile = json.loads(row[0])
    except Exception:
        return

    # 拆 path：profile.<section>.<key>[.<sub>]
    parts = path.split(".")
    if len(parts) < 3 or parts[0] != "profile":
        return
    p = profile.get("profile") or {}
    section, key = parts[1], parts[2]
    old_value = None

    if parts[1] == "social" and parts[2] == "relationships" and len(parts) == 4:
        name = parts[3]
        rel = (p.get("social") or {}).get("relationships", {})
        value = rel.get("value") if isinstance(rel, dict) else None
        if isinstance(value, dict) and name in value:
            old_value = value[name]
            if action == "deprecate":
                value.pop(name, None)
            elif action == "update" and decision.get("new_text"):
                # 写为 "rel" 简描述
                value[name] = decision["new_text"][:80]
    else:
        section_obj = p.get(section) or {}
        if isinstance(section_obj, dict) and isinstance(section_obj.get(key), dict):
            old_value = section_obj[key].get("value")
            if action == "deprecate":
                section_obj[key]["value"] = None
            elif action == "update" and decision.get("new_text"):
                section_obj[key]["value"] = decision["new_text"][:200]

    # 写 user_corrections 审计
    history = p.setdefault("interaction_history", {})
    corrs = history.setdefault("user_corrections", {})
    if isinstance(corrs, dict):
        history_value = corrs.get("value")
        if not isinstance(history_value, str):
            history_value = ""
        new_line = (
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d')}] {path}={old_value} "
            f"→ {action}"
        )
        corrs["value"] = (history_value + ("、" if history_value else "") + new_line)[:1500]
        corrs["confidence"] = 0.99
        corrs["updated_at"] = _now_iso()

    cur.execute(
        "UPDATE user_profiles SET profile_json=?, last_updated=datetime('now') "
        "WHERE user_id=?",
        (json.dumps(profile, ensure_ascii=False), user_id),
    )

    conn.execute(
        "INSERT INTO memory_deprecations "
        "(user_id, source, ref_id, original_text, reason, "
        "correction_conversation_id, correction_turn_id, "
        "llm_confidence, action, new_text, deprecated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, "profile", path,
            f"{path}={old_value}"[:500],
            decision.get("reason", "")[:200],
            ctx_meta.get("conversation_id"), ctx_meta.get("turn_id"),
            float(decision.get("confidence", 0)),
            action, decision.get("new_text"), _now_iso(),
        ),
    )


def _apply_audit_only(
    conn: sqlite3.Connection,
    user_id: str,
    cand: dict,
    decision: dict,
    ctx_meta: dict,
):
    """低置信度：只写审计不动数据。"""
    conn.execute(
        "INSERT INTO memory_deprecations "
        "(user_id, source, ref_id, original_text, reason, "
        "correction_conversation_id, correction_turn_id, "
        "llm_confidence, action, new_text, deprecated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, cand["source"], cand["id"], cand["text"][:500],
            decision.get("reason", "")[:200],
            ctx_meta.get("conversation_id"), ctx_meta.get("turn_id"),
            float(decision.get("confidence", 0)),
            "audit_only", decision.get("new_text"), _now_iso(),
        ),
    )


def _apply_banned_entities(
    conn: sqlite3.Connection,
    user_id: str,
    entities: list[str],
    ctx_meta: dict,
):
    """把被用户彻底否定的实体写入硬封禁表。

    后续 extract_and_store_memory / events / profile 写入时会字面过滤这些词。
    用 source='entity'、ref_id=词本身、action='deprecate'。
    """
    if not entities:
        return
    # 已存在的（仍生效）就不重复写
    rows = conn.execute(
        "SELECT ref_id FROM memory_deprecations "
        "WHERE user_id=? AND source='entity' AND restored_at IS NULL",
        (user_id,),
    ).fetchall()
    existing = {r[0] for r in rows}
    for ent in entities:
        e = (ent or "").strip()
        if not e or e in existing:
            continue
        conn.execute(
            "INSERT INTO memory_deprecations "
            "(user_id, source, ref_id, original_text, reason, "
            "correction_conversation_id, correction_turn_id, "
            "llm_confidence, action, new_text, deprecated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, "entity", e, e,
                "用户在 correction 中彻底否定该实体",
                ctx_meta.get("conversation_id"),
                ctx_meta.get("turn_id"),
                1.0, "deprecate", None, _now_iso(),
            ),
        )


def load_banned_entities(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """供 celery 写入链路调用：读取该用户当前生效的禁用实体词。"""
    try:
        rows = conn.execute(
            "SELECT ref_id FROM memory_deprecations "
            "WHERE user_id=? AND source='entity' AND restored_at IS NULL",
            (user_id,),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def text_hits_banned(text: str, banned: list[str]) -> bool:
    """判断一段文本是否命中任何禁用词（字面包含）。"""
    if not banned or not text:
        return False
    return any(b in text for b in banned)


# ============ 入口 ============


def run_correction_cleanup(
    user_id: str,
    conversation_id: str | None,
    turn_id: str | None,
    messages: list[dict],
    db_path: str,
    engine,
) -> dict:
    """同步执行（由 celery task 调度）。返回统计 dict 供日志。"""
    user_text, asst_text = _extract_correction_target(messages)
    if not user_text:
        return {"skipped": "no_user_correction"}

    query = (asst_text + " " + user_text).strip()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        # 1. 三层候选（episodic 用多 token 召回；event LIKE 拼 asst 实体词）
        ep_cands = _search_episodic_candidates_multi(engine, user_id, query)
        ev_cands = _search_event_candidates(conn, user_id, query)
        cur.execute(
            "SELECT profile_json FROM user_profiles WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
        profile = json.loads(row[0]) if row else {}
        pf_cands = _flatten_profile_fields(profile)
        for i, c in enumerate(ep_cands): c["ref"] = f"E{i+1}"
        for i, c in enumerate(ev_cands): c["ref"] = f"V{i+1}"
        for i, c in enumerate(pf_cands): c["ref"] = f"P{i+1}"
        candidates = ep_cands + ev_cands + pf_cands
        if not candidates:
            return {"skipped": "no_candidates"}

        # 2. LLM 判别（同时拿到 banned_entities）
        decisions, banned = _llm_judge(user_text, asst_text, candidates)
        if not decisions and not banned:
            return {"skipped": "no_decisions", "candidates": len(candidates)}

        # 3. 应用
        ctx_meta = {"conversation_id": conversation_id, "turn_id": turn_id}
        cand_by_ref = {c["ref"]: c for c in candidates}
        stats = {
            "candidates": len(candidates),
            "deprecate": 0, "update": 0, "audit_only": 0, "keep": 0,
            "banned_entities": banned,
            "by_source": {"episodic": 0, "event": 0, "profile": 0},
        }
        # 3.0 先写实体硬封禁
        _apply_banned_entities(conn, user_id, banned, ctx_meta)
        for d in decisions:
            cand = cand_by_ref.get(d["ref"])
            if not cand:
                continue
            action = d.get("action", "keep")
            conf = float(d.get("confidence", 0) or 0)
            if action == "keep":
                stats["keep"] += 1
                continue
            if conf < CONFIDENCE_THRESHOLD:
                _apply_audit_only(conn, user_id, cand, d, ctx_meta)
                stats["audit_only"] += 1
                continue
            if cand["source"] == "episodic":
                _apply_episodic(engine, user_id, cand, d, conn, ctx_meta)
            elif cand["source"] == "event":
                _apply_event(conn, user_id, cand, d, ctx_meta)
            elif cand["source"] == "profile":
                _apply_profile(conn, user_id, cand, d, ctx_meta)
            stats[action] = stats.get(action, 0) + 1
            stats["by_source"][cand["source"]] += 1

        conn.commit()
        logger.info(
            "[correction] user=%s conv=%s turn=%s stats=%s",
            user_id, conversation_id, turn_id, stats,
        )
        return stats
    except Exception as e:
        logger.exception("[correction] cleanup failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        conn.close()
