"""线上真实对话的审计导出。

把一次会话整理成 ``chat_audit_v1`` 包：
- 每轮 user/assistant 配对，附当时的 prompt_meta（路由 / 分层池 / 激活 / system / llm_request）
- 派生字段（不再调 LLM，不重跑 pipeline）：
  · pool_stats / activation_stats / activation_trace / previous_intent
  · consistency_checks（池↔激活↔system 一致性）
- 可选：导出时刻的用户记忆快照（profile/events/episodic）

设计原则：
- 不重跑 Router，不调用任何 LLM；仅基于已存 prompt_meta 与可读 DB
- 旧 turn 无 prompt_meta 时 audit.available=false，仍可导出
- 高度结构化，方便前端/脚本/将来回灌评测实验室
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查时引入，避免离线/纯函数测试时拉重依赖
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.conversation import Conversation

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "chat_audit_v1"

APP_VERSION = os.getenv("APP_VERSION", "0.2.0")
ROUTER_VERSION = os.getenv("ROUTER_VERSION", "v1.5")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen-max")

# 与 eval_runner.BOUNDARY_PATTERNS 同步；这里复制一份避免循环引入
BOUNDARY_PATTERNS = [
    "听起来你",
    "看起来你",
    "我能感受到你",
    "你一定",
    "你肯定",
    "是不是因为",
    "我记得你之前说过",
    "我一直记得",
    "加油",
]

# system prompt 分区标题（与 prompt_composer 对齐）
# v1.2 prompt 瘦身后的新段标题；保留旧标题作为兼容（处理瘦身前导出的老 audit 包）
_SECTION_HEADERS = {
    "stable_bg": "【你已知关于用户的事】",
    "explicit": "【本轮可以提及的具体记忆】",
    "followup": "【可轻问一次的近期事件】",
    "background": "【你大致还记得这些（默认不主动说出）】",
    "usage": "【本轮】",
    "hard": "≪硬边界",
}
_SECTION_HEADERS_LEGACY = {
    "stable_bg": "【稳定背景】",
    "explicit": "【当前可显性引用的记忆】",
    "followup": "【可轻跟进的事件】",
    "background": "【背景信息（默认不主动说出）】",
    "usage": "【本轮使用规则】",
    "hard": "硬边界",
}

_LAYER_KEYS = (
    "stable_profile",
    "relevant_relationships",
    "relevant_events",
    "relevant_memories",
    "background_only",
)


# ============ 工具：规范化、分区解析、ref 生成 ============


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    s = re.sub(r"^\[[a-z_]+(?:\s*@\s*\d{4}-\d{2}-\d{2})?\]\s*", "", text.strip())
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _mem_ref(mem: dict) -> str:
    """给一条记忆生成稳定标识，便于交叉引用。"""
    source = mem.get("source") or "?"
    meta = mem.get("meta") or {}
    if source == "event" and meta.get("event_id"):
        return f"event:{meta['event_id']}"
    if source == "episodic" and meta.get("id"):
        return f"episodic:{meta['id']}"
    if source == "relationship" and meta.get("name"):
        return f"relationship:{meta['name']}"
    snippet = (mem.get("text") or "")[:20]
    return f"{source}:{snippet}"


def _parse_system_sections(system_text: str | None) -> dict[str, str]:
    """按【...】标题切 system prompt，兼容 v1.2 新标题与瘦身前的旧标题。"""
    if not system_text:
        return {}
    sections: dict[str, str] = {}
    keys = ("stable_bg", "explicit", "followup", "background", "usage")
    positions: list[tuple[str, int]] = []
    for key in keys:
        # 先找新标题；找不到再 fallback 旧标题
        idx = system_text.find(_SECTION_HEADERS[key])
        if idx < 0:
            idx = system_text.find(_SECTION_HEADERS_LEGACY[key])
        if idx >= 0:
            positions.append((key, idx))
    positions.sort(key=lambda x: x[1])
    for i, (key, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(system_text)
        sections[key] = system_text[start:end]
    return sections


# ============ 派生字段 ============


def _pool_stats(prompt_meta: dict) -> dict:
    """各层的条数 + 各 usage 数量。"""
    layers = prompt_meta.get("context_layers") or {}
    by_layer: dict[str, int] = {}
    by_usage: dict[str, int] = {}
    for key in _LAYER_KEYS:
        items = layers.get(key) or []
        by_layer[key] = len(items)
        for it in items:
            u = it.get("usage") or "unknown"
            by_usage[u] = by_usage.get(u, 0) + 1
    return {"by_layer": by_layer, "by_usage": by_usage, "total": sum(by_layer.values())}


def _activation_stats(prompt_meta: dict) -> dict:
    activated = prompt_meta.get("activated") or []
    by_usage: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for it in activated:
        u = it.get("usage") or "unknown"
        s = it.get("source") or "unknown"
        by_usage[u] = by_usage.get(u, 0) + 1
        by_source[s] = by_source.get(s, 0) + 1
    return {
        "count": len(activated),
        "by_usage": by_usage,
        "by_source": by_source,
    }


def _activation_trace(prompt_meta: dict) -> dict:
    """activated 与 context_layers 之间的对照（基于 normalized text）。"""
    layers = prompt_meta.get("context_layers") or {}
    activated = prompt_meta.get("activated") or []

    layer_norm: dict[str, list[tuple[str, str]]] = {}
    for key in _LAYER_KEYS:
        layer_norm[key] = [
            (_normalize(it.get("text")), _mem_ref(it)) for it in (layers.get(key) or [])
        ]
    activated_norm = [(_normalize(it.get("text")), _mem_ref(it), it) for it in activated]

    activated_layer_map: dict[str, list[str]] = {}
    in_activated_not_in_any_layer: list[str] = []
    for norm, ref, _ in activated_norm:
        found: list[str] = []
        for key, items in layer_norm.items():
            if any(n == norm and n for n, _ in items):
                found.append(key)
        if found:
            activated_layer_map[ref] = found
        else:
            in_activated_not_in_any_layer.append(ref)

    activated_norm_set = {n for n, _, _ in activated_norm if n}
    in_layer_explicit_not_in_activated: list[str] = []
    for key, items in layer_norm.items():
        if key == "background_only":  # background 区不要求每条都进 activated
            continue
        # 找 usage=explicit_ok 的原始数据
        for orig in layers.get(key) or []:
            if orig.get("usage") != "explicit_ok":
                continue
            if _normalize(orig.get("text")) not in activated_norm_set:
                in_layer_explicit_not_in_activated.append(_mem_ref(orig))

    return {
        "activated_to_layers": activated_layer_map,
        "in_activated_not_in_any_layer": in_activated_not_in_any_layer,
        "in_layer_explicit_not_in_activated": in_layer_explicit_not_in_activated,
    }


def _previous_intent_from_history(history_before: list[dict]) -> str | None:
    for msg in reversed(history_before or []):
        if msg.get("role") != "assistant":
            continue
        meta = msg.get("prompt_meta") or {}
        route = meta.get("route") if isinstance(meta, dict) else None
        if isinstance(route, dict):
            intent = route.get("intent")
            return intent if isinstance(intent, str) and intent else None
        return None
    return None


# ============ 一致性检查 ============


def _check(check_id: str, passed: bool, severity: str, detail: str = "") -> dict:
    return {"id": check_id, "pass": passed, "severity": severity, "detail": detail}


def run_consistency_checks(prompt_meta: dict, reply: str | None) -> list[dict]:
    """对单 turn 的 prompt_meta 跑半自动一致性检查。"""
    out: list[dict] = []
    route = prompt_meta.get("route") or {}
    layers = prompt_meta.get("context_layers") or {}
    activated = prompt_meta.get("activated") or []
    system_text = prompt_meta.get("system") or ""
    intent = route.get("intent") or ""

    # 1. activated 都能在某层找到
    trace = _activation_trace(prompt_meta)
    out.append(
        _check(
            "activated_subset_of_layers",
            not trace["in_activated_not_in_any_layer"],
            "high",
            (
                f"凭空出现的激活：{trace['in_activated_not_in_any_layer']}"
                if trace["in_activated_not_in_any_layer"]
                else ""
            ),
        )
    )

    # 2. explicit 区在 system「显性引用」段
    sections = _parse_system_sections(system_text)
    explicit_section = sections.get("explicit", "")
    background_section = sections.get("background", "")
    explicit_missing: list[str] = []
    for it in activated:
        if it.get("usage") != "explicit_ok":
            continue
        snippet = (it.get("text") or "")[:12]
        if snippet and snippet not in explicit_section:
            explicit_missing.append(_mem_ref(it))
    out.append(
        _check(
            "explicit_in_system_section",
            not explicit_missing,
            "high",
            f"explicit 未出现在显性引用区：{explicit_missing}" if explicit_missing else "",
        )
    )

    # 3. background 区在 system「背景信息」段（仅检查 ctx.background_only 列表）
    bg_missing: list[str] = []
    for it in layers.get("background_only") or []:
        snippet = (it.get("text") or "")[:12]
        if snippet and snippet not in background_section:
            bg_missing.append(_mem_ref(it))
    out.append(
        _check(
            "background_in_system_section",
            not bg_missing,
            "medium",
            f"background 未出现在背景区：{bg_missing}" if bg_missing else "",
        )
    )

    # 4. explicit 数 ≤ route.max_explicit_memories
    cap = int(route.get("max_explicit_memories") or 0)
    explicit_count = sum(1 for it in activated if it.get("usage") == "explicit_ok")
    out.append(
        _check(
            "cap_respected",
            cap <= 0 or explicit_count <= cap,
            "medium",
            f"explicit={explicit_count} 超过 cap={cap}" if explicit_count > cap else "",
        )
    )

    # 5. sensitive 场景下 episodic 不应 explicit_ok
    sensitive = bool(route.get("sensitive_mode"))
    bad_eps: list[str] = []
    if sensitive:
        for it in layers.get("relevant_memories") or []:
            if it.get("usage") == "explicit_ok":
                bad_eps.append(_mem_ref(it))
    out.append(
        _check(
            "sensitive_no_explicit_episodic",
            not bad_eps,
            "high",
            f"敏感场景仍 explicit episodic：{bad_eps}" if bad_eps else "",
        )
    )

    # 6. casual / knowledge_task 时 stable_profile 不应含职业/行业
    career_leak: list[str] = []
    if intent in ("casual", "knowledge_task"):
        for it in layers.get("stable_profile") or []:
            t = it.get("text") or ""
            if t.startswith("职业") or t.startswith("行业"):
                career_leak.append(t)
    out.append(
        _check(
            "casual_no_career_in_profile",
            not career_leak,
            "medium",
            f"闲聊/知识场景注入了职业：{career_leak}" if career_leak else "",
        )
    )

    # 7. 非 casual 时声明的层不应全空
    load_layers = route.get("load_layers") or []
    layers_empty = (
        intent not in ("casual", "knowledge_task")
        and load_layers
        and all(
            not (layers.get(_layer_alias_to_key(name)) or [])
            for name in load_layers
        )
    )
    out.append(
        _check(
            "load_layers_non_empty",
            not layers_empty,
            "medium",
            "intent 非闲聊但所有声明层均为空（漏召回）" if layers_empty else "",
        )
    )

    # 8. 回复含禁词
    if reply:
        hit = [p for p in BOUNDARY_PATTERNS if p in reply]
        out.append(
            _check(
                "reply_boundary",
                not hit,
                "high",
                f"命中禁词：{hit}" if hit else "",
            )
        )
        # 9. 回复疑似复述 background_only（取 background 文本前 8 字作 ngram 简易匹配）
        bg_leak: list[str] = []
        for it in layers.get("background_only") or []:
            t = it.get("text") or ""
            t = re.sub(r"^\[[^\]]+\]\s*", "", t).strip()
            snippet = t[:8]
            if snippet and snippet in reply:
                bg_leak.append(_mem_ref(it))
        out.append(
            _check(
                "reply_no_background_quote",
                not bg_leak,
                "medium",
                f"回复疑似复述背景：{bg_leak}" if bg_leak else "",
            )
        )

    return out


def _layer_alias_to_key(name: str) -> str:
    """route.load_layers 用的别名（profile/relationships/events/episodic/profile_basic/profile_style）
    映射到 context_layers 字段名。
    """
    mapping = {
        "profile": "stable_profile",
        "profile_basic": "stable_profile",
        "profile_style": "stable_profile",
        "relationships": "relevant_relationships",
        "events": "relevant_events",
        "episodic": "relevant_memories",
    }
    return mapping.get(name, name)


# ============ Turn pairing ============


def _pair_turns(messages: list[dict]) -> list[dict]:
    """把扁平 messages 列表按 turn_id 配对为 (user, assistant?)；
    无 turn_id 时按线性顺序配对。
    """
    by_id: dict[str, dict[str, dict]] = {}
    order: list[str] = []
    fallback_pairs: list[dict] = []
    pending_user: dict | None = None

    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        tid = m.get("turn_id")
        if tid:
            if tid not in by_id:
                by_id[tid] = {}
                order.append(tid)
            by_id[tid][m["role"]] = m
        else:
            if m["role"] == "user":
                if pending_user is not None:
                    fallback_pairs.append({"user": pending_user, "assistant": None})
                pending_user = m
            else:  # assistant
                fallback_pairs.append({"user": pending_user, "assistant": m})
                pending_user = None
    if pending_user is not None:
        fallback_pairs.append({"user": pending_user, "assistant": None})

    paired = [{"turn_id": tid, **by_id[tid]} for tid in order]
    paired.extend(
        {
            "turn_id": None,
            **({"user": p["user"]} if p["user"] else {}),
            **({"assistant": p["assistant"]} if p["assistant"] else {}),
        }
        for p in fallback_pairs
    )
    return paired


def _build_turn_node(
    idx: int, pair: dict, history_before: list[dict]
) -> dict:
    user_msg = pair.get("user")
    asst_msg = pair.get("assistant")

    user_text = (user_msg or {}).get("content") or ""
    asst_text = (asst_msg or {}).get("content") or ""
    prompt_meta = (asst_msg or {}).get("prompt_meta")
    has_meta = isinstance(prompt_meta, dict) and prompt_meta

    audit: dict[str, Any] = {
        "available": bool(has_meta),
        "missing_reason": None if has_meta else "no_prompt_meta",
    }
    if has_meta:
        audit["prompt_meta"] = prompt_meta
        audit["derived"] = {
            "pool_stats": _pool_stats(prompt_meta),
            "activation_stats": _activation_stats(prompt_meta),
            "activation_trace": _activation_trace(prompt_meta),
            "previous_intent": _previous_intent_from_history(history_before),
            "consistency_checks": run_consistency_checks(prompt_meta, asst_text or None),
        }

    return {
        "turn_id": pair.get("turn_id"),
        "index": idx,
        "input": {
            "user_message": user_text,
            "history_before": history_before,
            "history_turns": len(history_before),
        },
        "output": {
            "assistant_reply": asst_text,
            "error": (asst_msg or {}).get("error"),
            "ts": (asst_msg or {}).get("ts"),
        },
        "audit": audit,
        "evaluation": {
            "feel": None,
            "reply_reasonable": None,
            "root_cause": [],
            "note": "",
        },
    }


def _summarize_audit(turns: list[dict]) -> dict:
    by_check: dict[str, dict[str, int]] = {}
    high_failed = 0
    turns_with_failure: list[str] = []
    with_audit = 0

    for t in turns:
        if not t["audit"].get("available"):
            continue
        with_audit += 1
        checks = (t["audit"].get("derived") or {}).get("consistency_checks") or []
        failed_here = False
        for c in checks:
            cid = c.get("id")
            slot = by_check.setdefault(cid, {"failed": 0, "total": 0})
            slot["total"] += 1
            if not c.get("pass"):
                slot["failed"] += 1
                if c.get("severity") == "high":
                    high_failed += 1
                failed_here = True
        if failed_here:
            tid = t.get("turn_id") or f"index_{t['index']}"
            turns_with_failure.append(tid)

    return {
        "by_check_id": by_check,
        "high_severity_failed_count": high_failed,
        "turns_with_failures": turns_with_failure,
        "turns_with_audit": with_audit,
    }


# ============ Memory snapshot（可选） ============


async def capture_memory_snapshot(
    user_id: str, db: "AsyncSession"
) -> dict[str, Any]:
    """导出时刻该用户记忆库快照（profile + events + episodic）。"""
    from sqlalchemy import select

    from app.models.event import UserEvent
    from app.models.profile import UserProfile

    snap: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile_json": None,
        "profile_last_updated": None,
        "events": [],
        "episodic": [],
    }

    profile_row = (
        await db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
    ).scalar_one_or_none()
    if profile_row and profile_row.profile_json:
        try:
            snap["profile_json"] = json.loads(profile_row.profile_json)
        except json.JSONDecodeError:
            snap["profile_json"] = profile_row.profile_json
        snap["profile_last_updated"] = (
            profile_row.last_updated.isoformat()
            if profile_row.last_updated
            else None
        )

    events = (
        await db.execute(
            select(UserEvent)
            .where(UserEvent.user_id == user_id)
            .order_by(
                UserEvent.occurred_at.desc(),
                UserEvent.detected_at.desc(),
            )
        )
    ).scalars().all()
    snap["events"] = [
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

    try:
        from app.services.mem0_engine import get_mem0

        data = await asyncio.to_thread(get_mem0().get_all, user_id)
        items = (
            data.get("results", data.get("memories", []))
            if isinstance(data, dict)
            else data
        )
        if isinstance(items, list):
            snap["episodic"] = [
                {
                    "id": it.get("id"),
                    "memory": it.get("memory"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                }
                for it in items
            ]
    except Exception as e:
        snap["episodic_error"] = str(e)

    return snap


# ============ 主入口 ============


async def build_audit_pack(
    conv: "Conversation",
    *,
    include_snapshot: bool = False,
    db: "AsyncSession | None" = None,
) -> dict[str, Any]:
    """构造 chat_audit_v1 导出包。"""
    try:
        messages = json.loads(conv.messages_json or "[]")
        if not isinstance(messages, list):
            messages = []
    except json.JSONDecodeError:
        logger.warning("conversation %s messages_json invalid", conv.id)
        messages = []

    pairs = _pair_turns(messages)

    # 为每个 turn 计算 "history_before"（截至该 turn 之前的全部 user/assistant，
    # 携带 prompt_meta，复用 _previous_intent_from_history）
    flat = [
        m for m in messages if m.get("role") in ("user", "assistant")
    ]

    def _history_before_for(pair: dict) -> list[dict]:
        anchor = pair.get("user") or pair.get("assistant")
        if not anchor:
            return []
        out: list[dict] = []
        for m in flat:
            if m is anchor:
                break
            out.append(
                {
                    "role": m.get("role"),
                    "content": m.get("content", ""),
                    **(
                        {"prompt_meta": m["prompt_meta"]}
                        if m.get("prompt_meta")
                        else {}
                    ),
                }
            )
        return out

    turn_nodes: list[dict] = []
    for idx, pair in enumerate(pairs):
        history_before = _history_before_for(pair)
        turn_nodes.append(_build_turn_node(idx, pair, history_before))

    summary_checks = _summarize_audit(turn_nodes)

    pack: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exporter": "backend_api",
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "user_id": conv.user_id,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "message_count": len(messages),
            "turn_count": len(turn_nodes),
        },
        "environment": {
            "app_version": APP_VERSION,
            "router_version": ROUTER_VERSION,
            "chat_model": CHAT_MODEL,
            "note": "各轮以 turn.audit.prompt_meta.model 为准，本字段仅为导出时配置",
        },
        "memory_snapshot": None,
        "turns": turn_nodes,
        "summary": {
            "turns_total": len(turn_nodes),
            "turns_with_audit": summary_checks["turns_with_audit"],
            "turns_missing_audit": len(turn_nodes) - summary_checks["turns_with_audit"],
            "auto_checks": {
                "by_check_id": summary_checks["by_check_id"],
                "high_severity_failed_count": summary_checks[
                    "high_severity_failed_count"
                ],
                "turns_with_failures": summary_checks["turns_with_failures"],
            },
        },
    }

    if include_snapshot and db is not None:
        try:
            pack["memory_snapshot"] = await capture_memory_snapshot(conv.user_id, db)
        except Exception as e:
            logger.warning("capture memory snapshot failed: %s", e)
            pack["memory_snapshot"] = {"error": str(e)}

    return pack
