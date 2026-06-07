"""线上聊天记录评估（P0+P1）：在 chat_audit_v1 之上叠加 L1 启发式 + 归因建议。

设计原则：
- 不重跑 Router/Context/LLM，零 token 成本
- 单轮判断基于 prompt_meta（route / context_layers / activated / system / reply）
- snapshot 采用「当时 pipeline 看到的池子」= context_layers + snapshot_stats
- 旧轮无 snapshot_stats 也能跑，仅缺一点「池外有没有」的判断
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

REVIEW_SCHEMA_VERSION = "chat_eval_review_v1"

# ============ 通用文本工具 ============


_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5a-zA-Z]{2,}")

_STOPWORDS = {
    "什么", "怎么", "怎样", "知道", "记得", "可以", "应该", "为什么",
    "我的", "你的", "他的", "她的", "我们", "你们", "他们", "她们",
    "今天", "现在", "最近", "已经", "还是", "或者", "这个", "那个",
    "一下", "一些", "有没有", "一直", "总是", "好吗", "对吗", "是吗",
}

# 用户句里出现这些关键词时，更可能是"在调取记忆"，触发 recall/data 类规则
_RECALL_KEYWORDS = {
    "出差", "面试", "工作", "公司", "项目", "App", "应用",
    "妻子", "老婆", "孩子", "儿子", "女儿", "邻居", "同事", "猫", "狗",
    "上次", "上周", "之前", "那天", "上个月",
}

# 通用鸡汤 / 客服腔 / 模板措辞
_GENERIC_PHRASES = [
    "慢慢来", "会好起来", "加油", "未来可期", "你已经很棒了",
    "说出来会感觉好一些", "心事说出来",
    "我能更好地", "我能帮你", "如果你愿意分享",
    "听起来你", "看起来你", "我能感受到你",
    "建议你", "你可以试试", "深呼吸", "放松一下",
]


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    toks = set()
    for w in _TOKEN_RE.findall(text):
        w = w.strip().lower()
        if not w or w in _STOPWORDS:
            continue
        toks.add(w)
    return toks


def _has_any(text: str, words: list[str] | set[str]) -> list[str]:
    if not text:
        return []
    return [w for w in words if w and w in text]


# ============ L1 数据结构 ============


def _rule(rule_id: str, status: str, severity: str, detail: str = "",
          attribution: list[str] | None = None) -> dict:
    """status ∈ {pass, suspicious, fail, skip}; attribution ⊆ {A,B,C,D,E,F}"""
    return {
        "id": rule_id,
        "status": status,
        "severity": severity,
        "detail": detail,
        "attribution": attribution or [],
    }


# ============ 单条 L1 规则 ============


def _rule_error_reply(reply: str | None, error: str | None) -> dict:
    """F 层：连接错误/空回复。"""
    has_error = bool(error) or (
        reply is not None and (
            reply.startswith("[连接失败")
            or reply.startswith("[错误:")
            or reply.startswith("[连接中断")
        )
    )
    if has_error:
        return _rule(
            "error_reply", "fail", "high",
            f"传输/系统错误：error={error or 'n/a'}",
            attribution=["F"],
        )
    if reply is not None and not reply.strip():
        return _rule(
            "error_reply", "fail", "high",
            "空回复",
            attribution=["F"],
        )
    return _rule("error_reply", "pass", "high")


def _rule_generic_reply(reply: str | None, intent: str) -> dict:
    """E 层：鸡汤/模板/客服腔。仅在情绪/关系/质问等需要走心的场景检查。"""
    if not reply:
        return _rule("generic_reply", "skip", "medium", "无回复")
    if intent in {"knowledge_task", "casual"}:
        return _rule("generic_reply", "skip", "medium", "intent 非情绪类，规则不适用")
    hits = _has_any(reply, _GENERIC_PHRASES)
    if hits:
        return _rule(
            "generic_reply", "suspicious", "medium",
            f"命中模板措辞：{hits}",
            attribution=["E"],
        )
    return _rule("generic_reply", "pass", "medium")


_PUNCT_RE = re.compile(r"[\s，。！？、；：,\.!\?;:\(\)\[\]【】「」“”\"'\u3000]+")


def _ngrams(text: str, n: int = 2) -> set[str]:
    s = _PUNCT_RE.sub("", text or "").lower()
    if len(s) < n:
        return {s} if s else set()
    return {s[i: i + n] for i in range(len(s) - n + 1)}


def _rule_reply_off_topic(
    user_msg: str, reply: str | None, intent: str
) -> dict:
    """E 层：回复与用户句字符 2-gram 重叠极少 且 没有反问。
    中文不分词，token 法不可靠；改 2-gram 子串重叠率（>=0.1 视为有交集）。
    """
    if not reply or not user_msg:
        return _rule("reply_off_topic", "skip", "medium", "缺少回复/用户句")
    if intent == "casual":
        return _rule("reply_off_topic", "skip", "medium", "闲聊场景不检查 off_topic")
    u = _ngrams(user_msg, 2)
    r = _ngrams(reply, 2)
    if not u or not r:
        return _rule("reply_off_topic", "skip", "medium", "字符量过少，不评")
    overlap = u & r
    ratio = len(overlap) / max(len(u), 1)
    has_question = "?" in reply or "？" in reply
    if ratio < 0.1 and not has_question:
        return _rule(
            "reply_off_topic", "suspicious", "medium",
            f"用户句与回复字符 2-gram 重叠率 {ratio:.0%} 且未反问",
            attribution=["E"],
        )
    return _rule("reply_off_topic", "pass", "medium")


def _gather_pool_texts(prompt_meta: dict) -> dict[str, list[str]]:
    layers = prompt_meta.get("context_layers") or {}
    activated = prompt_meta.get("activated") or []
    system_text = prompt_meta.get("system") or ""
    return {
        "explicit": [
            (it.get("text") or "")
            for it in activated
            if it.get("usage") == "explicit_ok"
        ],
        "background": [
            (it.get("text") or "")
            for it in activated
            if it.get("usage") == "background_only"
        ],
        "pool_all": [
            (it.get("text") or "")
            for key in (
                "stable_profile",
                "relevant_relationships",
                "relevant_events",
                "relevant_memories",
                "background_only",
            )
            for it in (layers.get(key) or [])
        ],
        "system": [system_text],
    }


def _hits_in(user_kw: list[str], texts: list[str]) -> list[str]:
    hits: list[str] = []
    joined = "\n".join(texts)
    for kw in user_kw:
        if kw and kw in joined and kw not in hits:
            hits.append(kw)
    return hits


def _extract_recall_kw(user_msg: str) -> list[str]:
    """中文不便分词，直接子串匹配。"""
    if not user_msg:
        return []
    return [k for k in _RECALL_KEYWORDS if k in user_msg]


def _rule_recall_for_challenge(
    user_msg: str, prompt_meta: dict
) -> dict:
    """C/D 层：memory_challenge 时，用户句中的"实词"应在 explicit 或 background 中出现至少 1 个。"""
    intent = (prompt_meta.get("route") or {}).get("intent")
    if intent != "memory_challenge":
        return _rule("recall_for_challenge", "skip", "high", "非 memory_challenge")
    user_kw = _extract_recall_kw(user_msg)
    if not user_kw:
        return _rule("recall_for_challenge", "skip", "high", "用户句无可识别召回关键词")

    pools = _gather_pool_texts(prompt_meta)
    in_explicit = _hits_in(user_kw, pools["explicit"])
    in_background = _hits_in(user_kw, pools["background"])
    in_pool = _hits_in(user_kw, pools["pool_all"])

    if in_explicit:
        return _rule(
            "recall_for_challenge", "pass", "high",
            f"用户关键词已 explicit：{in_explicit}",
        )
    if in_background and in_pool:
        # 在池里但只到 background → C/D 层未把它显性化
        return _rule(
            "recall_for_challenge", "suspicious", "high",
            f"用户关键词仅在 background：{in_background}，但 memory_challenge 应有 explicit",
            attribution=["C", "D"],
        )
    if in_pool:
        return _rule(
            "recall_for_challenge", "suspicious", "high",
            f"用户关键词在池里却未激活：{in_pool}",
            attribution=["C"],
        )
    return _rule(
        "recall_for_challenge", "fail", "high",
        f"用户关键词在池里都找不到：{user_kw}（可能 A 数据缺失，或 C 召回未触达）",
        attribution=["A", "C"],
    )


def _rule_recall_for_self_summary(
    user_msg: str, prompt_meta: dict
) -> dict:
    """C/D 层：self_summary 询问家庭/关系时，relationship 应有 explicit。"""
    intent = (prompt_meta.get("route") or {}).get("intent")
    if intent != "self_summary":
        return _rule("recall_for_self_summary", "skip", "high", "非 self_summary")
    # 是否在问家庭/关系
    family_signal = any(
        k in user_msg for k in
        ("家", "成员", "家人", "妻子", "老婆", "孩子", "儿子",
         "女儿", "身边", "朋友", "亲人", "家庭")
    )
    if not family_signal:
        return _rule(
            "recall_for_self_summary", "skip", "high",
            "self_summary 但未涉及家庭/关系话题",
        )
    activated = prompt_meta.get("activated") or []
    explicit_rel = [
        (it.get("text") or "")
        for it in activated
        if it.get("usage") == "explicit_ok" and it.get("source") == "relationship"
    ]
    if explicit_rel:
        return _rule(
            "recall_for_self_summary", "pass", "high",
            f"已激活 {len(explicit_rel)} 条 relationship",
        )
    pool_rel = (prompt_meta.get("context_layers") or {}).get(
        "relevant_relationships"
    ) or []
    if pool_rel:
        return _rule(
            "recall_for_self_summary", "fail", "high",
            f"池里有 {len(pool_rel)} 条关系，但都未 explicit_ok",
            attribution=["C", "D"],
        )
    return _rule(
        "recall_for_self_summary", "fail", "high",
        "关系层完全为空",
        attribution=["A", "C"],
    )


def _rule_data_vs_activation_gap(
    user_msg: str, prompt_meta: dict
) -> dict:
    """C 层（精化）：用户句关键词在池里有，但既未 explicit 也未 background，
    说明 Context/Compose 把它过滤掉了。"""
    user_kw = _extract_recall_kw(user_msg)
    if not user_kw:
        return _rule("data_vs_activation_gap", "skip", "medium", "用户句无召回关键词")
    pools = _gather_pool_texts(prompt_meta)
    in_pool = _hits_in(user_kw, pools["pool_all"])
    in_activated_or_bg = _hits_in(
        user_kw, pools["explicit"] + pools["background"]
    )
    gap = [w for w in in_pool if w not in in_activated_or_bg]
    if gap:
        return _rule(
            "data_vs_activation_gap", "suspicious", "medium",
            f"池里有但本轮未激活：{gap}",
            attribution=["C"],
        )
    return _rule("data_vs_activation_gap", "pass", "medium")


# 用户在纠错时可能给出的"正确事实"信号词
_CORRECTION_FACT_HINTS = {
    "是", "不是", "其实", "应该", "实际", "真的",
    "同学", "朋友", "孩子", "猫", "狗", "宠物", "动物", "人",
}

# 纠错推卸句式（D 层模板违规）
_CORRECTION_DEFLECT_PHRASES = [
    "请你告诉我", "请你再告诉",
    "你告诉我", "告诉我一下",
    "我会更新记忆", "我会更新一下记忆",
    "你说的是什么", "你说的是哪",
    "再告诉我一次",
]


def _rule_correction_persisted(
    prev_turn: dict | None, curr_meta: dict
) -> dict:
    """A+B 层：上一轮已 intent=correction，本轮池里**仍然**出现上一轮纠正过的实体词
    → 说明 correction_cleanup 没生效或被 extract 链路重新写回。

    检测方式：用上一轮 user_msg 抽 _RECALL_KEYWORDS + _ENTITY_TOKENS 命中词，
    再看本轮 context_layers 的所有 episodic/event/relationship 文本里是否包含。
    """
    if not prev_turn:
        return _rule("correction_persisted", "skip", "high", "无上一轮")
    prev_audit = prev_turn.get("audit") or {}
    prev_meta = prev_audit.get("prompt_meta") or {}
    prev_intent = (prev_meta.get("route") or {}).get("intent")
    if prev_intent != "correction":
        return _rule("correction_persisted", "skip", "high", "上一轮非 correction")
    prev_user = ((prev_turn.get("input") or {}).get("user_message")) or ""
    # 用户纠正里出现的实体词（候选黑名单）
    candidates = sorted(
        {w for w in _ENTITY_TOKENS if w in prev_user}
        | {w for w in _RECALL_KEYWORDS if w in prev_user},
        key=len, reverse=True,
    )
    if not candidates:
        return _rule(
            "correction_persisted", "skip", "high",
            "上一轮用户句无可追踪的实体词",
        )
    layers = (curr_meta.get("context_layers") or {})
    pool_texts: list[str] = []
    for key in (
        "relevant_relationships",
        "relevant_events",
        "relevant_memories",
        "background_only",
    ):
        for it in (layers.get(key) or []):
            pool_texts.append(it.get("text") or "")
    joined = "\n".join(pool_texts)
    # 关键：要看的是「被纠错的错误实体」是否还在池里
    # 简化策略：上一轮 user 中的 candidates 出现在本轮池子里，就算未清理
    still_in = [w for w in candidates if w and w in joined]
    if still_in:
        return _rule(
            "correction_persisted", "fail", "high",
            f"上一轮纠正过的实体在本轮池里仍出现：{still_in}",
            attribution=["A", "B"],
        )
    return _rule("correction_persisted", "pass", "high")


def _rule_correction_no_concrete_ack(
    user_msg: str, reply: str | None, intent: str
) -> dict:
    """D 层：correction 回复缺乏「复述正确事实」，只剩道歉/推卸。

    判定：intent=correction 且
      a) reply 命中推卸短语，或
      b) reply 与 user_msg 的字符 2-gram 重合率 < 5%（几乎没承接用户给的具体事实）
    """
    if intent != "correction":
        return _rule(
            "correction_no_concrete_ack", "skip", "high", "非 correction"
        )
    if not reply:
        return _rule(
            "correction_no_concrete_ack", "skip", "high", "无回复"
        )
    deflects = _has_any(reply, _CORRECTION_DEFLECT_PHRASES)
    u_grams = _ngrams(user_msg, 2)
    r_grams = _ngrams(reply, 2)
    overlap = u_grams & r_grams if u_grams and r_grams else set()
    overlap_ratio = (len(overlap) / max(len(u_grams), 1)) if u_grams else 0.0
    if deflects and overlap_ratio < 0.10:
        return _rule(
            "correction_no_concrete_ack", "fail", "high",
            f"纠错回复在推卸：命中 {deflects}，对用户事实承接率 {overlap_ratio:.0%}",
            attribution=["D"],
        )
    if deflects:
        return _rule(
            "correction_no_concrete_ack", "suspicious", "high",
            f"出现推卸句式 {deflects}（但有一定事实承接）",
            attribution=["D"],
        )
    if overlap_ratio < 0.05:
        return _rule(
            "correction_no_concrete_ack", "suspicious", "high",
            f"几乎没承接用户给的事实（重合率 {overlap_ratio:.0%}）",
            attribution=["D"],
        )
    return _rule("correction_no_concrete_ack", "pass", "high")


# 回复中常见的"具体实体"白名单——出现这些词时如果池里没有对应字面 → 幻觉
_ENTITY_TOKENS = {
    "猫", "狗", "鸟", "鱼", "兔子", "仓鼠", "乌龟", "鹦鹉",
    "小鹏",  # 经典误造词
    "出差", "面试", "辞职", "升职", "搬家", "结婚",
    "北京", "上海", "深圳", "广州", "杭州",
    "华联", "公司", "项目",
    "妻子", "老婆", "丈夫", "孩子", "儿子", "女儿",
    "同学", "同事", "朋友", "邻居",
}


def _rule_fabrication_under_challenge(
    reply: str | None, prompt_meta: dict
) -> dict:
    """E+A 层：memory_challenge 下，reply 提到具体实体词，但池子里完全没有对应字面
    → 大概率幻觉（如池里只有"养了一只猫"，AI 却说"猫和狗"）。
    """
    intent = (prompt_meta.get("route") or {}).get("intent")
    if intent != "memory_challenge":
        return _rule("fabrication_under_challenge", "skip", "high", "非 memory_challenge")
    if not reply:
        return _rule("fabrication_under_challenge", "skip", "high", "无回复")

    pools = _gather_pool_texts(prompt_meta)
    pool_joined = "\n".join(pools["pool_all"])
    reply_entities = sorted(
        {w for w in _ENTITY_TOKENS if w in reply},
        key=len, reverse=True,
    )
    if not reply_entities:
        return _rule(
            "fabrication_under_challenge", "skip", "high",
            "回复未提及强实体词",
        )
    hallucinated = [w for w in reply_entities if w not in pool_joined]
    if hallucinated:
        return _rule(
            "fabrication_under_challenge", "fail", "high",
            f"回复提到 {hallucinated}，池里无对应字面",
            attribution=["E", "A"],
        )
    return _rule(
        "fabrication_under_challenge", "pass", "high",
        f"实体 {reply_entities} 均在池中",
    )


# ============ 人格签名一致性规则 ============

# 每个人格的硬指标。修改这里要同步 prompt_composer.PERSONALITY_CONTRACT。
# - max_reply_chars：回复总长上限（按中文字符计，超出即 fail）
# - max_questions：reply 里反问句数量上限（"？""?"统计）
# - max_explicit_memories：单轮允许"显性引用"的旧记忆条数
# - allow_proactive_memory：是否允许用户没问就主动引用旧记忆
_PERSONALITY_SIGNATURE = {
    "introvert": {
        "max_reply_chars": 36,        # 30 字硬约束 + 20% buffer 容标点
        "max_questions": 0,
        "max_explicit_memories": 1,
        "allow_proactive_memory": False,
    },
    "balanced": {
        "max_reply_chars": 72,        # 60 字 + 20%
        "max_questions": 1,
        "max_explicit_memories": 2,
        "allow_proactive_memory": True,
    },
    "extrovert": {
        "max_reply_chars": 108,       # 90 字 + 20%
        "max_questions": 1,
        "max_explicit_memories": 3,
        "allow_proactive_memory": True,
    },
}

# 仅 knowledge_task 与人格无关，跳过；其余包括敏感场景都按契约检查
# （契约自含"敏感场景压抑版"子分支，能区分内向/中性/外向的不同表现）
_PERSONALITY_SKIP_INTENTS = {
    "knowledge_task",
}


def _count_questions(reply: str) -> int:
    """数 reply 里"问句"数量：以？或?结尾的子句，并去掉显然反问助词（吗/呢）只一个的口语化情形。"""
    if not reply:
        return 0
    return reply.count("？") + reply.count("?")


def _count_chinese_chars(reply: str) -> int:
    """按"可见汉字+字母+数字"统计长度，忽略空白和标点。"""
    if not reply:
        return 0
    return sum(1 for c in reply if c.strip() and c not in "，。！？、；：…—~·,.!?;:\n\r\"'""''「」（）()【】[]<>《》")


def _rule_personality_signature(reply: str | None, prompt_meta: dict) -> dict:
    """G 层（性格契约）：reply 是否符合本轮 personality 的硬指标。
    
    指标：字数上限、反问数上限、显性引用条数上限、是否主动引用记忆。
    任何一个硬指标越界即 fail。
    """
    route = prompt_meta.get("route") or {}
    intent = route.get("intent") or ""
    personality = route.get("personality") or "balanced"
    sig = _PERSONALITY_SIGNATURE.get(personality)
    if not sig:
        return _rule("personality_signature", "skip", "medium",
                     f"未知人格 {personality}")
    if intent in _PERSONALITY_SKIP_INTENTS:
        return _rule("personality_signature", "skip", "medium",
                     f"intent={intent}（敏感场景人格主动性被覆盖）")
    if not reply:
        return _rule("personality_signature", "skip", "medium", "无回复")

    violations: list[str] = []

    chars = _count_chinese_chars(reply)
    if chars > sig["max_reply_chars"]:
        violations.append(f"字数 {chars}>{sig['max_reply_chars']}")

    qcount = _count_questions(reply)
    if qcount > sig["max_questions"]:
        violations.append(f"反问 {qcount}>{sig['max_questions']}")

    activated = prompt_meta.get("activated") or []
    explicit_count = sum(1 for it in activated if it.get("usage") == "explicit_ok")
    if explicit_count > sig["max_explicit_memories"]:
        violations.append(
            f"explicit {explicit_count}>{sig['max_explicit_memories']}"
        )

    # 主动引用判断（仅对禁主动引用的人格，如内向型）
    # 这些 intent 本身就允许/要求引用记忆，不算"主动"：
    if not sig["allow_proactive_memory"]:
        _MEM_ALLOWED_INTENTS = {
            "self_summary",        # 用户要求"总结你对我的了解"
            "relationship_topic",  # 用户在谈某个人，必须引用关系
            "memory_challenge",    # 用户在质问记忆，必须复述
            "correction",          # 纠错必须复述用户给的事实
        }
        if activated and intent not in _MEM_ALLOWED_INTENTS:
            violations.append(
                f"内向型主动引用 {len(activated)} 条旧记忆（intent={intent}）"
            )

    if violations:
        return _rule(
            "personality_signature", "fail", "medium",
            f"{personality}：" + "；".join(violations),
            attribution=["D"],
        )
    return _rule(
        "personality_signature", "pass", "medium",
        f"{personality} 契约通过（{chars}字/{qcount}问/{explicit_count}显性）",
    )


# ============ 评估单轮 ============


def _l0_status_from_checks(checks: list[dict]) -> str:
    if not checks:
        return "skip"
    has_high_fail = any(
        not c.get("pass") and c.get("severity") == "high" for c in checks
    )
    if has_high_fail:
        return "fail"
    any_fail = any(not c.get("pass") for c in checks)
    return "warn" if any_fail else "pass"


def _l1_status_aggregate(rules: list[dict]) -> str:
    statuses = [r.get("status") for r in rules]
    if "fail" in statuses:
        return "bad"
    if "suspicious" in statuses:
        return "suspicious"
    if any(s == "pass" for s in statuses):
        return "ok"
    return "skip"


def _final_status(l0: str, l1: str) -> str:
    """整轮一句话结论。
    
    评分原则（v1.2 后调整）：
    - L0 fail（high 失败）或 L1 bad → bad
    - L1 suspicious → suspicious（无论 L0）
    - L0 pass + L1 pass/ok → good
    - L0 warn（仅 medium 失败）+ L1 pass/ok/skip → ok（不再因 medium 假阳性掉到 suspicious）
    - 其它 → skip
    """
    if l0 == "fail" or l1 == "bad":
        return "bad"
    if l1 == "suspicious":
        return "suspicious"
    if l0 == "pass" and l1 in {"ok", "skip"}:
        return "good"
    if l0 in {"pass", "warn"} and l1 in {"ok", "skip"}:
        return "ok"
    return "skip"


def review_turn(turn: dict, prev_turn: dict | None = None) -> dict:
    """对 audit 包里的一个 turn 计算 L1 评估，**就地** 返回结论 dict。

    prev_turn：上一轮的 turn 字典，用于跨轮规则（如 correction_persisted）。
    """
    audit = turn.get("audit") or {}
    user_msg = ((turn.get("input") or {}).get("user_message")) or ""
    reply = ((turn.get("output") or {}).get("assistant_reply")) or ""
    err = ((turn.get("output") or {}).get("error"))

    # 旧/异常轮：仍能给 F 层结论
    if not audit.get("available"):
        err_rule = _rule_error_reply(reply, err)
        rules = [err_rule]
        attribution = set(err_rule["attribution"])
        if err_rule["status"] == "pass":
            # 仅缺 prompt_meta，不构成 fail
            attribution = set()
            final = "skip"
        else:
            final = "bad"
        return {
            "l0_status": "skip",
            "l0_high_fail": 0,
            "l0_medium_fail": 0,
            "l1_status": _l1_status_aggregate(rules),
            "rules": rules,
            "suggested_root_cause": sorted(attribution),
            "final_status": final,
            "missing_reason": audit.get("missing_reason") or "no_prompt_meta",
            "snapshot_status": "unavailable",
        }

    derived = audit.get("derived") or {}
    checks = derived.get("consistency_checks") or []
    prompt_meta = audit.get("prompt_meta") or {}
    intent = (prompt_meta.get("route") or {}).get("intent") or ""

    rules = [
        _rule_error_reply(reply, err),
        _rule_recall_for_challenge(user_msg, prompt_meta),
        _rule_recall_for_self_summary(user_msg, prompt_meta),
        _rule_data_vs_activation_gap(user_msg, prompt_meta),
        _rule_generic_reply(reply, intent),
        _rule_reply_off_topic(user_msg, reply, intent),
        _rule_correction_persisted(prev_turn, prompt_meta),
        _rule_correction_no_concrete_ack(user_msg, reply, intent),
        _rule_fabrication_under_challenge(reply, prompt_meta),
        _rule_personality_signature(reply, prompt_meta),
    ]

    l0 = _l0_status_from_checks(checks)
    l1 = _l1_status_aggregate(rules)

    # 归因聚合：L0 失败 → 主推 C/D，但保留 L1 给出的具体方向
    attribution: set[str] = set()
    for r in rules:
        if r.get("status") in ("fail", "suspicious"):
            attribution.update(r.get("attribution") or [])
    # L0 high 失败也补一个 C/D 兜底（如 cap_respected/explicit_in_system_section）
    if l0 == "fail":
        attribution.update(["C", "D"])
    elif l0 == "warn":
        attribution.add("D")

    final = _final_status(l0, l1)

    snapshot_stats = prompt_meta.get("snapshot_stats") or {}
    snapshot_status = "at_turn" if snapshot_stats else "context_only"

    return {
        "l0_status": l0,
        "l0_high_fail": sum(
            1 for c in checks if not c.get("pass") and c.get("severity") == "high"
        ),
        "l0_medium_fail": sum(
            1 for c in checks if not c.get("pass") and c.get("severity") == "medium"
        ),
        "l1_status": l1,
        "rules": rules,
        "suggested_root_cause": sorted(attribution),
        "final_status": final,
        "snapshot_status": snapshot_status,
    }


# ============ 会话级汇总 ============


def review_audit_pack(pack: dict) -> dict:
    """对 chat_audit_v1 包跑 L1 评估，原地添加 turn.review 字段并返回扩展后的 pack。"""
    turns = pack.get("turns") or []
    counters = {
        "turns_total": len(turns),
        "turns_skipped": 0,
        "turns_with_snapshot_at_turn": 0,
        "turns_legacy_snapshot": 0,
        "turns_unavailable": 0,
        "l0_pass": 0, "l0_warn": 0, "l0_fail": 0, "l0_skip": 0,
        "l1_ok": 0, "l1_suspicious": 0, "l1_bad": 0, "l1_skip": 0,
        "final_ok": 0, "final_suspicious": 0, "final_bad": 0, "final_skip": 0,
    }
    by_rule: dict[str, dict[str, int]] = {}
    root_cause_counter: dict[str, int] = {}
    by_check_id: dict[str, dict[str, int]] = {}

    prev: dict | None = None
    for turn in turns:
        review = review_turn(turn, prev_turn=prev)
        turn["review"] = review
        prev = turn
        counters[f"l0_{review['l0_status']}"] = (
            counters.get(f"l0_{review['l0_status']}", 0) + 1
        )
        counters[f"l1_{review['l1_status']}"] = (
            counters.get(f"l1_{review['l1_status']}", 0) + 1
        )
        counters[f"final_{review['final_status']}"] = (
            counters.get(f"final_{review['final_status']}", 0) + 1
        )
        if review["snapshot_status"] == "at_turn":
            counters["turns_with_snapshot_at_turn"] += 1
        elif review["snapshot_status"] == "context_only":
            counters["turns_legacy_snapshot"] += 1
        else:
            counters["turns_unavailable"] += 1
        if review["final_status"] == "skip":
            counters["turns_skipped"] += 1

        for r in review["rules"]:
            slot = by_rule.setdefault(
                r["id"], {"pass": 0, "suspicious": 0, "fail": 0, "skip": 0}
            )
            slot[r["status"]] = slot.get(r["status"], 0) + 1
        for code in review["suggested_root_cause"]:
            root_cause_counter[code] = root_cause_counter.get(code, 0) + 1

        # 也聚合 L0 check 统计（来自 audit.summary 可重算）
        audit = turn.get("audit") or {}
        checks = (audit.get("derived") or {}).get("consistency_checks") or []
        for c in checks:
            slot = by_check_id.setdefault(c["id"], {"failed": 0, "total": 0})
            slot["total"] += 1
            if not c.get("pass"):
                slot["failed"] += 1

    evaluable = counters["turns_total"] - counters["turns_skipped"]
    structure_pass_rate = (
        counters["l0_pass"] / evaluable if evaluable else 0.0
    )
    final_ok_rate = (
        counters["final_ok"] / evaluable if evaluable else 0.0
    )

    pack["review"] = {
        "schema": REVIEW_SCHEMA_VERSION,
        "counters": counters,
        "structure_pass_rate": round(structure_pass_rate, 3),
        "final_ok_rate": round(final_ok_rate, 3),
        "rule_stats": by_rule,
        "check_stats": by_check_id,
        "root_cause_top": sorted(
            root_cause_counter.items(), key=lambda kv: kv[1], reverse=True
        ),
        "evaluable_turns": evaluable,
    }
    return pack
