"""
用户画像引擎：负责读取、更新、衰减用户结构化画像。
"""
import json
import logging
import os
import re
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

允许的 dimension_path（叶子字段，严格使用以下列表，多一级少一级都不行）：
basic.name, basic.nickname, basic.age, basic.birthday, basic.gender, basic.location, basic.language,
social.relationships,
career.job_title, career.industry, career.skills, career.work_pain_points,
interests.long_term, interests.short_term, interests.content_preference, interests.interaction_style,
values_attitudes.tech_attitude, values_attitudes.sensitive_topics,
habits.active_hours, habits.question_depth,
goals_pains.short_term_goals, goals_pains.long_term_goals, goals_pains.current_pains,
interaction_history.frequent_topics, interaction_history.explicit_preferences

【basic.age 与 basic.birthday 格式要求】
- basic.age 的 value 必须是「整数」，表示当前年龄（如 42）。禁止写 "83年"、"40岁"、"约40"等字符串
- basic.birthday 的 value 必须是「4 位出生年份字符串」（如 "1983"）或完整日期字符串（如 "1983-06-15"）
- 用户说"我83年的"/"我是83年出生" → 提取 basic.birthday = "1983"（不要写到 age）
- 用户说"我今年40"/"我40岁了" → 提取 basic.age = 40（整数）
- 信息不全时只提取已知字段，不要乱填

【所有人际关系，无论亲属还是朋友，都用 social.relationships 一个字段】
不要再使用 social.family_structure（已弃用）。家人/同事/朋友/邻居等都写进 social.relationships。

【social.relationships 格式要求（必须严格遵守）】
value 必须是 JSON 对象，key 是人物名称或称谓（如 "妻子"、"小孙孙"）。
key 禁止使用 "用户/我/本人/自己" 等指代用户的词。

每个 value 有且只有两种合法形式，二选一：

形式 A · 直系关系（人物与用户本人直接相关）
  value 必须是字符串，描述与用户的关系。
  ❌ 禁止写成 {"rel": "...", "via": "用户"} 或 {"rel": "...", "via": "我"} 这种 via 指向自己的对象
  ✅ 直接用字符串
  例：
    "妻子": "配偶，全职在家"
    "儿子": "子女，9岁半"
    "邻居老爷爷": "邻居，80岁，养狗叫可乐"

形式 B · 间接关系（人物通过另一个被记录的人才与用户相关）
  value 是对象 {"rel": "关系描述", "via": "中间人名"}。
  约束：
    - via 必须是当前 relationships 对象里另一个 key 的名字（一个已记录的具体人）
    - via 严禁取 "用户/我/本人/自己/self/user/me"
    - 如果中间人当前还没被记录，必须在本次输出中也把中间人作为直系或间接关系一并加入
  例：
    "小孙孙": {"rel": "儿子的同学", "via": "儿子"}
    "李工": {"rel": "妻子的同事", "via": "妻子"}

