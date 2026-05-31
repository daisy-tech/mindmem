"""Memory Use 自动化评测 runner。

批量评测：对固定 eval persona（persona_a_zhang）记忆库跑 case 集。
单条调试：对当前登录用户真实记忆库跑单条 query。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.chat import (
    CHAT_MODEL,
    ChatMessage,
    DASHSCOPE_BASE_URL,
    _enrich_prompt_meta,
    _prepare_context,
)
from app.services.personality import MemoryPersonality

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
REPORTS_DIR = Path(os.getenv("EVAL_REPORTS_DIR", "/app/data/eval_reports"))
DRAFTS_DIR = Path(os.getenv("EVAL_DRAFTS_DIR", "/app/data/eval_drafts"))

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

# 进程内任务状态（单 worker 够用；多 worker 可改 Redis）
_job_state: dict[str, Any] = {
    "running": False,
    "run_id": None,
    "progress": 0,
    "total": 0,
    "current_case": None,
    "error": None,
}


def get_job_state() -> dict[str, Any]:
    return dict(_job_state)


def prepare_eval_job(run_type: str) -> tuple[str, int]:
    """在 HTTP 请求内同步标记任务开始，便于前端立刻看到进度。"""
    if _job_state.get("running"):
        raise RuntimeError("已有评测任务在运行")

    cases = _load_cases(run_type)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + f"_{run_type}"
    _job_state.update(
        {
            "running": True,
            "run_id": run_id,
            "progress": 0,
            "total": len(cases),
            "current_case": "准备中…",
            "error": None,
        }
    )
    return run_id, len(cases)


def fail_eval_job(error: str) -> None:
    _job_state["error"] = error
    _job_state["running"] = False
    _job_state["current_case"] = None


def _load_suite(run_type: str) -> dict:
    name = "smoke_cases.json" if run_type == "smoke" else "full_cases.json"
    path = EVAL_DIR / name
    if not path.exists() and run_type == "full":
        path = EVAL_DIR / "smoke_cases.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_cases(run_type: str) -> list[dict]:
    return _load_suite(run_type).get("cases", [])


def _activated_text_blob(prompt_meta: dict) -> str:
    parts: list[str] = []
    for m in prompt_meta.get("activated") or []:
        parts.append(str(m.get("text", "")))
    for layer in (prompt_meta.get("context_layers") or {}).values():
        if isinstance(layer, list):
            for m in layer:
                parts.append(str(m.get("text", "")))
    parts.append(prompt_meta.get("system") or "")
    return "\n".join(parts)


def _score_l1(case: dict, prompt_meta: dict, reply: str | None) -> dict:
    expect = case.get("expect") or {}
    route = prompt_meta.get("route") or {}
    actual_intent = route.get("intent", "")
    blob = _activated_text_blob(prompt_meta)

    intent_ok = False
    expected = expect.get("intent")
    optional = expect.get("optional_intents") or []
    if expected:
        intent_ok = actual_intent == expected
    elif optional:
        intent_ok = actual_intent in optional
    else:
        intent_ok = True

    keywords = expect.get("must_activate_keywords") or []
    keyword_hits = sum(1 for k in keywords if k in blob)
    keyword_total = len(keywords)

    forbidden_sys = expect.get("forbidden_phrases_in_system") or []
    sys_violations = [p for p in forbidden_sys if p in (prompt_meta.get("system") or "")]

    reply_text = reply or ""
    forbidden_reply = expect.get("forbidden_phrases_in_reply") or []
    reply_violations = [p for p in forbidden_reply if p in reply_text]
    boundary_violations = [p for p in BOUNDARY_PATTERNS if p in reply_text]

    return {
        "intent_match": intent_ok,
        "expected_intent": expected or optional,
        "actual_intent": actual_intent,
        "intent_confidence": route.get("intent_confidence"),
        "intent_source": route.get("intent_source"),
        "router_version": route.get("router_version"),
        "keyword_hits": keyword_hits,
        "keyword_total": keyword_total,
        "system_violations": sys_violations,
        "reply_violations": reply_violations,
        "boundary_violations": boundary_violations,
        "pass": intent_ok
        and not sys_violations
        and not reply_violations
        and not boundary_violations
        and (keyword_total == 0 or keyword_hits >= max(1, keyword_total // 2)),
    }


def _auto_checks(prompt_meta: dict, reply: str | None, run_chat: bool) -> list[dict]:
    """单条调试默认自动检查项（无需填写 expect）。"""
    reply_text = (reply or "").strip()
    route = prompt_meta.get("route") or {}
    activated = prompt_meta.get("activated") or []

    checks: list[dict] = [
        {
            "id": "route",
            "label": "路由完成",
            "pass": bool(route.get("intent")),
            "detail": (
                f"intent={route.get('intent', '—')}"
                + (f" ({route.get('intent_source')})" if route.get("intent_source") else "")
                + (f" v={route.get('router_version')}" if route.get("router_version") else "")
            ),
        },
        {
            "id": "prompt",
            "label": "Prompt 组装",
            "pass": bool(prompt_meta.get("system")),
            "detail": "system prompt 已生成" if prompt_meta.get("system") else "缺失",
        },
        {
            "id": "llm_messages",
            "label": "LLM messages",
            "pass": bool((prompt_meta.get("llm_request") or {}).get("messages")),
            "detail": f"{len((prompt_meta.get('llm_request') or {}).get('messages') or [])} 条",
        },
        {
            "id": "memory",
            "label": "记忆加载",
            "pass": True,
            "informational": True,
            "detail": f"激活 {len(activated)} 条",
        },
    ]

    boundary = [p for p in BOUNDARY_PATTERNS if p in reply_text]
    if run_chat:
        checks.append(
            {
                "id": "reply",
                "label": "Chat 回复",
                "pass": bool(reply_text) and not reply_text.startswith("[chat error"),
                "detail": f"{len(reply_text)} 字" if reply_text else "无回复",
            }
        )
        checks.append(
            {
                "id": "boundary",
                "label": "边界违规（回复）",
                "pass": len(boundary) == 0,
                "detail": "、".join(boundary) if boundary else "无违规句式",
            }
        )
    else:
        checks.append(
            {
                "id": "boundary",
                "label": "边界违规（回复）",
                "pass": True,
                "informational": True,
                "detail": "未调用 Chat，跳过",
            }
        )

    return checks


def evaluate_query_result(
    prompt_meta: dict,
    reply: str | None,
    run_chat: bool,
    expect: dict | None = None,
) -> dict:
    """汇总单条调试 PASS/FAIL。"""
    auto_checks = _auto_checks(prompt_meta, reply, run_chat)
    hard_auto = [c for c in auto_checks if not c.get("informational")]
    auto_pass = all(c["pass"] for c in hard_auto)

    l1: dict | None = None
    if expect and any(expect.get(k) for k in ("intent", "optional_intents", "must_activate_keywords", "forbidden_phrases_in_reply", "forbidden_phrases_in_system")):
        l1 = _score_l1({"expect": expect}, prompt_meta, reply)

    expect_pass = l1.get("pass", True) if l1 else True
    overall = auto_pass and expect_pass

    return {
        "pass": overall,
        "auto_pass": auto_pass,
        "expect_pass": expect_pass if l1 else None,
        "auto_checks": auto_checks,
        "l1": l1,
    }


async def _run_chat_once(system_prompt: str, messages: list[dict]) -> tuple[str, int]:
    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
    )
    resp = await client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
    return text, getattr(resp.usage, "total_tokens", 0) or 0


async def run_eval_job(
    *,
    eval_user_id: str,
    db: AsyncSession,
    run_type: str = "smoke",
    run_chat: bool = False,
    run_id: str | None = None,
    persona_ref: str | None = None,
    triggered_by_user_id: str | None = None,
) -> str:
    suite = _load_suite(run_type)
    cases = suite.get("cases", [])
    persona_ref = persona_ref or suite.get("persona_ref") or "persona_a_zhang"
    if not run_id:
        if _job_state.get("running"):
            raise RuntimeError("已有评测任务在运行")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + f"_{run_type}"
        _job_state.update(
            {
                "running": True,
                "run_id": run_id,
                "progress": 0,
                "total": len(cases),
                "current_case": None,
                "error": None,
            }
        )
    else:
        _job_state["total"] = len(cases)
        _job_state["error"] = None

    started = datetime.now(timezone.utc)
    case_results: list[dict] = []
    passes = 0

    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        for i, case in enumerate(cases):
            _job_state["progress"] = i
            _job_state["current_case"] = case.get("id")

            history = [ChatMessage(**m) for m in case.get("history") or []]
            message = case["message"]

            p_override: MemoryPersonality | None = None
            if case.get("personality"):
                try:
                    p_override = MemoryPersonality(case["personality"])
                except ValueError:
                    pass

            _, system_prompt, prompt_meta = await _prepare_context(
                eval_user_id,
                message,
                history,
                db,
                personality_override=p_override,
            )
            prompt_meta = _enrich_prompt_meta(
                prompt_meta,
                message=message,
                history=history,
                system_prompt=system_prompt,
            )

            reply: str | None = None
            tokens = 0
            if run_chat:
                llm_req = prompt_meta.get("llm_request") or {}
                msgs = llm_req.get("messages") or []
                try:
                    reply, tokens = await _run_chat_once(system_prompt, msgs)
                except Exception as e:
                    reply = f"[chat error: {e}]"

            l1 = _score_l1(case, prompt_meta, reply)
            if l1.get("pass"):
                passes += 1

            case_results.append(
                {
                    "id": case["id"],
                    "bucket": case.get("bucket"),
                    "personality": case.get("personality"),
                    "message": message,
                    "history": case.get("history") or [],
                    "expect": case.get("expect") or {},
                    "candidate": {
                        "prompt_meta": prompt_meta,
                        "reply": reply,
                        "tokens": tokens,
                        "l1": l1,
                    },
                }
            )

        finished = datetime.now(timezone.utc)
        report = {
            "run_id": run_id,
            "run_type": run_type,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_sec": int((finished - started).total_seconds()),
            "persona_ref": persona_ref,
            "eval_user_id": eval_user_id,
            "triggered_by_user_id": triggered_by_user_id,
            "models": {
                "chat": {"name": CHAT_MODEL, "temperature": 0.7 if run_chat else None},
                "run_chat": run_chat,
            },
            "suite": {
                "total_cases": len(cases),
                "executed_cases": len(case_results),
                "case_ids": [c["id"] for c in cases],
            },
            "verdict": {
                "pass": passes >= len(cases) * 0.6,
                "pass_count": passes,
                "total": len(cases),
                "pass_rate": round(passes / len(cases), 3) if cases else 0,
            },
            "cases": case_results,
        }

        run_dir = REPORTS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(run_dir / "report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        summary_md = _render_summary(report)
        with open(run_dir / "summary.md", "w", encoding="utf-8") as f:
            f.write(summary_md)

        _job_state["progress"] = len(cases)
        return run_id
    except Exception as e:
        logger.exception("eval job failed")
        _job_state["error"] = str(e)
        raise
    finally:
        _job_state["running"] = False
        _job_state["current_case"] = None


def _render_summary(report: dict) -> str:
    v = report.get("verdict") or {}
    lines = [
        f"# MindMem Eval Report `{report.get('run_id')}`",
        "",
        f"- 类型: {report.get('run_type')}",
        f"- 耗时: {report.get('duration_sec')}s",
        f"- L1 通过: {v.get('pass_count')}/{v.get('total')} ({v.get('pass_rate', 0):.0%})",
        f"- 结论: {'PASS' if v.get('pass') else 'FAIL'}",
        "",
        "## Cases",
        "",
    ]
    for c in report.get("cases") or []:
        l1 = (c.get("candidate") or {}).get("l1") or {}
        mark = "✅" if l1.get("pass") else "❌"
        lines.append(
            f"- {mark} **{c.get('id')}** ({c.get('bucket')}) "
            f"intent={l1.get('actual_intent')} "
            f"keywords={l1.get('keyword_hits')}/{l1.get('keyword_total')}"
        )
    return "\n".join(lines) + "\n"


def list_reports() -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    items: list[dict] = []
    for p in sorted(REPORTS_DIR.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        rp = p / "report.json"
        if not rp.exists():
            continue
        try:
            with open(rp, encoding="utf-8") as f:
                r = json.load(f)
            items.append(
                {
                    "run_id": r.get("run_id", p.name),
                    "run_type": r.get("run_type"),
                    "started_at": r.get("started_at"),
                    "duration_sec": r.get("duration_sec"),
                    "verdict": r.get("verdict"),
                    "suite": r.get("suite"),
                }
            )
        except Exception:
            items.append({"run_id": p.name, "error": "invalid report"})
    return items


def load_report(run_id: str) -> dict:
    safe = "".join(c for c in run_id if c.isalnum() or c in "_-")
    path = REPORTS_DIR / safe / "report.json"
    if not path.exists():
        raise FileNotFoundError(run_id)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def run_single_query(
    *,
    user_id: str,
    db: AsyncSession,
    message: str,
    history: list[dict] | None = None,
    personality: str | None = None,
    run_chat: bool = False,
    expect: dict | None = None,
) -> dict:
    """单条 query 评测：route → context → prompt，可选 Chat。"""
    t0 = time.perf_counter()
    hist = [ChatMessage(**m) for m in (history or [])]

    p_override: MemoryPersonality | None = None
    if personality:
        try:
            p_override = MemoryPersonality(personality)
        except ValueError:
            raise ValueError(f"无效人格: {personality}")

    t_ctx = time.perf_counter()
    _, system_prompt, prompt_meta = await _prepare_context(
        user_id,
        message,
        hist,
        db,
        personality_override=p_override,
    )
    ctx_ms = int((time.perf_counter() - t_ctx) * 1000)

    prompt_meta = _enrich_prompt_meta(
        prompt_meta,
        message=message,
        history=hist,
        system_prompt=system_prompt,
    )
    if p_override:
        prompt_meta.setdefault("route", {})["personality"] = p_override.value
        from app.services.personality import PERSONALITY_CONFIG

        cfg = PERSONALITY_CONFIG.get(p_override.value, {})
        prompt_meta["route"]["personality_label"] = cfg.get("label", p_override.value)

    reply: str | None = None
    chat_ms = 0
    tokens = 0
    if run_chat:
        t_chat = time.perf_counter()
        llm_req = prompt_meta.get("llm_request") or {}
        msgs = llm_req.get("messages") or []
        try:
            reply, tokens = await _run_chat_once(system_prompt, msgs)
        except Exception as e:
            reply = f"[chat error: {e}]"
        chat_ms = int((time.perf_counter() - t_chat) * 1000)

    total_ms = int((time.perf_counter() - t0) * 1000)
    evaluation = evaluate_query_result(prompt_meta, reply, run_chat, expect)
    return {
        "prompt_meta": prompt_meta,
        "reply": reply,
        "run_chat": run_chat,
        "personality": personality or prompt_meta.get("route", {}).get("personality"),
        "timing_ms": {
            "total": total_ms,
            "context": ctx_ms,
            "chat": chat_ms,
        },
        "tokens": tokens if run_chat else None,
        "evaluation": evaluation,
        "expect": expect or {},
    }


def _user_drafts_dir(user_id: str) -> Path:
    safe = "".join(c for c in user_id if c.isalnum() or c in "_-")
    return DRAFTS_DIR / safe


def list_drafts(user_id: str) -> list[dict]:
    root = _user_drafts_dir(user_id)
    if not root.exists():
        return []
    items: list[dict] = []
    for p in sorted(root.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            items.append(
                {
                    "draft_id": d.get("draft_id", p.stem),
                    "title": d.get("title", ""),
                    "message": d.get("input", {}).get("message", ""),
                    "personality": d.get("input", {}).get("personality"),
                    "run_chat": d.get("input", {}).get("run_chat", False),
                    "created_at": d.get("created_at"),
                    "has_reply": bool(d.get("result", {}).get("reply")),
                }
            )
        except Exception:
            items.append({"draft_id": p.stem, "title": "(损坏)", "error": True})
    return items


def load_draft(user_id: str, draft_id: str) -> dict:
    safe_id = "".join(c for c in draft_id if c.isalnum() or c in "_-")
    path = _user_drafts_dir(user_id) / f"{safe_id}.json"
    if not path.exists():
        raise FileNotFoundError(draft_id)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_draft(user_id: str, payload: dict) -> dict:
    """保存单条调试草稿（含 input + result）。"""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    root = _user_drafts_dir(user_id)
    root.mkdir(parents=True, exist_ok=True)

    draft_id = payload.get("draft_id") or f"draft_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    draft_id = "".join(c for c in str(draft_id) if c.isalnum() or c in "_-")

    inp = payload.get("input") or {}
    message = (inp.get("message") or "").strip()
    title = (payload.get("title") or "").strip()
    if not title:
        title = message[:40] + ("…" if len(message) > 40 else "")

    doc = {
        "draft_id": draft_id,
        "user_id": user_id,
        "title": title or "未命名草稿",
        "created_at": payload.get("created_at")
        or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "message": inp.get("message", ""),
            "history": inp.get("history") or [],
            "personality": inp.get("personality", "balanced"),
            "run_chat": bool(inp.get("run_chat", False)),
        },
        "result": payload.get("result") or {},
    }

    path = root / f"{draft_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return {"ok": True, "draft_id": draft_id, "title": doc["title"]}


def delete_draft(user_id: str, draft_id: str) -> bool:
    safe_id = "".join(c for c in draft_id if c.isalnum() or c in "_-")
    path = _user_drafts_dir(user_id) / f"{safe_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True
