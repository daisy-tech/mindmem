"""根据 MemoryRoute 加载/筛选/排序记忆，输出结构化 MemoryContext。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import UserEvent
from app.models.profile import UserProfile
from app.services.mem0_engine import get_mem0
from app.services.memory_router import MemoryRoute, MemoryUsage
from app.services.personality import PERSONALITY_CONFIG

logger = logging.getLogger(__name__)


# ============ 去重与主题过滤工具 ============


# 仅剥「用户dxj(的)」「用户某某的」或事件类型标签；避免把「用户妻子…」整句误当主语
_NOISE_PREFIX_RE = re.compile(
    r"^(?:"
    r"用户[A-Za-z0-9][A-Za-z0-9_-]{0,11}的?|"
    r"用户[\u4e00-\u9fa5]{1,4}的|"
    r"\[[a-z_]+(?:\s*@\s*\d{4}-\d{2}-\d{2})?\]\s*"
    r")"
)


def _normalize_for_compare(text: str) -> str:
    """去掉前缀（"用户xx的"/事件类型标签）和标点后用于子串比较。"""
    s = (text or "").strip()
    m = _NOISE_PREFIX_RE.match(s)
    if m:
        rest = s[m.end():].lstrip(" ,，。：:")
        # 剥完太短说明误伤，保留原文
        if len(rest) >= 4:
            s = rest
    s = re.sub(r"[\s，。；,;:.]+", "", s).lower()
    return s


_SENSITIVE_TOPIC_KEYWORDS = {
    "压力", "工作", "繁忙", "心烦", "失眠", "睡", "焦虑", "累",
    "妻子", "老婆", "孩子", "儿子", "女儿", "家", "带孩子",
    "难受", "烦躁", "心里", "情绪", "崩溃", "委屈",
}


def _is_topic_related(text: str, intent: str) -> bool:
    """对敏感场景过滤：保留与情绪/家庭主题相关的，剔除明显无关项（如邻居宠物等）。"""
    if intent != "emotional_support":
        return True
    return any(k in (text or "") for k in _SENSITIVE_TOPIC_KEYWORDS)


def _dedupe_memories(items: list["RoutedMemory"]) -> list["RoutedMemory"]:
    """跨层去重：
    1. 规范化后子串包含 → 丢短的
    2. 相似度 >=0.85 → 保留更详细的一条
    保持原顺序中的"赢家"。
    """
    if len(items) <= 1:
        return list(items)
    norms = [_normalize_for_compare(x.text) for x in items]
    keep: list[bool] = [True] * len(items)

    for i in range(len(items)):
        if not keep[i] or not norms[i]:
            continue
        for j in range(len(items)):
            if i == j or not keep[j] or not norms[j]:
                continue
            a, b = norms[i], norms[j]
            if not a or not b:
                continue
            # i 是 j 的子串或几乎相同
            contained = a in b or b in a
            similar = (
                SequenceMatcher(None, a, b).ratio() >= 0.82
                if abs(len(a) - len(b)) <= max(len(a), len(b)) * 0.5
                else False
            )
            if contained or similar:
                # 谁短谁丢；同长谁分数低谁丢
                if len(a) < len(b):
                    keep[i] = False
                    break
                if len(a) > len(b):
                    keep[j] = False
                else:
                    if items[i].score < items[j].score:
                        keep[i] = False
                        break
                    keep[j] = False
    return [items[k] for k in range(len(items)) if keep[k]]


class RoutedMemory(BaseModel):
    source: str  # profile / relationship / event / episodic
    text: str
    usage: str
    reason: str
    score: float = 0.0
    meta: dict[str, Any] = Field(default_factory=dict)


class MemoryContext(BaseModel):
    route: MemoryRoute
    stable_profile: list[RoutedMemory] = Field(default_factory=list)
    relevant_relationships: list[RoutedMemory] = Field(default_factory=list)
    relevant_events: list[RoutedMemory] = Field(default_factory=list)
    relevant_memories: list[RoutedMemory] = Field(default_factory=list)
    background_only: list[RoutedMemory] = Field(default_factory=list)


# ============ 工具 ============


def collect_relationship_keys(profile_json: dict) -> list[str]:
    """从画像中提取所有关系人名字，供 Router 使用。"""
    rel_field = (
        profile_json.get("profile", {}).get("social", {}).get("relationships")
    )
    if not isinstance(rel_field, dict):
        return []
    value = rel_field.get("value")
    if not isinstance(value, dict):
        return []
    return [str(k) for k in value.keys() if k]


def profile_summary_text(profile_json: dict) -> str:
    """简化版画像摘要，给 Router 做参考（暂时只取姓名/职业/城市）。"""
    p = profile_json.get("profile", {})
    parts: list[str] = []
    for path in [("basic", "name"), ("career", "job_title"), ("basic", "location")]:
        sec = p.get(path[0], {})
        field = sec.get(path[1])
        if isinstance(field, dict) and field.get("value") is not None:
            parts.append(str(field["value"]))
    return "，".join(parts)


# ============ 稳定画像 ============

STABLE_BASIC_FIELDS = ["name", "nickname", "gender", "birthday", "location", "language"]
STABLE_CAREER_FIELDS = ["job_title", "industry"]

BASIC_LABELS = {
    "name": "姓名",
    "nickname": "昵称",
    "gender": "性别",
    "birthday": "出生年份",
    "location": "所在地",
    "language": "常用语言",
}
CAREER_LABELS = {"job_title": "职业", "industry": "行业"}


def _extract_stable_profile(profile_json: dict, intent: str) -> list[RoutedMemory]:
    out: list[RoutedMemory] = []
    p = profile_json.get("profile", {})

    def _add(section: str, keys: list[str], label_map: dict[str, str]):
        sec = p.get(section, {})
        for k in keys:
            field = sec.get(k)
            if not (isinstance(field, dict) and field.get("value") is not None):
                continue
            conf = float(field.get("confidence", 0.5))
            if conf < 0.5:
                continue
            label = label_map.get(k, k)
            text = f"{label}: {field['value']}"
            out.append(
                RoutedMemory(
                    source="profile",
                    text=text,
                    # 稳定背景默认作为背景，self_summary 时允许显性
                    usage=MemoryUsage.EXPLICIT_OK
                    if intent == "self_summary"
                    else MemoryUsage.BACKGROUND_ONLY,
                    reason="稳定基础信息",
                    score=conf,
                )
            )

    _add("basic", STABLE_BASIC_FIELDS, BASIC_LABELS)
    # casual / knowledge_task：不注入职业/行业，避免闲聊 system 出现公司名等敏感词
    if intent not in ("casual", "knowledge_task"):
        _add("career", STABLE_CAREER_FIELDS, CAREER_LABELS)
    return out


# ============ 社会关系 ============


def _extract_relationships(profile_json: dict, route: MemoryRoute) -> list[RoutedMemory]:
    out: list[RoutedMemory] = []
    rel_field = (
        profile_json.get("profile", {}).get("social", {}).get("relationships")
    )
    if not isinstance(rel_field, dict):
        return out
    value = rel_field.get("value")
    if not isinstance(value, dict):
        return out

    subjects = set(route.inferred_subjects)
    for name, v in value.items():
        if isinstance(v, dict):
            rel = str(v.get("rel") or v.get("relation") or "")
            via = str(v.get("via") or "")
            text = f"{name}（通过 {via}）：{rel}" if via else f"{name}：{rel}"
        else:
            text = f"{name}：{v}"

        usage = MemoryUsage.BACKGROUND_ONLY
        reason = "默认社会关系背景"
        if name in subjects:
            usage = MemoryUsage.EXPLICIT_OK
            reason = "用户当前正在聊该人物"
        elif route.intent == "self_summary":
            usage = MemoryUsage.EXPLICIT_OK
            reason = "用户主动询问关系全貌"

        out.append(
            RoutedMemory(
                source="relationship",
                text=text,
                usage=usage,
                reason=reason,
                score=1.0 if usage == MemoryUsage.EXPLICIT_OK else 0.5,
                meta={"name": name},
            )
        )
    return out


# ============ 事件 ============


def _select_events_by_policy(
    rows: list[UserEvent], route: MemoryRoute
) -> list[UserEvent]:
    policy = route.event_policy
    out: list[UserEvent] = []
    for r in rows:
        if r.status != "active":
            continue
        if policy == "none":
            continue
        if policy == "background_pain_points":
            if r.event_type in {"pain_point", "feedback"}:
                out.append(r)
            continue
        if policy == "track_or_follow_up":
            if r.event_type in {"plan", "status_change"}:
                out.append(r)
            continue
        if policy == "related_only":
            if r.event_type in {"plan", "status_change", "achievement"}:
                out.append(r)
            continue
        if policy == "summary":
            if (r.importance or 0) >= 0.5:
                out.append(r)
            continue
    return out


def _event_usage(
    event: UserEvent, route: MemoryRoute, personality_cfg: dict
) -> tuple[str, str]:
    pp = personality_cfg.get("pain_point_policy", "triggered_only")
    plan_followup = personality_cfg.get("plan_followup", "once")

    if event.event_type == "pain_point":
        if pp == "background_only":
            return MemoryUsage.BACKGROUND_ONLY, "痛点：内向人格，仅作背景"
        if pp == "triggered_only" and route.intent in {
            "emotional_support",
            "relationship_topic",
        }:
            return MemoryUsage.BACKGROUND_ONLY, "痛点：相关话题触发，仅作背景"
        if pp == "soft_triggered" and route.intent in {
            "emotional_support",
            "relationship_topic",
        }:
            return MemoryUsage.BACKGROUND_ONLY, "痛点：外向型温柔承接，但不主动展开"
        return MemoryUsage.AVOID_UNLESS_ASKED, "痛点：本场景不使用"

    if event.event_type == "plan":
        if plan_followup == "asked_only":
            return MemoryUsage.AVOID_UNLESS_ASKED, "计划：内向人格仅在被问起时使用"
        if route.intent == "plan_followup":
            return MemoryUsage.EXPLICIT_OK, "计划话题：直接讨论"
        if plan_followup in {"once", "active_once"}:
            return MemoryUsage.FOLLOW_UP_ONCE, "计划：可主动跟进一次"
        return MemoryUsage.BACKGROUND_ONLY, "计划：作为背景"

    if event.event_type == "status_change":
        return MemoryUsage.EXPLICIT_OK, "近期状态变化"

    if event.event_type == "achievement":
        return MemoryUsage.EXPLICIT_OK, "成就：相关时可祝贺/延续"

    if event.event_type == "feedback":
        return MemoryUsage.BACKGROUND_ONLY, "反馈：影响回复风格，不主动提"

    return MemoryUsage.BACKGROUND_ONLY, "一般经历，作为背景"


def _score_event(event: UserEvent, route: MemoryRoute) -> float:
    score = float(event.importance or 0) * 0.4
    try:
        detected = datetime.fromisoformat(
            (event.detected_at or "").replace("Z", "+00:00")
        )
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - detected).days
        recency = max(0.0, 1.0 - days / 60.0)
    except Exception:
        recency = 0.5
    score += recency * 0.3
    if route.intent == "plan_followup" and event.event_type == "plan":
        score += 0.2
    if route.intent == "emotional_support" and event.event_type in {
        "pain_point",
        "feedback",
    }:
        score += 0.15
    return score


# ============ 情节记忆 ============


def _search_episodic(user_id: str, query: str, limit: int) -> list[dict]:
    if not query or limit <= 0:
        return []
    try:
        results = get_mem0().search(query, user_id=user_id, limit=limit)
        return results.get("results", []) or []
    except Exception as e:
        logger.warning("mem0 search failed: %s", e)
        return []


# ============ 主入口 ============


async def build_context(
    route: MemoryRoute,
    user_id: str,
    db: AsyncSession,
    profile_json: dict | None = None,
) -> MemoryContext:
    """根据 route 加载记忆、打分、标 usage，返回 MemoryContext。"""
    cfg = PERSONALITY_CONFIG[route.personality.value]
    ctx = MemoryContext(route=route)

    if profile_json is None:
        row = (
            await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        profile_json = json.loads(row.profile_json) if row else {}

    # 1) 稳定画像
    ctx.stable_profile = _extract_stable_profile(profile_json, route.intent)

    # 2) 社会关系
    if "relationships" in route.load_layers or route.intent == "self_summary":
        ctx.relevant_relationships = _extract_relationships(profile_json, route)

    # 3) 事件
    if "events" in route.load_layers and route.event_policy != "none":
        result = await db.execute(
            select(UserEvent)
            .where(UserEvent.user_id == user_id, UserEvent.status == "active")
            .order_by(UserEvent.importance.desc())
            .limit(30)
        )
        candidates = _select_events_by_policy(list(result.scalars().all()), route)
        scored: list[RoutedMemory] = []
        for e in candidates:
            usage, reason = _event_usage(e, route, cfg)
            if usage == MemoryUsage.AVOID_UNLESS_ASKED:
                continue
            score = _score_event(e, route)
            label_date = f" @ {e.occurred_at}" if e.occurred_at else ""
            text = f"[{e.event_type}{label_date}] {e.summary}"
            scored.append(
                RoutedMemory(
                    source="event",
                    text=text,
                    usage=usage,
                    reason=reason,
                    score=score,
                    meta={
                        "event_id": e.event_id,
                        "event_type": e.event_type,
                        "occurred_at": e.occurred_at,
                        "importance": e.importance,
                    },
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        ctx.relevant_events = scored[:3]

    # 4) 情节记忆
    if "episodic" in route.load_layers:
        if route.intent == "self_summary":
            limit = 6
        elif route.intent == "relationship_topic":
            limit = 5
        else:
            limit = 4
        raw = _search_episodic(user_id, route.query, limit)
        scored_eps: list[RoutedMemory] = []
        for r in raw:
            text = (r.get("memory") or "").strip()
            if not text:
                continue
            score = float(r.get("score", 0.0) or 0.0)
            usage = MemoryUsage.EXPLICIT_OK
            reason = "情节记忆语义匹配"
            if route.sensitive_mode:
                usage = MemoryUsage.BACKGROUND_ONLY
                reason = "敏感场景：情节记忆仅作背景"
            scored_eps.append(
                RoutedMemory(
                    source="episodic",
                    text=text,
                    usage=usage,
                    reason=reason,
                    score=score,
                    meta={"id": r.get("id")},
                )
            )
        ctx.relevant_memories = scored_eps

    # 5) 跨层去重 + 敏感场景主题过滤
    #    事件层和情节记忆层经常对同一事实有不同表述，必须去重
    ctx.relevant_events = _dedupe_memories(ctx.relevant_events)
    ctx.relevant_memories = _dedupe_memories(ctx.relevant_memories)
    # 跨层去重：事件 vs 情节记忆，避免两边各自说同一件事
    combined = _dedupe_memories([*ctx.relevant_events, *ctx.relevant_memories])
    combined_ids = {id(x) for x in combined}
    ctx.relevant_events = [x for x in ctx.relevant_events if id(x) in combined_ids]
    ctx.relevant_memories = [x for x in ctx.relevant_memories if id(x) in combined_ids]

    # 敏感场景下，剔除明显与情绪/家庭无关的项（如"邻居养狗"）
    if route.intent == "emotional_support":
        ctx.relevant_events = [
            r for r in ctx.relevant_events if _is_topic_related(r.text, route.intent)
        ]
        ctx.relevant_memories = [
            r for r in ctx.relevant_memories if _is_topic_related(r.text, route.intent)
        ]
        # 关系也一样：emotional_support 时不要列邻居老爷爷之类的非家庭关系
        ctx.relevant_relationships = [
            r
            for r in ctx.relevant_relationships
            if any(
                k in r.text
                for k in ["妻子", "老婆", "孩子", "儿子", "女儿", "家", "妈", "爸"]
            )
        ]

    # 6) 显性记忆上限由人格控制（超过部分降级为 background_only）
    _cap_explicit(
        [
            *ctx.relevant_relationships,
            *ctx.relevant_events,
            *ctx.relevant_memories,
        ],
        route.max_explicit_memories,
    )

    # 7) 汇总 background_only（含再一次跨层去重，避免在不同 group 中又重复）
    bg_all: list[RoutedMemory] = []
    for group in (
        ctx.relevant_relationships,
        ctx.relevant_events,
        ctx.relevant_memories,
    ):
        for r in group:
            if r.usage == MemoryUsage.BACKGROUND_ONLY:
                bg_all.append(r)
    bg_dedup = _dedupe_memories(bg_all)
    # 背景信息最多 3 条，避免 prompt 过长
    ctx.background_only = bg_dedup[:3]

    return ctx


def _cap_explicit(items: list[RoutedMemory], cap: int) -> None:
    """把超过 cap 数量的 explicit_ok 记忆降级为 background_only。
    items 中元素与 ctx 子列表共享同一个对象，原地修改即可。
    """
    if cap < 0:
        cap = 0
    explicit = [x for x in items if x.usage == MemoryUsage.EXPLICIT_OK]
    explicit.sort(key=lambda r: r.score, reverse=True)
    keep_ids = {id(x) for x in explicit[:cap]}
    for x in items:
        if x.usage == MemoryUsage.EXPLICIT_OK and id(x) not in keep_ids:
            x.usage = MemoryUsage.BACKGROUND_ONLY
            x.reason = (x.reason or "") + "（超过人格显性上限，降级）"
