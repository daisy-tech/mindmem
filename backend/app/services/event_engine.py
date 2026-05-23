"""
事件记忆引擎：提取、去重、检索、注入上下文。
"""
import json
import logging
import os
import random
import string
from datetime import datetime, timezone

import openai

logger = logging.getLogger(__name__)

EVENT_TYPES = {"plan", "experience", "achievement", "pain_point", "feedback", "status_change"}

TYPE_LABELS = {
    "plan": "计划",
    "experience": "经历",
    "achievement": "成就",
    "pain_point": "困扰",
    "feedback": "反馈",
    "status_change": "状态变化",
}

TYPE_COLORS = {
    "plan": "#409eff",
    "experience": "#67c23a",
    "achievement": "#e6a23c",
    "pain_point": "#f56c6c",
    "feedback": "#909399",
    "status_change": "#a78bfa",
}

EXTRACT_PROMPT = """\
你是一个事件提取模块。当前日期：{current_date}。
从以下对话历史中，只提取用户明确陈述或强烈暗示的个人事件。

要求：
1. 只提取用户消息中的事件，不要提取 AI 助手的话。
2. 每条事件包含：summary, event_type, occurred_at, importance, details。
3. event_type 只能是以下之一：plan / experience / achievement / pain_point / feedback / status_change。
4. occurred_at 若用户使用相对时间（"昨天""下周三"），根据当前日期转换为绝对日期（YYYY-MM-DD）。
   若完全无法确定，填 null。
5. 同一事件被多次提及，只输出一条，在 details 中加 "mention_count": N。
6. 若无可提取事件，输出 []。

重要性参考：
- 计划（有截止日期/重要场合）: 0.9
- 成就/里程碑: 0.8
- 负面反馈/纠正: 0.8
- 健康/情绪痛点: 0.7
- 一般经历: 0.5
- 兴趣提及: 0.4

对话历史：
{history}

只输出 JSON 数组，不要有其他文字。示例：
[{{"summary":"用户计划下周五去上海出差","event_type":"plan","occurred_at":"2026-05-29","importance":0.9,"details":{{"destination":"上海"}}}}]"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_event_id() -> str:
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"evt_{ts}_{rand}"


def extract_events_from_conversation(messages: list, current_date: str | None = None) -> list[dict]:
    """调用 LLM 从对话中提取事件列表"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return []

    if current_date is None:
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history_text = "\n".join(
        f"[{'用户' if m['role'] == 'user' else 'AI'}]: {m['content']}"
        for m in messages
        if m.get("role") in ("user", "assistant")
    )

    client = openai.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    try:
        resp = client.chat.completions.create(
            model=os.getenv("EXTRACT_MODEL", os.getenv("CHAT_MODEL", "qwen-turbo")),
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT.format(
                    current_date=current_date, history=history_text
                )},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        events = json.loads(raw)
        return [e for e in events if isinstance(e, dict) and e.get("event_type") in EVENT_TYPES]
    except Exception as e:
        logger.warning("extract_events_from_conversation failed: %s", e)
        return []


def format_events_for_prompt(events: list[dict]) -> str:
    """将事件列表格式化为系统提示中的文本块"""
    if not events:
        return ""
    lines = []
    for e in events:
        date_str = f"[{e.get('occurred_at', '日期不明')}]" if e.get("occurred_at") else "[日期不明]"
        type_label = TYPE_LABELS.get(e.get("event_type", ""), e.get("event_type", ""))
        lines.append(f"- {date_str} ({type_label}) {e.get('summary', '')}")
    return "\n".join(lines)
