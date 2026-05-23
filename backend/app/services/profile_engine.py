"""
用户画像引擎：负责读取、更新、衰减用户结构化画像。
"""
import json
import logging
import os
from datetime import datetime, timezone

import openai

logger = logging.getLogger(__name__)

# 不参与衰减的核心字段
CORE_FIELDS = {"basic.name", "basic.nickname", "basic.gender", "basic.birthday"}

# 累积型字段：新信息追加而不是覆盖（存为字符串，用顿号分隔）
ACCUMULATIVE_FIELDS = {
    "career.skills",
    "career.work_pain_points",
    "interests.long_term",
    "interests.short_term",
    "interests.content_preference",
    "goals_pains.short_term_goals",
    "goals_pains.long_term_goals",
    "goals_pains.current_pains",
    "interaction_history.frequent_topics",
    "interaction_history.explicit_preferences",
    "interaction_history.user_corrections",
}

EXTRACT_SYSTEM_PROMPT = """\
你是一个信息提取器。从以下对话历史中，只从「用户」的消息里提取可能属于用户画像的「事实」。
不要从 AI 助手的消息中提取信息。

每个事实的格式：{"dimension_path": "basic.name", "value": "张三", "confidence": 0.95}

允许的 dimension_path（叶子字段）：
basic.name, basic.nickname, basic.age, basic.birthday, basic.gender, basic.location, basic.language,
social.family_structure, social.relationships,
career.job_title, career.industry, career.skills, career.work_pain_points,
interests.long_term, interests.short_term, interests.content_preference, interests.interaction_style,
values_attitudes.tech_attitude, values_attitudes.sensitive_topics,
habits.active_hours, habits.question_depth,
goals_pains.short_term_goals, goals_pains.long_term_goals, goals_pains.current_pains,
interaction_history.frequent_topics, interaction_history.explicit_preferences

【special.relationships 格式要求】
social.relationships 的 value 必须是 JSON 对象，key 为人物名称或称谓。
value 有两种形式：
1. 直接关系（与用户本人相关）：value 为字符串，如 "配偶，全职在家"
2. 间接关系（通过另一人相关）：value 为对象 {"rel": "关系描述", "via": "中间人名"}
   如小孙孙是儿子的同学，则写为 {"rel": "儿子的同学", "via": "儿子"}
示例：{
  "妻子": "配偶，全职在家",
  "儿子": "子女，9岁半",
  "邻居老爷爷": "邻居，80岁，养狗叫可乐",
  "小孙孙": {"rel": "儿子的同学", "via": "儿子"}
}
每次只提取本次对话中出现的人物，不需要包含历史中已知的所有人。

置信度规则：
- 用户明确陈述（"我叫X"、"我是X"）= 0.95
- 强烈暗示 = 0.7
- 轻微暗示 = 0.5

只输出 JSON 数组，不要有任何其他文字。若无可提取信息则输出 []。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_nested(profile: dict, path: str):
    """获取 'basic.name' 路径的值，返回字段 dict 或 None"""
    parts = path.split(".")
    cur = profile.get("profile", {})
    for p in parts[:-1]:
        cur = cur.get(p, {})
    return cur.get(parts[-1])


def _set_nested(profile: dict, path: str, field_dict: dict):
    """设置 'basic.name' 路径的值"""
    parts = path.split(".")
    cur = profile.setdefault("profile", {})
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = field_dict


def _del_nested(profile: dict, path: str) -> bool:
    parts = path.split(".")
    cur = profile.get("profile", {})
    for p in parts[:-1]:
        cur = cur.get(p)
        if cur is None:
            return False
    return cur.pop(parts[-1], None) is not None


def _merge_accumulative(old_val, new_val) -> str:
    """将旧值和新值合并为去重后的字符串（顿号分隔）"""
    def _to_items(v) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            # 支持顿号、逗号、分号分隔
            import re
            return [x.strip() for x in re.split(r'[、，,；;]', v) if x.strip()]
        return [str(v).strip()] if v else []

    old_items = _to_items(old_val)
    new_items = _to_items(new_val)

    # 去重：新项目如果已在旧项目中（模糊匹配）则跳过
    merged = list(old_items)
    for item in new_items:
        if not any(item in existing or existing in item for existing in merged):
            merged.append(item)

    return "、".join(merged)


def extract_facts_from_conversation(messages: list) -> list[dict]:
    """调用 LLM 从对话中提取候选事实，只看用户消息"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return []

    conversation_text = "\n".join(
        f"用户: {m['content']}" for m in user_msgs
    )

    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    try:
        resp = client.chat.completions.create(
            model=os.getenv("EXTRACT_MODEL", os.getenv("CHAT_MODEL", "qwen-turbo")),
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        logger.warning("extract_facts failed: %s", e)
        return []


def apply_facts_to_profile(
    profile: dict,
    facts: list[dict],
    session_id: str = "",
) -> list[dict]:
    """
    将提取的事实自动应用到画像，返回审计日志列表。

    决策策略：
    - 累积型字段：新值追加合并，不覆盖
    - social.relationships：对象深度合并
    - 覆盖型字段：
        - 值相同 → 提升置信度
        - 新置信度更高 → 自动替换
        - 新置信度相近但值不同 → 用更新的值（更近 = 更准），记录日志
    不再产生 _pending_clarifications。
    """
    audit_logs = []

    # 清理旧的待澄清队列（迁移兼容）
    profile.pop("_pending_clarifications", None)

    for fact in facts:
        path = fact.get("dimension_path", "")
        new_val = fact.get("value")
        new_conf = float(fact.get("confidence", 0.5))

        if not path or new_val is None:
            continue

        old_field = _get_nested(profile, path)

        if old_field is None:
            # 新增
            _set_nested(profile, path, {
                "value": new_val,
                "confidence": new_conf,
                "updated_at": _now_iso(),
            })
            audit_logs.append({
                "dimension_path": path,
                "old_value": "",
                "new_value": json.dumps(new_val, ensure_ascii=False),
                "action": "added",
                "session_id": session_id,
            })

        elif path == "social.relationships" and isinstance(new_val, dict):
            # 关系图：深度合并
            old_val = old_field.get("value", {})
            if not isinstance(old_val, dict):
                old_val = {}
            merged = {**old_val, **new_val}
            _set_nested(profile, path, {
                "value": merged,
                "confidence": max(float(old_field.get("confidence", 0.5)), new_conf),
                "updated_at": _now_iso(),
            })
            audit_logs.append({
                "dimension_path": path,
                "old_value": json.dumps(old_val, ensure_ascii=False),
                "new_value": json.dumps(merged, ensure_ascii=False),
                "action": "merged",
                "session_id": session_id,
            })

        elif path in ACCUMULATIVE_FIELDS:
            # 累积型：追加合并
            old_val = old_field.get("value", "")
            merged_val = _merge_accumulative(old_val, new_val)
            if merged_val != str(old_val):
                _set_nested(profile, path, {
                    "value": merged_val,
                    "confidence": max(float(old_field.get("confidence", 0.5)), new_conf),
                    "updated_at": _now_iso(),
                })
                audit_logs.append({
                    "dimension_path": path,
                    "old_value": json.dumps(old_val, ensure_ascii=False),
                    "new_value": json.dumps(merged_val, ensure_ascii=False),
                    "action": "appended",
                    "session_id": session_id,
                })

        else:
            # 覆盖型
            old_val = old_field.get("value")
            old_conf = float(old_field.get("confidence", 0.5))

            if _values_equal(old_val, new_val):
                # 相同值，提升置信度
                old_field["confidence"] = min(old_conf + 0.05, 0.99)
                old_field["updated_at"] = _now_iso()
                audit_logs.append({
                    "dimension_path": path,
                    "old_value": json.dumps(old_val, ensure_ascii=False),
                    "new_value": json.dumps(new_val, ensure_ascii=False),
                    "action": "confirmed",
                    "session_id": session_id,
                })
            else:
                # 值不同：新值更可信或更新 → 自动替换
                _set_nested(profile, path, {
                    "value": new_val,
                    "confidence": new_conf,
                    "updated_at": _now_iso(),
                })
                action = "replaced" if new_conf >= old_conf else "replaced_lower_conf"
                audit_logs.append({
                    "dimension_path": path,
                    "old_value": json.dumps(old_val, ensure_ascii=False),
                    "new_value": json.dumps(new_val, ensure_ascii=False),
                    "action": action,
                    "session_id": session_id,
                })

    profile["last_updated"] = _now_iso()
    return audit_logs


def _values_equal(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return set(str(x) for x in a) == set(str(x) for x in b)
    return str(a).strip() == str(b).strip()


def decay_profile(profile: dict) -> list[dict]:
    """
    衰减超过 30 天未更新的非核心字段。
    返回被删除的字段路径列表。
    """
    now = datetime.now(timezone.utc)
    deleted = []

    def _traverse(node: dict, path_prefix: str):
        for key, val in list(node.items()):
            if key.startswith("_"):
                continue
            cur_path = f"{path_prefix}.{key}" if path_prefix else key
            if isinstance(val, dict) and "value" in val and "updated_at" in val:
                if cur_path in CORE_FIELDS:
                    continue
                try:
                    updated = datetime.fromisoformat(val["updated_at"])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    days = (now - updated).days
                    if days > 30:
                        conf = float(val.get("confidence", 0.5))
                        new_conf = max(0.3, conf * 0.9)
                        if new_conf <= 0.3:
                            node.pop(key)
                            deleted.append(cur_path)
                        else:
                            val["confidence"] = round(new_conf, 4)
                except Exception:
                    pass
            elif isinstance(val, dict):
                _traverse(val, cur_path)

    _traverse(profile.get("profile", {}), "")
    return deleted


FIELD_LABELS: dict[str, str] = {
    "name": "姓名", "nickname": "昵称", "age": "年龄", "birthday": "生日",
    "gender": "性别", "location": "所在地", "language": "常用语言",
    "family_structure": "家庭结构", "relationships": "重要关系",
    "job_title": "职位", "industry": "所在行业", "skills": "技能",
    "work_pain_points": "工作痛点",
    "long_term": "长期兴趣", "short_term": "近期关注",
    "content_preference": "内容偏好", "interaction_style": "交流风格",
    "tech_attitude": "技术态度", "sensitive_topics": "敏感话题",
    "active_hours": "活跃时段", "question_depth": "提问深度",
    "short_term_goals": "短期目标", "long_term_goals": "长期目标",
    "current_pains": "当前痛点",
    "frequent_topics": "常聊话题", "unresolved_issues": "未解决问题",
    "user_corrections": "用户纠正记录", "explicit_preferences": "明确偏好",
}

PATH_LABELS: dict[str, str] = {
    "basic": "基本信息", "social": "社会关系", "career": "职业",
    "interests": "兴趣偏好", "values_attitudes": "价值观",
    "habits": "习惯", "goals_pains": "目标与痛点",
    "interaction_history": "交互偏好",
}


def _label(path: str) -> str:
    parts = path.split(".")
    section = PATH_LABELS.get(parts[0], parts[0])
    field = FIELD_LABELS.get(parts[-1], parts[-1]) if len(parts) > 1 else ""
    return f"{section} · {field}" if field else section


def format_profile_for_prompt(profile: dict) -> str:
    """将画像格式化为系统提示文本"""
    p = profile.get("profile", {})
    if not p:
        return ""

    lines = []

    def _add_section(section_key: str):
        section = p.get(section_key, {})
        items = []
        for field_key, field_val in section.items():
            if isinstance(field_val, dict) and "value" in field_val:
                conf = field_val.get("confidence", 0)
                if conf >= 0.5:
                    label = FIELD_LABELS.get(field_key, field_key)
                    items.append(f"  - {label}: {field_val['value']}（置信度 {conf:.0%}）")
        if items:
            lines.append(f"[{PATH_LABELS.get(section_key, section_key)}]")
            lines.extend(items)

    _add_section("basic")
    _add_section("career")
    _add_section("interests")
    _add_section("habits")
    _add_section("goals_pains")
    _add_section("social")
    _add_section("interaction_history")

    return "\n".join(lines)

import json
import logging
import os
from datetime import datetime, timezone

import openai

logger = logging.getLogger(__name__)

# 不参与衰减的核心字段
CORE_FIELDS = {"basic.name", "basic.nickname", "basic.gender", "basic.birthday"}

EXTRACT_SYSTEM_PROMPT = """\
你是一个信息提取器。从以下对话历史中，只从「用户」的消息里提取可能属于用户画像的「事实」。
不要从 AI 助手的消息中提取信息。

每个事实的格式：{"dimension_path": "basic.name", "value": "张三", "confidence": 0.95}

允许的 dimension_path（叶子字段）：
basic.name, basic.nickname, basic.age, basic.birthday, basic.gender, basic.location, basic.language,
social.family_structure, social.relationships,
career.job_title, career.industry, career.skills, career.work_pain_points,
interests.long_term, interests.short_term, interests.content_preference, interests.interaction_style,
values_attitudes.tech_attitude, values_attitudes.sensitive_topics,
habits.active_hours, habits.question_depth,
goals_pains.short_term_goals, goals_pains.long_term_goals, goals_pains.current_pains,
interaction_history.frequent_topics, interaction_history.explicit_preferences

【special.relationships 格式要求】
social.relationships 的 value 必须是 JSON 对象，key 为人物名称或称谓。
value 有两种形式：
1. 直接关系（与用户本人相关）：value 为字符串，如 "配偶，全职在家"
2. 间接关系（通过另一人相关）：value 为对象 {"rel": "关系描述", "via": "中间人名"}
   如小孙孙是儿子的同学，则写为 {"rel": "儿子的同学", "via": "儿子"}
示例：{
  "妻子": "配偶，全职在家",
  "儿子": "子女，9岁半",
  "邻居老爷爷": "邻居，80岁，养狗叫可乐",
  "小孙孙": {"rel": "儿子的同学", "via": "儿子"}
}
每次只提取本次对话中出现的人物，不需要包含历史中已知的所有人。

置信度规则：
- 用户明确陈述（"我叫X"、"我是X"）= 0.95
- 强烈暗示 = 0.7
- 轻微暗示 = 0.5

只输出 JSON 数组，不要有任何其他文字。若无可提取信息则输出 []。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_nested(profile: dict, path: str):
    """获取 'basic.name' 路径的值，返回字段 dict 或 None"""
    parts = path.split(".")
    cur = profile.get("profile", {})
    for p in parts[:-1]:
        cur = cur.get(p, {})
    return cur.get(parts[-1])


def _set_nested(profile: dict, path: str, field_dict: dict):
    """设置 'basic.name' 路径的值"""
    parts = path.split(".")
    cur = profile.setdefault("profile", {})
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = field_dict


def _del_nested(profile: dict, path: str) -> bool:
    parts = path.split(".")
    cur = profile.get("profile", {})
    for p in parts[:-1]:
        cur = cur.get(p)
        if cur is None:
            return False
    return cur.pop(parts[-1], None) is not None


def extract_facts_from_conversation(messages: list) -> list[dict]:
    """调用 LLM 从对话中提取候选事实，只看用户消息"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return []

    conversation_text = "\n".join(
        f"用户: {m['content']}" for m in user_msgs
    )

    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    try:
        resp = client.chat.completions.create(
            model=os.getenv("EXTRACT_MODEL", os.getenv("CHAT_MODEL", "qwen-turbo")),
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        # 容错：去掉可能的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        logger.warning("extract_facts failed: %s", e)
        return []


def apply_facts_to_profile(
    profile: dict,
    facts: list[dict],
    session_id: str = "",
) -> list[dict]:
    """
    将提取的事实应用到画像，返回审计日志列表。
    对矛盾信息写入 _pending_clarifications。
    """
    audit_logs = []
    pending = profile.setdefault("_pending_clarifications", [])

    for fact in facts:
        path = fact.get("dimension_path", "")
        new_val = fact.get("value")
        new_conf = float(fact.get("confidence", 0.5))

        if not path or new_val is None:
            continue

        old_field = _get_nested(profile, path)

        if old_field is None:
            # 新增
            _set_nested(profile, path, {
                "value": new_val,
                "confidence": new_conf,
                "updated_at": _now_iso(),
            })
            audit_logs.append({
                "dimension_path": path,
                "old_value": "",
                "new_value": json.dumps(new_val, ensure_ascii=False),
                "session_id": session_id,
            })
        else:
            old_val = old_field.get("value")
            old_conf = float(old_field.get("confidence", 0.5))

            # social.relationships 特殊处理：合并对象而非替换
            if path == "social.relationships" and isinstance(new_val, dict):
                if not isinstance(old_val, dict):
                    old_val = {}
                merged = {**old_val, **new_val}
                _set_nested(profile, path, {
                    "value": merged,
                    "confidence": max(old_conf, new_conf),
                    "updated_at": _now_iso(),
                })
                audit_logs.append({
                    "dimension_path": path,
                    "old_value": json.dumps(old_val, ensure_ascii=False),
                    "new_value": json.dumps(merged, ensure_ascii=False),
                    "session_id": session_id,
                })
            elif _values_equal(old_val, new_val):
                # 相同，提升置信度
                new_conf_bumped = min(old_conf + 0.05, 0.99)
                old_field["confidence"] = new_conf_bumped
                old_field["updated_at"] = _now_iso()
            elif new_conf > old_conf + 0.2:
                # 高置信度覆盖
                audit_logs.append({
                    "dimension_path": path,
                    "old_value": json.dumps(old_val, ensure_ascii=False),
                    "new_value": json.dumps(new_val, ensure_ascii=False),
                    "session_id": session_id,
                })
                _set_nested(profile, path, {
                    "value": new_val,
                    "confidence": new_conf,
                    "updated_at": _now_iso(),
                })
            else:
                # 矛盾，加入待澄清队列（去重）
                already = any(
                    c["path"] == path and c["new_value"] == new_val
                    for c in pending
                )
                if not already:
                    pending.append({
                        "path": path,
                        "old_value": old_val,
                        "new_value": new_val,
                        "created_at": _now_iso(),
                    })

    profile["last_updated"] = _now_iso()
    return audit_logs


def _values_equal(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return set(str(x) for x in a) == set(str(x) for x in b)
    return str(a).strip() == str(b).strip()


def decay_profile(profile: dict) -> list[dict]:
    """
    衰减超过 30 天未更新的非核心字段。
    返回被删除的字段路径列表。
    """
    now = datetime.now(timezone.utc)
    deleted = []

    def _traverse(node: dict, path_prefix: str):
        for key, val in list(node.items()):
            if key.startswith("_"):
                continue
            cur_path = f"{path_prefix}.{key}" if path_prefix else key
            if isinstance(val, dict) and "value" in val and "updated_at" in val:
                # 叶子字段
                if cur_path in CORE_FIELDS:
                    continue
                try:
                    updated = datetime.fromisoformat(val["updated_at"])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    days = (now - updated).days
                    if days > 30:
                        conf = float(val.get("confidence", 0.5))
                        new_conf = max(0.3, conf * 0.9)
                        if new_conf <= 0.3:
                            node.pop(key)
                            deleted.append(cur_path)
                        else:
                            val["confidence"] = round(new_conf, 4)
                except Exception:
                    pass
            elif isinstance(val, dict):
                _traverse(val, cur_path)

    _traverse(profile.get("profile", {}), "")
    return deleted
