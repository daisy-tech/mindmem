"""线上聊天评估结果落盘 / 读盘工具。

目录布局（容器内）::

    /app/eval/exports/reviews/{user_id}/{conv_id}.json   # 覆盖式，仅保留最新

容器内的 `/app/eval/exports` 通过 `./backend:/app` bind mount 映射到 host 上
``backend/eval/exports``，再经 NFS 共享到 mac，从而 mac 端、ECS 端、容器内
看到的都是同一份文件，无需"浏览器下载 + scp 上传"的来回搬运。

落盘内容就是 `review_audit_pack(...)` 的完整输出（chat_audit_v1 pack +
review summary），可以直接被前端反序列化呈现，无需再次评估。

设计原则：
- **覆盖式存储**：每个 (user_id, conv_id) 只保留最新，简单稳定。如需历史
  对比，用 git。
- **不落 LLM 调用**：写盘只 dump JSON。读盘直接返回，O(1)。
- **目录懒创建**：用户首次保存时再建 `{user_id}/`，避免在 git 留空目录。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _default_root() -> Path:
    # 默认放容器内 /app/eval/exports/reviews，对应 host backend/eval/exports/reviews
    return Path(os.getenv("EVAL_CHAT_REVIEWS_DIR", "/app/eval/exports/reviews"))


REVIEWS_DIR: Path = _default_root()


def _safe_segment(segment: str) -> str:
    """挡掉路径穿越与奇怪字符；只允许字母数字和 _-。

    严格模式：清洗结果与原值必须一致，否则 raise。避免不同 ID 被清洗成
    同一个 key 导致跨记录覆盖（例如 "a.b" 和 "ab" 都会变成 "ab"）。
    """
    if not segment:
        raise ValueError("empty id segment")
    clean = "".join(c for c in segment if c.isalnum() or c in "_-")
    if not clean or clean != segment:
        raise ValueError(f"invalid id segment: {segment!r}")
    return clean


def _user_dir(user_id: str) -> Path:
    return REVIEWS_DIR / _safe_segment(user_id)


def review_path(user_id: str, conv_id: str) -> Path:
    return _user_dir(user_id) / f"{_safe_segment(conv_id)}.json"


def save_review(user_id: str, conv_id: str, pack: dict[str, Any]) -> Path:
    """落盘评估结果（带 review）。

    用临时文件 + os.replace 做原子写入，避免并发评估同一 conv 写半截。
    显式 chmod 0644 / 0755：容器以 root 跑时默认写出 0600 文件，NFS 共享后
    mac 端非 root 用户无法读取（连 sudo 都被 NFS 协议层挡掉）。
    """
    path = review_path(user_id, conv_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o755)
    except OSError:
        pass  # NFS 已有目录权限不可改不致命
    # 附加落盘时间戳（独立于 pack.exported_at，便于"最近一次评估"展示）
    enriched = dict(pack)
    enriched["evaluated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    fd, tmp = tempfile.mkstemp(prefix=f".{conv_id}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)
        except OSError as e:
            logger.warning("chmod 0644 on %s failed: %s", path, e)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    logger.info("eval review saved: %s", path)
    return path


def load_review(user_id: str, conv_id: str) -> dict[str, Any] | None:
    """读盘已落盘的评估包，没有则返回 None。"""
    path = review_path(user_id, conv_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # 文件损坏不应阻塞前端，直接当 miss
        logger.warning("eval review %s read failed: %s", path, e)
        return None


def delete_review(user_id: str, conv_id: str) -> bool:
    """删除已存评估，返回是否实际删了。"""
    path = review_path(user_id, conv_id)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        logger.warning("eval review %s unlink failed: %s", path, e)
        return False


def _summarize(pack: dict[str, Any]) -> dict[str, Any]:
    """从 pack 抽出列表展示所需的轻量字段（不返回完整 pack 给列表 API）。"""
    review = pack.get("review") or {}
    counters = review.get("counters") or {}
    return {
        "evaluated_at": pack.get("evaluated_at"),
        "exported_at": pack.get("exported_at"),
        "turns_total": (pack.get("summary") or {}).get("turns_total"),
        "evaluable_turns": review.get("evaluable_turns"),
        "final_ok_rate": review.get("final_ok_rate"),
        "counters": {
            "final_ok": int(counters.get("final_ok") or 0),
            "final_suspicious": int(counters.get("final_suspicious") or 0),
            "final_bad": int(counters.get("final_bad") or 0),
            "turns_skipped": int(counters.get("turns_skipped") or 0),
        },
    }


def list_stored(user_id: str) -> list[dict[str, Any]]:
    """列出该用户已落盘的所有评估摘要（用于左侧会话列表标记）。"""
    root = _user_dir(user_id)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for p in root.iterdir():
        if p.suffix != ".json" or p.name.startswith("."):
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                pack = json.load(f)
        except Exception as e:
            logger.warning("skip corrupt review %s: %s", p, e)
            continue
        item = _summarize(pack)
        item["conversation_id"] = p.stem
        items.append(item)
    items.sort(key=lambda x: x.get("evaluated_at") or "", reverse=True)
    return items


def list_stored_for_ids(user_id: str, conv_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """按指定 conv_ids 直查已存摘要，避免遍历整个目录。

    适合 UI 已知会话列表后只查这些 id 的状态。
    """
    out: dict[str, dict[str, Any]] = {}
    for cid in conv_ids:
        try:
            path = review_path(user_id, cid)
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                pack = json.load(f)
        except Exception as e:
            logger.warning("skip corrupt review %s: %s", path, e)
            continue
        out[cid] = _summarize(pack)
    return out