完整示例（"儿子" 是直系→字符串，"小孙孙" 通过儿子→对象）：
{
  "妻子": "配偶，全职在家",
  "儿子": "子女，9岁半",
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


# 当 LLM 把 via 写成指向用户自己的词时，这条记录其实是直系关系
_SELF_VIA_ALIASES = {"用户", "我", "本人", "自己", "self", "user", "me"}

# 允许写入画像的合法字段路径白名单（叶子）
_ALLOWED_FIELD_PATHS = {
    "basic.name", "basic.nickname", "basic.age", "basic.birthday",
    "basic.gender", "basic.location", "basic.language",
    "social.relationships",
    "career.job_title", "career.industry", "career.skills", "career.work_pain_points",
    "interests.long_term", "interests.short_term",
    "interests.content_preference", "interests.interaction_style",
    "values_attitudes.tech_attitude", "values_attitudes.sensitive_topics",
    "habits.active_hours", "habits.question_depth",
    "goals_pains.short_term_goals", "goals_pains.long_term_goals",
    "goals_pains.current_pains",
    "interaction_history.frequent_topics", "interaction_history.explicit_preferences",
    "interaction_history.user_corrections",
}


_YEAR_RE = re.compile(r"(19|20)\d{2}")
_AGE_WITH_UNIT_RE = re.compile(r"(\d{1,3})\s*岁")
_TWO_DIGIT_RE = re.compile(r"(\d{2})")


def _two_digit_to_year(yy: int) -> str:
    """两位年份转四位：>=30 视为 19xx，否则 20xx"""
    return str(1900 + yy if yy >= 30 else 2000 + yy)


def _coerce_age_birthday(path: str, val):
    """规整 basic.age / basic.birthday 的 value：
    - 字符串含"岁" → 取数字当 age
    - 字符串含"年"/4位数字/纯2位数字 → 当作出生年，转 birthday
    - 整数 < 30 → age；30~99 → 2位数年份 → birthday；1900~2100 → birthday
    返回 (new_path, new_val)；无法识别返回 (None, None)。
    """
    # 整数：用值的范围判定
    if isinstance(val, int) and not isinstance(val, bool):
        if 0 < val < 30:
            return ("basic.age", val)
        if 30 <= val < 100:
            return ("basic.birthday", _two_digit_to_year(val))
        if 1900 <= val <= 2100:
            return ("basic.birthday", str(val))
        return (None, None)

    s = str(val).strip() if val is not None else ""
    if not s:
        return (None, None)

    if path == "basic.age":
        # 1) 明确含"岁" → age
        m = _AGE_WITH_UNIT_RE.search(s)
        if m:
            n = int(m.group(1))
            if 0 < n < 130:
                return ("basic.age", n)
        # 2) 含4位年份 → birthday
        m = _YEAR_RE.search(s)
        if m:
            return ("basic.birthday", m.group(0))
        # 3) 含2位数字（"83年"、"83") → birthday
        m = _TWO_DIGIT_RE.search(s)
        if m:
            return ("basic.birthday", _two_digit_to_year(int(m.group(1))))
        return (None, None)

    if path == "basic.birthday":
        if re.fullmatch(r"(19|20)\d{2}-\d{2}-\d{2}", s):
            return ("basic.birthday", s)
        m = _YEAR_RE.search(s)
        if m:
            return ("basic.birthday", m.group(0))
        m = _TWO_DIGIT_RE.search(s)
        if m:
            return ("basic.birthday", _two_digit_to_year(int(m.group(1))))
        return (None, None)

    return (path, val)


def _coerce_fact_path(fact: dict) -> dict | None:
    """把 LLM 偶尔吐出的非法 dimension_path 规整成合法叶子路径。
    - `social.relationships.妻子` + value="配偶" → `social.relationships` + value={"妻子": "配偶"}
    - `social.family_structure(...)` 一律改写为 social.relationships
    - basic.age / basic.birthday 的 value 做强类型校验（"83年"→birthday="1983"）
    - 完全不在白名单的路径 → 返回 None 表示丢弃
    """
    path = (fact.get("dimension_path") or "").strip()
    if not path:
        return None

    # 处理 social.relationships.<人名>
    if path.startswith("social.relationships."):
        name = path[len("social.relationships."):].strip()
        val = fact.get("value")
        if not name:
            return None
        return {
            "dimension_path": "social.relationships",
            "value": {name: val},
            "confidence": fact.get("confidence", 0.5),
        }

    # 把 social.family_structure(.xxx)? 一律迁到 social.relationships
    if path == "social.family_structure" or path.startswith("social.family_structure."):
        val = fact.get("value")
        if path != "social.family_structure":
            key = path[len("social.family_structure."):].strip()
            if key:
                val = {key: val}
        if not isinstance(val, dict):
            return None
        return {
            "dimension_path": "social.relationships",
            "value": val,
            "confidence": fact.get("confidence", 0.5),
        }

    # basic.age / basic.birthday 强类型校验
    if path in ("basic.age", "basic.birthday"):
        new_path, new_val = _coerce_age_birthday(path, fact.get("value"))
        if new_path is None:
            logger.info("丢弃非法 %s value=%r", path, fact.get("value"))
            return None
        return {
            "dimension_path": new_path,
            "value": new_val,
            "confidence": fact.get("confidence", 0.5),
        }

    if path in _ALLOWED_FIELD_PATHS:
        return fact

    # 兜底：截到允许的前缀（如 basic.name.first → basic.name，丢弃多余层级）
    parts = path.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in _ALLOWED_FIELD_PATHS:
            new_fact = dict(fact)
            new_fact["dimension_path"] = candidate
            return new_fact

    logger.warning("丢弃非法 dimension_path: %s", path)
    return None


def _heal_relationships_field(profile: dict) -> bool:
    """修复历史脏数据：
    - 把误写到 relationships 字段对象本身上的人名挪进 value
    - 把 social.family_structure 字段迁到 relationships，再删除
    返回是否做了修改。
    """
    section = profile.get("profile", {}).get("social")
    if not isinstance(section, dict):
        return False
    field = section.setdefault("relationships", {})
    if not isinstance(field, dict):
        field = {}
        section["relationships"] = field

    value = field.get("value")
    if not isinstance(value, dict):
        value = {} if value is None else {}

    changed = False
    # 1) 修字段对象上漏到外层的人名
    for k in list(field.keys()):
        if k in {"value", "confidence", "updated_at"}:
            continue
        v = field.pop(k)
        if isinstance(v, dict) and "value" in v and "confidence" in v:
            value[k] = v["value"]
        else:
            value[k] = v
        changed = True

    # 2) 把 family_structure 迁到 relationships，再删除字段
    fs = section.pop("family_structure", None)
    if isinstance(fs, dict) and isinstance(fs.get("value"), dict):
        for k, v in fs["value"].items():
            if k and k not in value:  # 不覆盖已存在的更详细的关系
                value[k] = v
        changed = True

    if changed or value:
        field["value"] = _normalize_relationships(value)
        field.setdefault("confidence", 0.7)
        field.setdefault("updated_at", _now_iso())
    return changed


def _heal_basic_age_birthday(profile: dict) -> bool:
    """修复历史 basic.age 字段：'83年' 这种应该迁到 birthday；非整数 age 一律重写"""
    basic = profile.get("profile", {}).get("basic")
    if not isinstance(basic, dict):
        return False
    age_field = basic.get("age")
    changed = False
    if isinstance(age_field, dict) and "value" in age_field:
        new_path, new_val = _coerce_age_birthday("basic.age", age_field["value"])
        if new_path is None:
            basic.pop("age", None)
            changed = True
        elif new_path == "basic.birthday":
            if "birthday" not in basic:
                basic["birthday"] = {
                    "value": new_val,
                    "confidence": age_field.get("confidence", 0.9),
                    "updated_at": age_field.get("updated_at", _now_iso()),
                }
            basic.pop("age", None)
            changed = True
        elif age_field["value"] != new_val:
            age_field["value"] = new_val
            changed = True
    return changed


def _heal_profile(profile: dict) -> bool:
    """所有历史脏数据自愈入口"""
    a = _heal_relationships_field(profile)
    b = _heal_basic_age_birthday(profile)
    return a or b


def _normalize_relationships(rels):
    """统一 social.relationships 的格式：
    - 去掉 key 为"用户/我"等指代自己的条目
    - 将 via 指向自己的间接关系扁平化为直系关系（value 改成字符串）
    - 将 via 指向"不存在的 key"的间接关系也扁平化为直系关系
      （LLM 偶尔把"邻居"这种分类词当成 via，但 relationships 里并没有名为"邻居"的人）
    """
    if not isinstance(rels, dict):
        return rels

    # 第一遍：先收集所有合法 key
    valid_keys = {
        str(k).strip()
        for k in rels.keys()
        if k and str(k).strip().lower() not in _SELF_VIA_ALIASES
    }

    cleaned: dict = {}
    for name, val in rels.items():
        if not name or str(name).strip().lower() in _SELF_VIA_ALIASES:
            continue
        if isinstance(val, dict):
            via = val.get("via")
            rel = val.get("rel") or val.get("relation") or val.get("relationship") or ""
            via_str = str(via).strip() if via else ""
            if (
                not via_str
                or via_str.lower() in _SELF_VIA_ALIASES
                or via_str not in valid_keys
                or via_str == str(name).strip()  # 不能指向自己
            ):
                # 降级为直系：rel 文本里若没体现关系，至少保留原 rel 文本
                cleaned[name] = str(rel) if rel else "关系未明"
            else:
                cleaned[name] = {"rel": str(rel), "via": via_str}
        else:
            cleaned[name] = val
    return cleaned


def _smart_merge_relationships(old: dict, new: dict) -> dict:
    """深度合并两份 relationships，避免新值的简短描述覆盖旧值的详细描述。
    策略：
    - key 仅在一方：直接采用
    - 都是字符串：取更长的（信息量更大）
    - 一方是字符串、另一方是 {rel, via}：保留 {rel, via}
    - 都是 {rel, via}：取 rel 更长的，via 优先取新的非空值
    """
    merged: dict = dict(old) if isinstance(old, dict) else {}
    if not isinstance(new, dict):
        return merged

    for k, nv in new.items():
        if k not in merged:
            merged[k] = nv
            continue
        ov = merged[k]
        # 统一抽 rel 文本与可选 via
        def _split(v):
            if isinstance(v, dict):
                return (
                    str(v.get("rel") or v.get("relation") or v.get("relationship") or ""),
                    v.get("via"),
                )
            return (str(v) if v is not None else "", None)

        o_rel, o_via = _split(ov)
        n_rel, n_via = _split(nv)
        rel = o_rel if len(o_rel) >= len(n_rel) else n_rel
        via = n_via or o_via
        if via:
            merged[k] = {"rel": rel, "via": str(via)}
        else:
            merged[k] = rel
    return merged


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
            model=os.getenv("EXTRACT_MODEL", os.getenv("CHAT_MODEL", "qwen3.7-plus")),
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0,
            extra_body={"enable_thinking": False},
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
    # 自愈历史脏数据（relationships 漏到外层、family_structure 残留、age 写成"83年"）
    _heal_profile(profile)

    for raw_fact in facts:
        fact = _coerce_fact_path(raw_fact)
        if fact is None:
            continue
        path = fact["dimension_path"]
        new_val = fact.get("value")
        new_conf = float(fact.get("confidence", 0.5))

        if new_val is None:
            continue

        # social.relationships 在写库前先做格式归一化
        if path == "social.relationships" and isinstance(new_val, dict):
            new_val = _normalize_relationships(new_val)
            if not new_val:
                continue

        old_field = _get_nested(profile, path)

        if old_field is None:
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
            # 关系图：归一化 + 智能合并（不让简短描述覆盖详细描述）
            old_val = old_field.get("value", {})
            if not isinstance(old_val, dict):
                old_val = {}
            old_val = _normalize_relationships(old_val)
            new_val_norm = _normalize_relationships(new_val)
            merged = _smart_merge_relationships(old_val, new_val_norm)
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
