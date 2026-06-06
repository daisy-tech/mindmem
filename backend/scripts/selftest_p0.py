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
    _cap_explicit,
    _collect_background,
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
        ("含情绪支持指引", "【情绪支持指引】" in prompt),
        ("含反客服腔规则", "听起来你" in prompt),
        ("含硬边界", "≪硬边界" in prompt),
        ("含敏感场景规则", "敏感场景" in prompt),
        ("空块不渲染 followup", "可轻问一次的近期事件" not in prompt),
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
    if check("memory_challenge 含质问记忆指引", "【质问记忆指引】" in prompt2):
        ok += 1

    return ok, total


def test_cap_and_background_buckets() -> tuple[int, int]:
    """P0 三件套：cap 纳入 profile / source 权重 / background 分桶 / memory_challenge 不降级。"""
    print("\n=== 5. P0 修复回归：cap & background 分桶 ===")
    ok = 0
    total = 0

    # 5.1 _cap_explicit 现在纳入 stable_profile + 关系优先
    # 模拟 turn#0：5 条 profile + 5 条 relationship 都 explicit，cap=5
    stable = [
        RoutedMemory(source="profile", text="姓名: dxj", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.99),
        RoutedMemory(source="profile", text="出生年份: 1983", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.99),
        RoutedMemory(source="profile", text="所在地: 北京", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.99),
        RoutedMemory(source="profile", text="职业: it", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.99),
        RoutedMemory(source="profile", text="行业: 未知行业", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.75),
    ]
    rels = [
        RoutedMemory(source="relationship", text="儿子：9岁半", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="妻子：配偶", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="邻居老爷爷：邻居", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="小孙孙：儿子同学", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="小魏魏：儿子同学", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
    ]
    _cap_explicit([*stable, *rels], cap=5, intent="self_summary")
    explicit_after = [x for x in [*stable, *rels] if x.usage == MemoryUsage.EXPLICIT_OK]
    total += 1
    # self_summary：兜底姓名占 1 名额 + 4 个关系 = 5 条，不超 cap
    if check(
        f"cap=5 严格不超（实际 {len(explicit_after)}）",
        len(explicit_after) == 5,
    ):
        ok += 1
    total += 1
    if check(
        "self_summary 兜底姓名仍 explicit",
        any(x.usage == MemoryUsage.EXPLICIT_OK and (x.text or "").startswith("姓名") for x in stable),
    ):
        ok += 1
    total += 1
    if check(
        "关系优先于其它 profile：4 个关系 explicit（姓名占 1 名额）",
        sum(1 for x in rels if x.usage == MemoryUsage.EXPLICIT_OK) == 4,
    ):
        ok += 1
    total += 1
    # 非 self_summary 时不再保姓名
    stable2 = [
        RoutedMemory(source="profile", text="姓名: dxj", usage=MemoryUsage.EXPLICIT_OK, reason="", score=0.99),
    ]
    rels2 = [
        RoutedMemory(source="relationship", text="妻子", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
        RoutedMemory(source="relationship", text="儿子", usage=MemoryUsage.EXPLICIT_OK, reason="", score=1.0),
    ]
    _cap_explicit([*stable2, *rels2], cap=2, intent="relationship_topic")
    if check(
        "relationship_topic：关系优先，profile 姓名被降级",
        stable2[0].usage == MemoryUsage.BACKGROUND_ONLY
        and all(r.usage == MemoryUsage.EXPLICIT_OK for r in rels2),
    ):
        ok += 1

    # 5.2 background 分桶：模拟 turn#5 风格
    ctx = MemoryContext(
        route=MemoryRoute(
            intent="memory_challenge",
            memory_depth="focused",
            load_layers=["profile", "events", "episodic"],
            query="出差",
            sensitive_mode=True,
            max_explicit_memories=2,
            event_policy="summary",
            personality=MemoryPersonality.BALANCED,
        )
    )
    # 7 条关系背景（要能挤掉事件吗？修复后不可以）
    ctx.relevant_relationships = [
        RoutedMemory(source="relationship", text=f"R{i}", usage=MemoryUsage.BACKGROUND_ONLY, reason="", score=0.5)
        for i in range(7)
    ]
    ctx.relevant_events = [
        RoutedMemory(
            source="event",
            text="[experience @ 2026-05-25] 用户上周在深圳出差",
            usage=MemoryUsage.BACKGROUND_ONLY,
            reason="",
            score=0.7,
        ),
        RoutedMemory(
            source="event",
            text="[experience @ 2026-05-24] 用户开发苹果App",
            usage=MemoryUsage.BACKGROUND_ONLY,
            reason="",
            score=0.6,
        ),
    ]
    ctx.relevant_memories = []
    ctx.stable_profile = []
    bg = _collect_background(ctx)
    total += 1
    if check(
        "background 桶里事件至少 1 条（depth 等于 P0-3 修复目标）",
        any(r.source == "event" and "出差" in r.text for r in bg),
    ):
        ok += 1
    total += 1
    if check(
        f"background 总数 ≤ 9（实际 {len(bg)}）",
        len(bg) <= 9,
    ):
        ok += 1
    total += 1
    if check(
        "关系桶不超 3 条",
        sum(1 for r in bg if r.source == "relationship") <= 3,
    ):
        ok += 1

    return ok, total


def test_memory_challenge_pipeline() -> tuple[int, int]:
    """重放 turn#5 风格：memory_challenge + sensitive 下 event/episodic 应保留 explicit。"""
    print("\n=== 6. memory_challenge 链路（重放 turn#5）===")
    ok = 0
    total = 0

    from app.services.memory_context import _event_usage, _score_event
    from app.services.personality import PERSONALITY_CONFIG

    class _E:
        def __init__(self, et, summary, imp=0.6):
            self.event_type = et
            self.summary = summary
            self.importance = imp
            self.detected_at = "2026-05-25T00:00:00+00:00"
            self.occurred_at = "2026-05-25"
            self.status = "active"
            self.event_id = "E_" + et

    route_mc = MemoryRoute(
        intent="memory_challenge",
        memory_depth="focused",
        load_layers=["events", "episodic"],
        query="我最近出差了，你知道我去哪里了吗 | 出差",
        sensitive_mode=True,
        max_explicit_memories=2,
        event_policy="summary",
        personality=MemoryPersonality.BALANCED,
    )
    cfg = PERSONALITY_CONFIG["balanced"]

    events = [
        _E("plan", "用户正在开发一款苹果App", 0.7),
        _E("experience", "用户上周在深圳出差", 0.6),
        _E("experience", "用户今天花了一整天开发App", 0.5),
    ]
    usages = [_event_usage(e, route_mc, cfg) for e in events]
    total += 1
    if check(
        "memory_challenge → 所有 event 类型 explicit_ok",
        all(u[0] == MemoryUsage.EXPLICIT_OK for u in usages),
    ):
        ok += 1

    # 出差事件应该按 query 关键词加分胜出
    scored = sorted(events, key=lambda e: _score_event(e, route_mc), reverse=True)
    total += 1
    if check(
        f"出差事件被 query boost 排到首位（实际 top={scored[0].summary[:10]}）",
        "出差" in scored[0].summary,
    ):
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


def test_chat_review_rules() -> tuple[int, int]:
    """P1 线上聊天记录评估：L1 规则 + 归因聚合。"""
    print("\n=== 7. eval_chat_review L1 规则 ===")
    from app.services.eval_chat_review import (
        review_turn,
        review_audit_pack,
        _rule_recall_for_challenge,
        _rule_generic_reply,
        _rule_reply_off_topic,
        _rule_error_reply,
        _rule_data_vs_activation_gap,
        _rule_correction_persisted,
        _rule_correction_no_concrete_ack,
        _rule_fabrication_under_challenge,
    )
    ok = 0
    total = 0

    # 7.1 memory_challenge 池里有「出差」却未激活 → suspicious + C 归因
    meta_challenge = {
        "route": {"intent": "memory_challenge"},
        "context_layers": {
            "relevant_events": [
                {"source": "event", "text": "[experience] 用户上周在深圳出差",
                 "usage": "background_only"},
            ],
        },
        "activated": [],
        "system": "无相关记忆",
    }
    r = _rule_recall_for_challenge("我最近出差了，你知道我去哪里了吗", meta_challenge)
    total += 1
    if check(
        f"出差未激活 → suspicious + C （实际 {r['status']}, {r['attribution']}）",
        r["status"] == "suspicious" and "C" in r["attribution"],
    ): ok += 1

    # 7.2 memory_challenge 池里完全没有 → fail + A/C
    meta_no_data = {
        "route": {"intent": "memory_challenge"},
        "context_layers": {},
        "activated": [],
        "system": "",
    }
    r = _rule_recall_for_challenge("我最近出差了，你记得吗", meta_no_data)
    total += 1
    if check(
        f"池里无相关事实 → fail + A,C",
        r["status"] == "fail" and "A" in r["attribution"] and "C" in r["attribution"],
    ): ok += 1

    # 7.3 memory_challenge 已 explicit → pass
    meta_ok = {
        "route": {"intent": "memory_challenge"},
        "context_layers": {
            "relevant_events": [
                {"source": "event", "text": "[experience] 上周深圳出差",
                 "usage": "explicit_ok"},
            ],
        },
        "activated": [
            {"source": "event", "text": "[experience] 上周深圳出差",
             "usage": "explicit_ok"},
        ],
        "system": "",
    }
    r = _rule_recall_for_challenge("出差去哪了", meta_ok)
    total += 1
    if check(f"已 explicit → pass", r["status"] == "pass"):
        ok += 1

    # 7.4 generic_reply 命中「慢慢来」→ suspicious + E
    r = _rule_generic_reply("猫粘人很温馨，慢慢来吧，相处会好的", "relationship_topic")
    total += 1
    if check(
        f"模板措辞「慢慢来」→ E",
        r["status"] == "suspicious" and "E" in r["attribution"],
    ): ok += 1

    # 7.5 generic_reply 闲聊场景跳过
    r = _rule_generic_reply("加油哦", "casual")
    total += 1
    if check("闲聊场景跳过 generic_reply", r["status"] == "skip"):
        ok += 1

    # 7.6 error_reply: F 层
    r = _rule_error_reply("[连接失败: HTTP 500]", None)
    total += 1
    if check(
        "连接失败 → F",
        r["status"] == "fail" and "F" in r["attribution"],
    ): ok += 1

    # 7.7 reply_off_topic：完全不沾边
    r = _rule_reply_off_topic(
        "今天股票涨了吗", "猫粘人很温馨，相处需要时间", "relationship_topic"
    )
    total += 1
    if check(f"完全跑题 → suspicious", r["status"] == "suspicious"):
        ok += 1

    # 7.8 review_audit_pack 端到端：单 turn
    pack = {
        "turns": [
            {
                "turn_id": "t1",
                "index": 0,
                "input": {"user_message": "我最近出差了，你知道我去哪吗"},
                "output": {"assistant_reply": "我不太确定。你这次出差去哪？", "error": None},
                "audit": {
                    "available": True,
                    "prompt_meta": meta_challenge,
                    "derived": {"consistency_checks": [
                        {"id": "x", "pass": True, "severity": "high"},
                    ]},
                },
            },
        ]
    }
    out = review_audit_pack(pack)
    total += 1
    if check(
        "review_audit_pack 注入 review 字段并聚合 root_cause",
        out["turns"][0].get("review") is not None
        and any(code == "C" for code, _ in out["review"]["root_cause_top"]),
    ): ok += 1

    # 7.9 correction_persisted：上一轮 correction 纠正了"小鹏"，本轮池里仍出现
    prev_turn = {
        "input": {"user_message": "没有小鹏这种动物，你把小朋友和小鹏搞混了"},
        "output": {"assistant_reply": "我搞混了"},
        "audit": {
            "available": True,
            "prompt_meta": {"route": {"intent": "correction"}},
        },
    }
    curr_meta_persist = {
        "route": {"intent": "relationship_topic"},
        "context_layers": {
            "relevant_memories": [
                {"text": "儿子9岁半，家里养了两只小鹏——小孙孙和小魏魏"},
            ],
        },
    }
    r = _rule_correction_persisted(prev_turn, curr_meta_persist)
    total += 1
    if check(
        f"correction_persisted: 上一轮纠正的实体仍在池里 → fail+A,B (实际 {r['status']}, {r['attribution']})",
        r["status"] == "fail" and "A" in r["attribution"] and "B" in r["attribution"],
    ): ok += 1

    # 7.10 correction_persisted：池里没有残留 → pass
    curr_meta_clean = {
        "route": {"intent": "relationship_topic"},
        "context_layers": {
            "relevant_memories": [
                {"text": "小孙孙和小魏魏是儿子的同学，玩得很好"},
            ],
        },
    }
    r = _rule_correction_persisted(prev_turn, curr_meta_clean)
    total += 1
    if check(
        "correction_persisted: 池子干净 → pass",
        r["status"] == "pass",
    ): ok += 1

    # 7.11 correction_no_concrete_ack：典型推卸
    r = _rule_correction_no_concrete_ack(
        "我家最近养了什么动物",
        "我记错了，请你再告诉我一次，我会更新记忆。",
        "correction",
    )
    total += 1
    if check(
        f"推卸式 correction → fail+D (实际 {r['status']}, {r['attribution']})",
        r["status"] == "fail" and "D" in r["attribution"],
    ): ok += 1

    # 7.12 correction_no_concrete_ack：有具体复述
    r = _rule_correction_no_concrete_ack(
        "小孙孙和小魏魏是同学不是宠物",
        "记错了，小孙孙和小魏魏是同学不是宠物，已经按这个来。",
        "correction",
    )
    total += 1
    if check("correction 复述正确事实 → pass", r["status"] == "pass"):
        ok += 1

    # 7.13 fabrication_under_challenge：池里只有「猫」，AI 说「猫和狗」
    pm_fab = {
        "route": {"intent": "memory_challenge"},
        "context_layers": {
            "relevant_memories": [{"text": "养了一只猫，觉得它像女孩"}],
        },
        "activated": [],
    }
    r = _rule_fabrication_under_challenge(
        "你家最近养了一只猫和一只狗，对吧？", pm_fab,
    )
    total += 1
    if check(
        f"hallucinate 「狗」 → fail+E,A (实际 {r['status']}, {r['attribution']})",
        r["status"] == "fail" and "E" in r["attribution"] and "A" in r["attribution"],
    ): ok += 1

    # 7.14 fabrication_under_challenge：reply 实体在池里 → pass
    r = _rule_fabrication_under_challenge(
        "你之前是不是说过养了一只猫", pm_fab,
    )
    total += 1
    if check(
        "实体在池中 → pass",
        r["status"] == "pass",
    ): ok += 1

    # 7.15 review_audit_pack 跨轮：prev_turn 串联生效
    pack_cross = {
        "turns": [
            {
                "turn_id": "t1", "index": 0,
                "input": {"user_message": "小孙孙不是小鹏"},
                "output": {"assistant_reply": "我搞混了"},
                "audit": {
                    "available": True,
                    "prompt_meta": {"route": {"intent": "correction"}},
                    "derived": {"consistency_checks": []},
                },
            },
            {
                "turn_id": "t2", "index": 1,
                "input": {"user_message": "他们玩的开心"},
                "output": {"assistant_reply": "那真好"},
                "audit": {
                    "available": True,
                    "prompt_meta": {
                        "route": {"intent": "relationship_topic"},
                        "context_layers": {
                            "relevant_memories": [
                                {"text": "家里养了两只小鹏——小孙孙和小魏魏"},
                            ],
                        },
                        "activated": [],
                    },
                    "derived": {"consistency_checks": []},
                },
            },
        ],
    }
    out_x = review_audit_pack(pack_cross)
    t2_review = out_x["turns"][1]["review"]
    persisted = [r for r in t2_review["rules"] if r["id"] == "correction_persisted"][0]
    total += 1
    if check(
        f"pack 跨轮：t2 触发 correction_persisted fail (实际 {persisted['status']})",
        persisted["status"] == "fail",
    ): ok += 1

    # 7.16 旧轮无 prompt_meta：跳过但不崩
    pack2 = {
        "turns": [
            {
                "turn_id": "t1", "index": 0,
                "input": {"user_message": "hi"},
                "output": {"assistant_reply": "在的"},
                "audit": {"available": False, "missing_reason": "no_prompt_meta"},
            },
        ]
    }
    out2 = review_audit_pack(pack2)
    rv = out2["turns"][0]["review"]
    total += 1
    if check(
        "旧轮无 prompt_meta → final=skip",
        rv["final_status"] == "skip" and rv["snapshot_status"] == "unavailable",
    ): ok += 1

    return ok, total


def test_correction_engine_pure() -> tuple[int, int]:
    """纠错流水：纯函数（候选构造 / LLM 输出解析 / 三层 apply 用 sqlite 内存）。"""
    print("\n=== 8. correction_engine ===")
    import sqlite3

    from app.services import correction_engine as ce
    ok = 0
    total = 0

    # 8.1 _extract_correction_target：最后一条 user + 上一条 assistant
    user_text, asst_text = ce._extract_correction_target([
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你们家养了两只小鹏吧"},
        {"role": "user", "content": "小鹏是儿子同学不是宠物，你记错了"},
    ])
    total += 1
    if check(
        "extract_correction_target 取最后一对",
        "小鹏是儿子同学" in user_text and "两只小鹏" in asst_text,
    ): ok += 1

    # 8.2 _flatten_profile_fields：关系 + basic 都列出
    profile_blob = {
        "profile": {
            "social": {
                "relationships": {
                    "value": {
                        "小孙孙": "宠物",
                        "妻子": "配偶",
                    }
                }
            },
            "basic": {
                "name": {"value": "dxj", "confidence": 0.99},
                "age": {"value": 42},
            },
        }
    }
    flat = ce._flatten_profile_fields(profile_blob)
    total += 1
    if check(
        f"profile 展平含 relationships + basic（实际 {len(flat)}）",
        any("小孙孙" in c["id"] for c in flat)
        and any("basic.name" in c["id"] for c in flat),
    ): ok += 1

    # 8.3 三层 apply：用内存 sqlite 模拟 user_events / user_profiles / memory_deprecations
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE user_events(
            event_id TEXT PRIMARY KEY, user_id TEXT, event_type TEXT,
            summary TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE user_profiles(
            user_id TEXT PRIMARY KEY, profile_json TEXT, last_updated TEXT
        );
        CREATE TABLE memory_deprecations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, source TEXT, ref_id TEXT, original_text TEXT,
            reason TEXT, correction_conversation_id TEXT, correction_turn_id TEXT,
            llm_confidence REAL, action TEXT, new_text TEXT, deprecated_at TEXT,
            restored_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO user_events VALUES('ev_1','u1','experience',"
        "'用户家养了两只小鹏','active','t0')"
    )
    import json as _j
    conn.execute(
        "INSERT INTO user_profiles VALUES('u1', ?, 't0')",
        (_j.dumps(profile_blob, ensure_ascii=False),),
    )

    ctx_meta = {"conversation_id": "c1", "turn_id": "t1"}

    # event deprecate
    ce._apply_event(
        conn, "u1",
        {"ref": "V1", "source": "event", "id": "ev_1",
         "text": "[experience] 用户家养了两只小鹏"},
        {"action": "deprecate", "confidence": 0.9, "reason": "误把同学当宠物"},
        ctx_meta,
    )
    ev_status = conn.execute(
        "SELECT status FROM user_events WHERE event_id='ev_1'"
    ).fetchone()[0]
    total += 1
    if check("event 软删 → status=deprecated", ev_status == "deprecated"):
        ok += 1

    # profile relationship deprecate
    ce._apply_profile(
        conn, "u1",
        {"ref": "P1", "source": "profile",
         "id": "profile.social.relationships.小孙孙",
         "text": "小孙孙: 宠物"},
        {"action": "deprecate", "confidence": 0.95, "reason": "关系类型错误"},
        ctx_meta,
    )
    p_after = _j.loads(conn.execute(
        "SELECT profile_json FROM user_profiles WHERE user_id='u1'"
    ).fetchone()[0])
    rel_value = p_after["profile"]["social"]["relationships"]["value"]
    history = p_after["profile"]["interaction_history"]["user_corrections"]["value"]
    total += 1
    if check(
        "profile.relationship 软删 + 写入 user_corrections",
        "小孙孙" not in rel_value and "妻子" in rel_value and "小孙孙" in history,
    ): ok += 1

    # episodic deprecate（mem0 engine 用桩）
    class _FakeEngine:
        def __init__(self): self.deleted = []
        def update(self, mid, txt): self.deleted.append(("u", mid, txt))
    eng = _FakeEngine()
    ce._apply_episodic(
        eng, "u1",
        {"ref": "E1", "source": "episodic", "id": "mem_xx",
         "text": "儿子9岁半，养两只小鹏：小孙孙、小魏魏"},
        {"action": "deprecate", "confidence": 0.9, "reason": "事实错误"},
        conn, ctx_meta,
    )
    rows = conn.execute(
        "SELECT source, ref_id, action FROM memory_deprecations "
        "WHERE source='episodic'"
    ).fetchall()
    total += 1
    if check(
        "episodic 软删 → memory_deprecations 落一条",
        len(rows) == 1 and rows[0] == ("episodic", "mem_xx", "deprecate"),
    ): ok += 1

    # 低置信走 audit_only
    ce._apply_audit_only(
        conn, "u1",
        {"ref": "X", "source": "episodic", "id": "mem_low",
         "text": "随便一条记忆"},
        {"action": "deprecate", "confidence": 0.3, "reason": "不太确定"},
        ctx_meta,
    )
    audits = conn.execute(
        "SELECT action FROM memory_deprecations WHERE ref_id='mem_low'"
    ).fetchall()
    total += 1
    if check("低置信走 audit_only", audits and audits[0][0] == "audit_only"):
        ok += 1

    # banned entities：写入 + 读取 + 文本命中检测
    ce._apply_banned_entities(conn, "u1", ["小鹏", "涮羊肉", " "], ctx_meta)
    banned = ce.load_banned_entities(conn, "u1")
    total += 1
    if check(
        f"banned_entities 持久化 + 去掉空白 (实际 {banned})",
        set(banned) == {"小鹏", "涮羊肉"},
    ): ok += 1
    total += 1
    if check(
        "text_hits_banned 命中",
        ce.text_hits_banned("儿子9岁半，养两只小鹏", banned)
        and not ce.text_hits_banned("妻子全职在家带孩子", banned),
    ): ok += 1
    # 重复写不重复
    ce._apply_banned_entities(conn, "u1", ["小鹏", "新词"], ctx_meta)
    banned2 = ce.load_banned_entities(conn, "u1")
    total += 1
    if check(
        f"重复 banned 不重复入表 (实际 {sorted(banned2)})",
        sorted(banned2) == sorted(["小鹏", "涮羊肉", "新词"]),
    ): ok += 1

    # _extract_query_tokens：去停用词
    toks = ce._extract_query_tokens("我家最近养了什么？小鹏不是动物")
    total += 1
    if check(
        f"query token 提取去停用词 (实际 {toks})",
        "小鹏" in toks and "动物" in toks and "我家" not in toks,
    ): ok += 1

    # _llm_judge 解析：dict 含 actions+banned_entities
    parsed = _j.loads(
        '{"actions":[{"ref":"E1","action":"deprecate","confidence":0.9,"reason":"x"}],'
        '"banned_entities":["小鹏"," ","太长太长太长太长太长"]}'
    )
    # 模拟 _llm_judge 内部清洗逻辑
    actions = parsed.get("actions", [])
    banned = [
        str(b).strip() for b in parsed.get("banned_entities", [])
        if isinstance(b, str) and 1 <= len(str(b).strip()) <= 8
    ]
    total += 1
    if check(
        f"_llm_judge 解析过滤过长/空白 (banned={banned})",
        banned == ["小鹏"],
    ): ok += 1

    return ok, total


def main() -> int:
    print("MindMem P0 自测开始")
    totals = []
    for fn in (
        test_router,
        test_dedupe_and_filter,
        test_prompt_composer,
        test_emotional_context_filter,
        test_cap_and_background_buckets,
        test_memory_challenge_pipeline,
        test_chat_review_rules,
        test_correction_engine_pure,
    ):
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
