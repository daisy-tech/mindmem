"""回放三份真实聊天的评估，对比修复前后差异。

用法：
    python scripts/replay_personality_eval.py <eval_review.json> ...

读取每份 *_eval_review.json，从中抽出 chat_audit pack，重跑当前 v1.2.x 评估，
然后输出每份的 final_status 分布 + personality_signature 规则命中情况，
便于对比"修复段标题假阳性 + 加性格契约 + 加 personality_signature 规则"前后的差异。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 强制无 DB 桩注入（评估纯 audit 包，不需要 DB）
os.environ.setdefault("USER_DB_PATH", str(ROOT / "data" / "memobot.db"))

from app.services.eval_chat_review import review_audit_pack  # noqa: E402


def _audit_pack_from_review(review_path: Path) -> dict:
    """从 *_eval_review.json 里抽出 chat_audit 部分（去掉旧 review 字段）。"""
    with review_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    pack = {
        "version": "chat_audit_v1",
        "conversation_id": data.get("conversation_id"),
        "user_id": data.get("user_id"),
        "turns": [],
    }
    for t in data.get("turns") or []:
        pack["turns"].append({
            "turn_id": t.get("turn_id"),
            "input": t.get("input"),
            "output": t.get("output"),
            "audit": t.get("audit"),
        })
    return pack


def _summarize(pack: dict) -> dict:
    """跑评估后聚合：final_status 分布 + personality_signature 规则统计。"""
    enriched = review_audit_pack(pack)
    fs_count: dict[str, int] = {}
    psig_count: dict[str, int] = {}
    psig_examples: list[dict] = []
    other_failures: list[dict] = []
    for turn in enriched.get("turns") or []:
        review = turn.get("review") or {}
        fs = review.get("final_status") or "unknown"
        fs_count[fs] = fs_count.get(fs, 0) + 1
        for rule in review.get("rules") or []:
            if rule.get("id") == "personality_signature":
                status = rule.get("status") or "unknown"
                psig_count[status] = psig_count.get(status, 0) + 1
                if status == "fail" and len(psig_examples) < 3:
                    psig_examples.append({
                        "turn": (turn.get("turn_id") or "")[-6:],
                        "intent": (turn.get("audit", {}).get("prompt_meta", {})
                                   .get("route", {}).get("intent")),
                        "reply": (turn.get("output", {}).get("assistant_reply") or "")[:60],
                        "detail": rule.get("detail"),
                    })
            elif rule.get("status") == "fail":
                other_failures.append({
                    "turn": (turn.get("turn_id") or "")[-6:],
                    "rule": rule.get("id"),
                    "detail": (rule.get("detail") or "")[:80],
                })
    return {
        "personality": (enriched.get("turns", [{}])[0].get("audit", {})
                        .get("prompt_meta", {}).get("route", {}).get("personality")
                        if enriched.get("turns") else None),
        "total_turns": len(enriched.get("turns") or []),
        "final_status": fs_count,
        "personality_signature": psig_count,
        "personality_signature_fail_examples": psig_examples,
        "other_l1_failures": other_failures[:10],
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python replay_personality_eval.py <file1.json> [file2.json ...]")
        return 1
    rows = []
    for path_str in sys.argv[1:]:
        path = Path(path_str)
        if not path.exists():
            print(f"⚠️  跳过：{path} 不存在")
            continue
        pack = _audit_pack_from_review(path)
        summary = _summarize(pack)
        summary["file"] = path.name
        rows.append(summary)

    print("=" * 90)
    print(f"{'文件':<48} {'人格':<10} {'共轮':<5} {'final_status':<35}")
    print("-" * 90)
    for r in rows:
        fs_str = " ".join(f"{k}:{v}" for k, v in r["final_status"].items())
        print(f"{r['file'][:46]:<48} {(r['personality'] or '?'):<10} {r['total_turns']:<5} {fs_str}")

    print()
    print("=" * 90)
    print("personality_signature 规则结果")
    print("-" * 90)
    for r in rows:
        psig = r["personality_signature"]
        psig_str = " ".join(f"{k}:{v}" for k, v in psig.items())
        print(f"  {r['file'][:60]}")
        print(f"    {r['personality']}: {psig_str}")
        for ex in r["personality_signature_fail_examples"]:
            print(f"      ❌ turn={ex['turn']} intent={ex['intent']}")
            print(f"         reply: {ex['reply']}")
            print(f"         why  : {ex['detail']}")

    print()
    print("=" * 90)
    print("其它 L1 规则失败（前 10 条）")
    print("-" * 90)
    for r in rows:
        if not r["other_l1_failures"]:
            print(f"  {r['file'][:60]}：无")
            continue
        print(f"  {r['file'][:60]}")
        for f in r["other_l1_failures"]:
            print(f"    turn={f['turn']} rule={f['rule']:<28} {f['detail']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
