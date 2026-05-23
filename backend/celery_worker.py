import json
import logging
import os
import re

import openai
from celery import Celery
from celery.schedules import crontab

from app.services.mem0_engine import Mem0Engine

logger = logging.getLogger(__name__)

celery_app = Celery(
    "memobot",
    broker=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://redis:6379/0"),
)

celery_app.conf.beat_schedule = {
    "daily-profile-decay": {
        "task": "celery_worker.decay_all_profiles",
        "schedule": crontab(hour=3, minute=0),  # 每天凌晨 3 点
    },
}

# Single shared engine per worker process
_engine: Mem0Engine | None = None


def _get_engine() -> Mem0Engine:
    global _engine
    if _engine is None:
        _engine = Mem0Engine()
    return _engine


def _get_db_path() -> str:
    return os.getenv("USER_DB_PATH", "/app/data/memobot.db")


# ──────────────────────────────────────────────
# 记忆去重相关
# ──────────────────────────────────────────────

CANDIDATE_EXTRACT_PROMPT = """\
你是一个事实提取器。从用户消息中提炼 0-5 条相互独立的"事实"，每条用一句简洁中文描述。

严格规则：
1. 只提取用户消息中明确表达的内容，不要推断、不要泛化、不要总结情绪。
2. 每条事实是一个客观陈述句，**不要带主语**——记忆本来就是属于这位用户的，不需要再重复
   ❌ "用户dxj定居北京昌平"
   ❌ "dxj定居北京昌平"
   ❌ "他定居北京昌平"
   ✅ "定居北京昌平"
3. 禁止以下元数据噪音：
   - 任何人名/用户名作为主语开头
   - "于2026年5月23日" 等具体日期
   - "该信息于...向MemoBot透露"、"向系统表达"
   - "反映了..."、"表明..."、"体现出..."
4. 多个相关小事实尽量合并成一条更具体的陈述（如"儿子9岁半，养了两只小鹏：小孙孙、小魏魏"）。

正例：
  ["定居北京昌平，每天去海淀上班，做IT", "儿子9岁半", "妻子全职在家带孩子"]
反例（绝对禁止）：
  ["用户dxj是IT从业者，该信息于2026年5月23日向MemoBot透露"]
  ["dxj的邻居80岁"]
  ["用户表达了对北京生活压力很大的感受"]

只输出 JSON 字符串数组，不要有其他文字。无可提取内容则输出 []。"""

MERGE_MEMORY_PROMPT = """\
你需要把"旧记忆"和"新增信息"融合成一条简洁、完整的中文记忆。

规则：
- 保留旧记忆中具体的细节（人名、年龄、地点、习惯等），不能丢失
- 新增信息补充进来或修正旧的
- 输出一句话（最长 60 字），不要列点，不要加日期/平台等元数据
- **不要带主语**（不要"用户/他/dxj"等开头），直接陈述事实
- 不要写"用户XX说"、"该信息于..."、"反映了..."等套话

只输出融合后的那句话，不要其他文字。"""

# 相似度阈值（mem0 的 search 分数 0-1）
SIM_SKIP = 0.82    # ≥此：跳过（默认 0.92 太松，导致措辞稍变就放过）
SIM_UPDATE = 0.62  # ≥此：合并到已有记忆


_SUBJECT_PREFIX_RE = re.compile(
    r"^(用户(?:[\u4e00-\u9fa5A-Za-z0-9]{1,12})?(?:的)?|"
    r"[\u4e00-\u9fa5A-Za-z0-9]{1,12}的|"
    r"他|她|我|本人|该用户|这位用户)"
)


def _strip_subject_prefix(text: str) -> str:
    """剥掉记忆开头的主语前缀，使记忆更简洁。
    "用户dxj定居北京" → "定居北京"
    "dxj的妻子全职在家" → "妻子全职在家"
    """
    s = (text or "").strip()
    # 只剥一次，避免误伤
    m = _SUBJECT_PREFIX_RE.match(s)
    if not m:
        return s
    rest = s[m.end():].lstrip(" ,，。：:")
    # 如果剥完只剩半句话（太短），还是保留原文
    if len(rest) < 4:
        return s
    return rest


