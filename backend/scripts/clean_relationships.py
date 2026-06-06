"""LLM 驱动的 social.relationships 语义清洗。

会做：
- 合并指代同一人的不同 key（如"孩子"和"儿子"）
- 删除内容空泛的条目（"子女"这种没有信息量的描述）
- 尽量从用户最近的对话上下文里补回丢失的细节

默认 dry-run。加 --apply 才写库。

用法：
    docker compose exec backend python3 scripts/clean_relationships.py [phone]
    docker compose exec backend python3 scripts/clean_relationships.py [phone] --apply
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import openai  # noqa: E402

from app.services.profile_engine import _now_iso, _normalize_relationships  # noqa: E402

DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")

CLEAN_PROMPT = """\
你会收到：
1. 当前 relationships 字典（可能有重复 / 描述空泛）
2. 用户最近的对话历史（用于补回细节）

请输出清洗后的 relationships 字典，规则：

【合并 & 去重】
- 同一个人的不同 key 合并，保留更具体的称谓
  例：「孩子」「子女」并入「儿子」；「老婆」并入「妻子」
- key 必须是人物的具体称谓或姓名，禁用「孩子/朋友/同事」等纯分类词

【绝对禁止编造】
- 只能用"用户最近的话"里**明确提到过**的信息
- 严禁推断：比如对话里只说"9岁半"，**不要**写"就读小学三年级"
- 严禁脑补：对话里没说的爱好/职业/地点，一律不写

【via 约束（非常重要）】
- 间接关系 value 是对象 {"rel":"...","via":"中间人名"}
- via 必须是本字典里另一个 key 的名字（一个具体的人）
- ❌ via 不能是「邻居/朋友/同事/家人」这种概念分类词
- ❌ via 不能是「用户/我/本人」等指代自己
- 如果想不到合适的中间人，就写成直系关系（value 直接是字符串）

【格式】
直系关系：value 是字符串
  "妻子": "配偶，全职在家，1983年生"
  "邻居老爷爷": "邻居，80岁，养狗叫可乐"   ← 邻居用直系，不要硬塞 via
间接关系：value 是对象（且 via 必须存在于本字典）
  "小孙孙": {"rel": "儿子的同学，爱听相声", "via": "儿子"}

只输出 JSON 对象，不要其他文字。"""


def pick_user(con, phone):
    if phone:
        row = con.execute("SELECT id, phone FROM users WHERE phone=?", (phone,)).fetchone()
    else:
        row = con.execute(
            "SELECT id, phone FROM users ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("找不到用户")
    return row


def recent_user_messages(con, uid, n=30):
    rows = con.execute(
        "SELECT messages_json FROM conversations WHERE user_id=? "
        "ORDER BY updated_at DESC LIMIT 5",
        (uid,),
    ).fetchall()
    msgs = []
    for (mj,) in rows:
        for m in json.loads(mj or "[]"):
            if m.get("role") == "user":
                msgs.append(m.get("content", ""))
    return msgs[:n]


def llm_clean(rel_dict: dict, user_msgs: list[str], model: str) -> dict:
    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    payload = (
        "当前 relationships:\n"
        + json.dumps(rel_dict, ensure_ascii=False, indent=2)
        + "\n\n用户最近的话（这是唯一可参考的事实来源，没说过的细节一律不要写）:\n"
        + "\n".join(f"- {m}" for m in user_msgs)
        + "\n\n请输出清洗后的 JSON 对象："
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CLEAN_PROMPT},
            {"role": "user", "content": payload},
        ],
        temperature=0,
        extra_body={"enable_thinking": False},
    )
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def _parse_args(argv):
    """支持: [phone] [--apply] [--model qwen-xxx | --model=qwen-xxx]"""
    apply = False
    model = "qwen-max"  # 关键修复用强模型，降低编造概率
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--apply":
            apply = True
        elif a == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 1
        elif a.startswith("--model="):
            model = a.split("=", 1)[1]
        else:
            positional.append(a)
        i += 1
    phone = positional[0] if positional else None
    return phone, apply, model


def main():
    phone, apply, model = _parse_args([a for a in sys.argv[1:] if a])

    con = sqlite3.connect(DB_PATH)
    uid, uphone = pick_user(con, phone)
    print(f"用户 phone={uphone}  uid={uid}")
    print(f"模型：{model}")
    print(f"模式：{'APPLY (写库)' if apply else 'DRY-RUN (仅预览)'}\n")

    row = con.execute(
        "SELECT profile_json FROM user_profiles WHERE user_id=?", (uid,)
    ).fetchone()
    if not row:
        print("无画像")
        return
    profile = json.loads(row[0] or "{}")
    rel_field = (
        profile.get("profile", {}).get("social", {}).get("relationships")
    )
    if not isinstance(rel_field, dict) or not isinstance(rel_field.get("value"), dict):
        print("无 relationships.value")
        return

    before = rel_field["value"]
    print("【清洗前】")
    print(json.dumps(before, ensure_ascii=False, indent=2))

    msgs = recent_user_messages(con, uid)
    print(f"\n→ 调用 {model} 清洗（参考 {len(msgs)} 条用户最近对话）…")
    cleaned = llm_clean(before, msgs, model)
    cleaned = _normalize_relationships(cleaned)

    print("\n【清洗后】")
    print(json.dumps(cleaned, ensure_ascii=False, indent=2))

    if not apply:
        print("\n[DRY-RUN] 未写库。确认无误后加 --apply 执行。")
        con.close()
        return

    rel_field["value"] = cleaned
    rel_field["updated_at"] = _now_iso()
    profile["last_updated"] = _now_iso()
    cur = con.cursor()
    cur.execute(
        "UPDATE user_profiles SET profile_json=?, last_updated=datetime('now') WHERE user_id=?",
        (json.dumps(profile, ensure_ascii=False), uid),
    )
    con.commit()
    con.close()
    print("\n✅ 已写库")


if __name__ == "__main__":
    main()
