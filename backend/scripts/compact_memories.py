"""一次性压缩 Mem0 中的冗余/啰嗦记忆。

策略：
1. 拉取某用户所有记忆
2. 把所有记忆文本拼成清单交给 LLM 去重 + 改写为干净简洁的版本
3. 默认 dry-run，加 --apply 才真的执行：删除旧的、写入新的

用法：
    docker compose exec backend python3 scripts/compact_memories.py [phone]
    docker compose exec backend python3 scripts/compact_memories.py [phone] --apply
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
from celery_worker import _strip_subject_prefix  # noqa: E402

DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")

COMPACT_PROMPT = """\
你会收到一组"原始记忆条目"（可能有重复、啰嗦、含噪音元数据）。
请把它们重写成一组高质量的简洁记忆，规则：

1. 合并明显重复或语义相同的条目（如多条"邻居80岁老爷爷养狗叫可乐"只保留 1 条）
2. 每条记忆是一句客观陈述，最长 60 字，**不要带主语**（不要"用户/dxj/他"等开头）
   ❌ "用户dxj定居北京昌平" / "dxj定居北京昌平" / "他定居北京昌平"
   ✅ "定居北京昌平，每天去海淀上班"
3. 删除所有噪音元数据：
   - 任何人名/用户名作为主语
   - "于2026年5月23日" 等具体日期
   - "该信息于...向MemoBot透露"、"向系统表达"
   - "反映了..."、"表明..."、"体现出..."
4. 保留所有具体细节（人名、年龄、地点、习惯等）
5. 同一主题的多条小事实合并成一条更具体的（如"住北京昌平"+"在海淀上班"+"做IT" → 一条）
6. 按"基本信息 > 家庭/关系 > 工作 > 生活/兴趣 > 情绪/痛点"的顺序排列

只输出 JSON 字符串数组，不要其他文字。
"""


def pick_user(con: sqlite3.Connection, phone: str | None):
    if phone:
        row = con.execute("SELECT id, phone FROM users WHERE phone=?", (phone,)).fetchone()
    else:
        row = con.execute(
            "SELECT id, phone FROM users ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("找不到用户")
    return row


def llm_compact(texts: list[str], model: str) -> list[str]:
    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": COMPACT_PROMPT},
            {"role": "user", "content": f"原始记忆条目：\n{numbered}\n\n请输出精简后的 JSON 字符串数组："},
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
    """[phone] [--apply] [--model qwen-xxx]"""
    apply = False
    model = "qwen-max"
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
    return (positional[0] if positional else None), apply, model


def main():
    phone, apply, model = _parse_args([a for a in sys.argv[1:] if a])

    con = sqlite3.connect(DB_PATH)
    try:
        uid, uphone = pick_user(con, phone)
    finally:
        con.close()

    print(f"用户 phone={uphone}  uid={uid}")
    print(f"模型：{model}")
    print(f"模式：{'APPLY (真的清理)' if apply else 'DRY-RUN (仅预览)'}\n")

    from app.services.mem0_engine import get_mem0
    engine = get_mem0()

    data = engine.get_all(uid)
    items = data.get("results", data.get("memories", []))
    print(f"当前记忆条数：{len(items)}\n")
    if not items:
        return

    texts = [(it["id"], (it.get("memory") or "").strip()) for it in items]
    raw_texts = [t for _, t in texts if t]

    print(f"→ 调用 {model} 压缩…")
    compacted = llm_compact(raw_texts, model)
    print(f"\n精简后条数：{len(compacted)}\n")
    print("=" * 60)
    for i, t in enumerate(compacted, 1):
        print(f"  {i}. {t}")
    print("=" * 60)

    if not apply:
        print("\n[DRY-RUN] 未做任何写入。确认效果后重新运行加 --apply 真正执行。")
        return

    print("\n→ 删除旧记忆…")
    for mid, _ in texts:
        try:
            engine.delete(mid, uid)
        except Exception as e:
            print(f"  删除 {mid[:8]} 失败: {e}")
    print(f"  已删除 {len(texts)} 条")

    print("→ 写入精简后记忆…")
    written = 0
    for t in compacted:
        clean = _strip_subject_prefix((t or "").strip())
        if clean:
            engine.add(clean, user_id=uid, infer=False)
            written += 1
    print(f"  已写入 {written} 条")

    print("\n✅ 完成")


if __name__ == "__main__":
    main()
