"""一次性修复历史脏数据：
- 把误写到 social.relationships 字段对象上的人名挪回 value
- 顺便把 via 指向"用户/我/本人" 的间接关系扁平化为直系

用法：docker compose exec backend python3 scripts/fix_dirty_profile.py
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

from app.services.profile_engine import (  # noqa: E402
    _heal_profile,
    _normalize_relationships,
)

DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute("SELECT user_id, profile_json FROM user_profiles").fetchall()
    fixed = 0
    for uid, pj in rows:
        profile = json.loads(pj or "{}")
        before = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        _heal_profile(profile)
        # 再跑一遍归一化（清掉残留的 via:用户）
        rel = (
            profile.get("profile", {}).get("social", {}).get("relationships")
        )
        if isinstance(rel, dict) and isinstance(rel.get("value"), dict):
            rel["value"] = _normalize_relationships(rel["value"])
        after = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        if before != after:
            cur.execute(
                "UPDATE user_profiles SET profile_json=? WHERE user_id=?",
                (json.dumps(profile, ensure_ascii=False), uid),
            )
            print(f"✔ fixed user={uid}")
            new_rel = (
                profile.get("profile", {})
                .get("social", {})
                .get("relationships", {})
            )
            print("  now relationships =")
            print(
                "  " + json.dumps(new_rel, ensure_ascii=False, indent=2).replace("\n", "\n  ")
            )
            fixed += 1
    con.commit()
    con.close()
    print(f"\n完成。共修复 {fixed} 个用户。")


if __name__ == "__main__":
    main()
