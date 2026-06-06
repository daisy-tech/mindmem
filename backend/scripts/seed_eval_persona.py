#!/usr/bin/env python3
"""灌库：合成评测用户 persona_a_zhang（老张）。

用法（ECS / 本地 backend 容器内）：
    python3 scripts/seed_eval_persona.py
    python3 scripts/seed_eval_persona.py persona_a_zhang

环境变量（可选）：
    EVAL_USER_ID      固定 UUID，默认 a0000001-0000-4000-8000-000000000001
    EVAL_USER_PHONE   默认 13800000001
    USER_DB_PATH      默认 /app/data/memobot.db
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from app.services.eval_persona import seed_persona  # noqa: E402


def main() -> None:
    persona_ref = sys.argv[1] if len(sys.argv) > 1 else "persona_a_zhang"
    result = asyncio.run(seed_persona(persona_ref))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