def _extract_candidates(messages: list) -> list[str]:
    """用轻量 LLM 从对话中提炼候选事实列表"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return []

    text = "\n".join(m["content"] for m in user_msgs)
    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    try:
        resp = client.chat.completions.create(
            model=os.getenv("EXTRACT_MODEL", "qwen-plus"),
            messages=[
                {"role": "system", "content": CANDIDATE_EXTRACT_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("_extract_candidates failed: %s", e)
        return []


def _llm_merge(old_text: str, new_text: str) -> str:
    """用 LLM 把旧记忆 + 新增信息融合成一句简洁的话"""
    try:
        client = openai.OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        resp = client.chat.completions.create(
            model=os.getenv("EXTRACT_MODEL", "qwen-plus"),
            messages=[
                {"role": "system", "content": MERGE_MEMORY_PROMPT},
                {"role": "user", "content": f"旧记忆：{old_text}\n新增信息：{new_text}"},
            ],
            temperature=0,
        )
        merged = (resp.choices[0].message.content or "").strip()
        merged = merged.strip("`\"' ")
        if not merged or len(merged) > 120:
            # 兜底：取更长更具体的那条，新优先
            return new_text if len(new_text) >= len(old_text) else old_text
        return merged
    except Exception as e:
        logger.warning("_llm_merge failed: %s", e)
        return new_text


def _check_and_store(engine: Mem0Engine, user_id: str, text: str):
    """
    对单条候选事实做相似度检查后决定：跳过 / 合并已有 / 新建。
    所有写入都用 infer=False，避免 Mem0 自身再次抽取/改写产生噪音。
    """
    try:
        results = engine.search(text, user_id=user_id, limit=3)
        items = results.get("results", [])

        if items:
            top = items[0]
            score = float(top.get("score", 0))

            if score >= SIM_SKIP:
                logger.info("[mem] skip (score=%.3f): %s", score, text[:60])
                return "skip"

            if score >= SIM_UPDATE:
                old_text = top.get("memory", "")
                merged = _llm_merge(old_text, text) if old_text and old_text != text else text
                engine.update(top["id"], merged)
                logger.info("[mem] merge (score=%.3f): %s -> %s",
                            score, text[:40], merged[:40])
                return "update"

        engine.add(text, user_id=user_id, infer=False)
        logger.info("[mem] add: %s", text[:60])
        return "add"

    except Exception as e:
        logger.warning("_check_and_store failed: %s", e)
        return "error"


@celery_app.task
def extract_and_store_memory(user_id: str, messages: list):
    """对话结束后，去重后写入情节记忆"""
    engine = _get_engine()

    candidates = _extract_candidates(messages)

    if not candidates:
        # 提炼失败时不写入，避免 Mem0 自身抽取出冗余/带元数据的噪音
        logger.info("[mem] no candidates extracted, skip writing")
        return

    stats = {"skip": 0, "update": 0, "add": 0, "error": 0}
    for raw in candidates:
        text = _strip_subject_prefix(str(raw or ""))
        if not text:
            continue
        action = _check_and_store(engine, user_id, text)
        stats[action] = stats.get(action, 0) + 1

    logger.info("[mem] done for user %s: %s", user_id, stats)



@celery_app.task
def extract_and_store_events(user_id: str, messages: list):
    """对话结束后，提取结构化事件并去重写入 SQLite。

    去重策略（双保险）：
    1. SQLite 关键词预筛：对新事件 summary 提取 3 个关键词，先查 DB 是否已有相似记录
       - 若命中：直接更新 mention_count，跳过向量搜索
    2. Qdrant 向量搜索（阈值 0.75）：语义层面捕捉措辞不同但内容相同的事件
       - score >= 0.75：更新已有记录
       - score < 0.75：新建
    """
    import sqlite3
    import re
    from datetime import datetime, timezone
    from app.services.event_engine import (
        extract_events_from_conversation,
        _gen_event_id,
        _now_iso,
    )

    # 相似度阈值：0.75 足以捕捉同话题不同措辞
    SIM_THRESHOLD = 0.75

    def _keywords(text: str) -> list[str]:
        """从 summary 中提取 2-4 个有意义的词作为关键词"""
        # 去除常见虚词，取长度 >= 2 的词
        stopwords = {"用户", "今日", "昨日", "上周", "发现", "进行", "已经", "并且",
                     "2026", "年", "月", "日", "的", "了", "在", "于", "将", "并"}
        tokens = re.findall(r'[\u4e00-\u9fff]{2,8}|[A-Za-z0-9]{3,}', text)
        return [t for t in tokens if t not in stopwords][:4]

    def _sqlite_lookup(conn, user_id: str, keywords: list[str]) -> dict | None:
        """用关键词在 SQLite 做 LIKE 预筛，返回最先命中的行"""
        for kw in keywords:
            row = conn.execute(
                "SELECT event_id, mention_count, event_type FROM user_events "
                "WHERE user_id=? AND status='active' AND summary LIKE ?",
                (user_id, f"%{kw}%")
            ).fetchone()
            if row:
                return {"id": row[0], "mention_count": row[1], "event_type": row[2]}
        return None

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_events = extract_events_from_conversation(messages, current_date)
    if not new_events:
        return

    engine = _get_engine()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    now = _now_iso()

    try:
        for ev in new_events:
            summary = ev.get("summary", "").strip()
            if not summary:
                continue

            event_type = ev.get("event_type", "experience")
            keywords = _keywords(summary)

            # ── 第一关：SQLite 关键词预筛 ──
            sqlite_hit = _sqlite_lookup(conn, user_id, keywords)
            if sqlite_hit and sqlite_hit["event_type"] == event_type:
                new_count = sqlite_hit["mention_count"] + 1
                conn.execute(
                    "UPDATE user_events SET mention_count=?, last_referenced_at=?, "
                    "importance=MIN(importance+0.05, 1.0), updated_at=? WHERE event_id=?",
                    (new_count, now, now, sqlite_hit["id"])
                )
                logger.info("[events] sqlite-hit update %s: %s", sqlite_hit["id"], summary[:60])
                continue

            # ── 第二关：Qdrant 向量搜索 ──
            existing = None
            try:
                search_result = engine.client.search(summary, top_k=3, filters={"user_id": user_id})
                hits = search_result.get("results", []) if isinstance(search_result, dict) else []
                for hit in hits:
                    score = float(hit.get("score", 0))
                    if score < SIM_THRESHOLD:
                        break  # 结果已按分数降序排列，后面更低
                    # 用 hit 的 memory 文本反查 SQLite
                    mem_text = hit.get("memory", "")
                    row = conn.execute(
                        "SELECT event_id, mention_count FROM user_events "
                        "WHERE user_id=? AND status='active' AND summary=?",
                        (user_id, mem_text)
                    ).fetchone()
                    if row:
                        existing = {"id": row[0], "score": score, "mention_count": row[1]}
                        break
            except Exception as e:
                logger.warning("[events] qdrant search failed: %s", e)

            if existing:
                new_count = existing["mention_count"] + 1
                conn.execute(
                    "UPDATE user_events SET mention_count=?, last_referenced_at=?, "
                    "importance=MIN(importance+0.05, 1.0), updated_at=? WHERE event_id=?",
                    (new_count, now, now, existing["id"])
                )
                logger.info("[events] qdrant-hit update (score=%.2f) %s: %s",
                            existing["score"], existing["id"], summary[:60])
            else:
                event_id = _gen_event_id()
                conn.execute(
                    "INSERT INTO user_events "
                    "(event_id, user_id, event_type, summary, details_json, related_json, "
                    "occurred_at, detected_at, last_referenced_at, importance, status, mention_count, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, user_id, event_type, summary,
                        json.dumps(ev.get("details", {}), ensure_ascii=False),
                        json.dumps(ev.get("related_entities", []), ensure_ascii=False),
                        ev.get("occurred_at"), now, now,
                        float(ev.get("importance", 0.5)),
                        "active", 1, now, now,
                    )
                )
                logger.info("[events] new event %s: %s", event_id, summary[:60])

        conn.commit()
    except Exception as e:
        logger.error("extract_and_store_events failed: %s", e)
    finally:
        conn.close()


@celery_app.task
def extract_and_update_profile(user_id: str, messages: list):
    """对话结束后，提取结构化用户画像并更新"""
    import sqlite3
    from app.services.profile_engine import (
        extract_facts_from_conversation,
        apply_facts_to_profile,
    )

    facts = extract_facts_from_conversation(messages)
    if not facts:
        return

    session_id = ""
    db_path = _get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # 读取或初始化画像
        cur.execute("SELECT profile_json FROM user_profiles WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        profile = json.loads(row[0]) if row else {}

        # 应用事实，获取审计日志
        audit_logs = apply_facts_to_profile(profile, facts, session_id)

        profile_json = json.dumps(profile, ensure_ascii=False)
        if row:
            cur.execute(
                "UPDATE user_profiles SET profile_json = ?, last_updated = datetime('now') WHERE user_id = ?",
                (profile_json, user_id),
            )
        else:
            cur.execute(
                "INSERT INTO user_profiles (user_id, profile_json, last_updated) VALUES (?, ?, datetime('now'))",
                (user_id, profile_json),
            )

        # 写审计日志（含 action 字段）
        for log in audit_logs:
            cur.execute(
                "INSERT INTO memory_audit_log (user_id, dimension_path, old_value, new_value, action, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (user_id, log["dimension_path"], log["old_value"], log["new_value"],
                 log.get("action", ""), log.get("session_id", "")),
            )

        conn.commit()
        logger.info("Profile updated for user %s: %d facts applied", user_id, len(facts))
    except Exception as e:
        logger.error("extract_and_update_profile failed: %s", e)
    finally:
        conn.close()


@celery_app.task
def decay_all_profiles():
    """每日凌晨衰减所有用户画像中过期字段的置信度"""
    import sqlite3
    from app.services.profile_engine import decay_profile

    db_path = _get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT user_id, profile_json FROM user_profiles")
        rows = cur.fetchall()
        for user_id, profile_json in rows:
            try:
                profile = json.loads(profile_json)
                deleted = decay_profile(profile)
                if deleted:
                    logger.info("Decayed fields for %s: %s", user_id, deleted)
                cur.execute(
                    "UPDATE user_profiles SET profile_json = ?, last_updated = datetime('now') WHERE user_id = ?",
                    (json.dumps(profile, ensure_ascii=False), user_id),
                )
            except Exception as e:
                logger.warning("Decay failed for user %s: %s", user_id, e)
        conn.commit()
    except Exception as e:
        logger.error("decay_all_profiles failed: %s", e)
    finally:
        conn.close()
