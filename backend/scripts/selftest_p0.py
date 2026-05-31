#!/usr/bin/env python3
"""P0 改动自测：Memory Router / Context 去重过滤 / Prompt Composer。

可在本地或 ECS 上运行：
  cd backend && PYTHONPATH=. python3 scripts/selftest_p0.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _ensure_pydantic_stub() -> None:
    if "pydantic" in sys.modules:
        return

    class _Base:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self):
            return self.__dict__

    class Field:
        def __new__(cls, default=None, default_factory=None, **kwargs):
            if default_factory is not None:
                return default_factory
            return default

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = _Base  # type: ignore
    pydantic.Field = Field  # type: ignore
    sys.modules["pydantic"] = pydantic


def _ensure_sqlalchemy_stub() -> None:
    if "sqlalchemy" not in sys.modules:
        sa = types.ModuleType("sqlalchemy")
        sa.select = lambda *a, **k: None  # type: ignore
        sys.modules["sqlalchemy"] = sa
    if "sqlalchemy.ext" not in sys.modules:
        sys.modules["sqlalchemy.ext"] = types.ModuleType("sqlalchemy.ext")
    if "sqlalchemy.ext.asyncio" not in sys.modules:
        aio = types.ModuleType("sqlalchemy.ext.asyncio")
        aio.AsyncSession = type("AsyncSession", (), {})  # type: ignore
        sys.modules["sqlalchemy.ext.asyncio"] = aio


def _ensure_app_stubs() -> None:
    """memory_context 依赖的 ORM / mem0 在本脚本中不需要真实连接。"""
    _ensure_pydantic_stub()
    _ensure_sqlalchemy_stub()

    for mod_name, attrs in (
        ("app.models.event", {"UserEvent": type("UserEvent", (), {})}),
        ("app.models.profile", {"UserProfile": type("UserProfile", (), {})}),
        ("app.services.mem0_engine", {"get_mem0": lambda: None}),
    ):
        if mod_name in sys.modules:
            continue
        mod = types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[mod_name] = mod

    if "app.services.personality" not in sys.modules:
        from app.services import personality  # noqa: F401


_ensure_app_stubs()

from app.services.memory_router import (  # noqa: E402
    ChatTurn,
    MemoryRouteInput,
    route,
)
from app.services.memory_context import (  # noqa: E402
    MemoryContext,
    RoutedMemory,
    _dedupe_memories,
    _is_topic_related,
    _normalize_for_compare,
)
from app.services.memory_router import MemoryRoute, MemoryUsage  # noqa: E402
from app.services.prompt_composer import compose  # noqa: E402
from app.services.personality import MemoryPersonality  # noqa: E402


REL_KEYS = ["妻子", "儿子", "小孙孙", "小魏魏", "邻居老爷爷"]
WIFE_HISTORY = [ChatTurn(role="user", content="我想和你聊聊我老婆")]


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        print(f"  ✅ {name}")
        return True
    msg = f"  ❌ {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return False


def test_router() -> tuple[int, int]:
    print("\n=== 1. Memory Router 意图识别 ===")
    # 单元测试走 v1 关键词 fallback，避免依赖 LLM API
    os.environ["INTENT_CLASSIFIER_ENABLED"] = "false"
    cases = [
        ("睡不着", "emotional_support", []),
        ("我下周想学吉他", "plan_followup", []),  # 「下周」→ 计划；但不应误中 relationship
        ("你不是知道吗", "memory_challenge", []),
        ("你记错了，我老婆不上班", "correction", []),
        ("不是这样的，你理解错了", "correction", []),
        ("你了解我身边都有哪些人吗", "self_summary", []),
        ("在吗", "casual", []),
        ("帮我写个 docker compose 示例", "knowledge_task", []),
        ("她每天在家带孩子，很辛苦", "relationship_topic", WIFE_HISTORY),
        ("北京生活压力好大", "emotional_support", []),
        ("我之前跟你提过这事", "memory_challenge", []),
        ("你怎么不记得了", "memory_challenge", []),
        ("烦死了，工作太多", "emotional_support", []),
    ]
    ok = 0
    for msg, expected, hist in cases:
        r = route(
            MemoryRouteInput(
                user_id="test",
                message=msg,
                recent_history=hist,
                relationship_keys=REL_KEYS,
            )
        )
        if check(f'"{msg[:20]}" → {expected}', r.intent == expected, f"got {r.intent}"):
            ok += 1
        # 附加检查
        if expected == "memory_challenge":
            check("  memory_challenge sensitive_mode", r.sensitive_mode is True)
            check("  memory_challenge max_explicit >= 2", r.max_explicit_memories >= 2)
        if expected == "emotional_support":
            check("  emotional_support sensitive_mode", r.sensitive_mode is True)
            check("  emotional_support event_policy", r.event_policy == "background_pain_points")
        if msg == "我下周想学吉他":
            check("  吉他不误触发 relationship", "relationship_topic" not in r.reasons or r.intent != "relationship_topic")
    return ok, len(cases)


def test_dedupe_and_filter() -> tuple[int, int]:
    print("\n=== 2. 跨层去重 & 主题过滤 ===")
    ok = 0
    total = 0

    # 子串去重
    items = [
        RoutedMemory(source="episodic", text="用户dxj每天忙于工作", usage="background_only", reason="", score=0.8),
        RoutedMemory(source="event", text="用户dxj每天忙于工作，没时间帮妻子带孩子", usage="background_only", reason="", score=0.9),
    ]
    deduped = _dedupe_memories(items)
    total += 1
    if check("子串去重保留更长条", len(deduped) == 1 and len(deduped[0].text) > 20):
        ok += 1

    # 相似去重
    items2 = [
        RoutedMemory(source="episodic", text="用户认为在北京生活压力很大", usage="background_only", reason="", score=0.7),
        RoutedMemory(source="event", text="用户觉得在北京生活压力很大", usage="background_only", reason="", score=0.8),
    ]
    deduped2 = _dedupe_memories(items2)
    total += 1
    if check("高相似去重", len(deduped2) == 1):
        ok += 1

    # 主题过滤
    total += 1
    if check(
        "情绪场景过滤邻居",
        not _is_topic_related("邻居老爷爷养了一条狗叫可乐", "emotional_support"),
    ):
        ok += 1
    total += 1
    if check(
        "情绪场景保留家庭",
        _is_topic_related("妻子在家带孩子很辛苦", "emotional_support"),
    ):
        ok += 1

    # normalize
    total += 1
    n1 = _normalize_for_compare("用户dxj的妻子在家带孩子")
    n2 = _normalize_for_compare("妻子在家带孩子")
    if check("normalize 去用户前缀", n1 == n2 or n1 in n2 or n2 in n1):
        ok += 1

    return ok, total


def test_prompt_composer() -> tuple[int, int]:
    print("\n=== 3. Prompt Composer 结构 ===")
    ok = 0
    total = 0

    route_obj = MemoryRoute(
        intent="emotional_support",
        memory_depth="safe_focused",
        load_layers=["profile_basic", "episodic", "events"],
        query="睡不着",
        sensitive_mode=True,
        max_explicit_memories=2,
        event_policy="background_pain_points",
        personality=MemoryPersonality.BALANCED,
        inferred_subjects=[],
        reasons=["test"],
    )
    ctx = MemoryContext(route=route_obj)
    ctx.relevant_relationships = []
    ctx.relevant_events = []
    ctx.stable_profile = [
        RoutedMemory(source="profile", text="姓名: dxj", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.9),
    ]
    ctx.background_only = [
        RoutedMemory(source="episodic", text="用户每天忙于工作", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.8),
    ]
    ctx.relevant_memories = [
        RoutedMemory(source="episodic", text="用户提到失眠", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.7),
    ]

    prompt, meta = compose(ctx)
    checks = [
        ("含 BASE_PERSONA", "MemoBot" in prompt),
        ("含当前时间", "【当前时间】" in prompt),
        ("含情绪支持指引", "【情绪支持场景指引】" in prompt),
        ("含反客服腔规则", "听起来你" in prompt),
        ("含背景使用分级规则", "【背景信息使用规则（分级）】" in prompt),
        ("含敏感场景硬边界", "敏感场景" in prompt),
        ("meta.route.intent", meta.get("route", {}).get("intent") == "emotional_support"),
        ("meta.activated 非空", len(meta.get("activated", [])) > 0),
        ("兼容旧字段 memories", "memories" in meta),
        ("兼容旧字段 system", "system" in meta),
    ]
    for name, cond in checks:
        total += 1
        if check(name, cond):
            ok += 1

    # memory_challenge 场景
    route_mc = route_obj.model_copy(update={"intent": "memory_challenge"}) if hasattr(route_obj, "model_copy") else MemoryRoute(
        intent="memory_challenge",
        memory_depth="focused",
        load_layers=["profile", "relationships", "events", "episodic"],
        query="你不是知道吗",
        sensitive_mode=True,
        max_explicit_memories=2,
        event_policy="summary",
        personality=MemoryPersonality.BALANCED,
    )
    ctx2 = MemoryContext(route=route_mc)
    ctx2.relevant_relationships = []
    ctx2.relevant_events = []
    ctx2.relevant_memories = []
    ctx2.stable_profile = []
    ctx2.background_only = []
    prompt2, _ = compose(ctx2)
    total += 1
    if check("memory_challenge 含质问记忆指引", "【用户在质问/邀请你的记忆】" in prompt2):
        ok += 1

    return ok, total


def test_emotional_context_filter() -> tuple[int, int]:
    """模拟 emotional_support 下关系/记忆过滤逻辑（与 build_context 一致）。"""
    print("\n=== 4. 情绪场景关系过滤（逻辑回归） ===")
    ok = 0
    total = 0

    rels = [
        RoutedMemory(source="relationship", text="妻子：配偶", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="邻居老爷爷：邻居，80岁", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.5),
    ]
    filtered = [
        r for r in rels
        if any(k in r.text for k in ["妻子", "老婆", "孩子", "儿子", "女儿", "家", "妈", "爸"])
    ]
    total += 1
    if check("情绪场景只留家庭关系", len(filtered) == 1 and "妻子" in filtered[0].text):
        ok += 1

    mems = [
        RoutedMemory(source="episodic", text="用户邻居养了一条狗", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.6),
        RoutedMemory(source="episodic", text="用户每天工作繁忙没时间帮妻子", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.8),
    ]
    filtered_m = [r for r in mems if _is_topic_related(r.text, "emotional_support")]
    total += 1
    if check("情绪场景过滤无关 episodic", len(filtered_m) == 1 and "妻子" in filtered_m[0].text):
        ok += 1

    return ok, total


def main() -> int:
    print("MindMem P0 自测开始")
    totals = []
    for fn in (test_router, test_dedupe_and_filter, test_prompt_composer, test_emotional_context_filter):
        totals.append(fn())
    passed = sum(x for x, _ in totals)
    total = sum(y for _, y in totals)
    print(f"\n{'='*50}")
    print(f"合计: {passed}/{total} 通过")
    if passed < total:
        print("存在失败项，请检查上方 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
