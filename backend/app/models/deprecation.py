"""记忆软删/纠正流水表。

用户用 correction intent 主动纠错时，三层记忆（episodic / event / profile）
都不做硬删，统一写入这张表，build_context 时过滤：
- episodic：按 ref_id 过滤 mem0 返回结果
- event：同步把 user_events.status 置为 'deprecated'
- profile：同步在 profile JSON 里把字段值清空 + 记审计

写入也作为审计——`restored_at IS NULL` 即"当前处于停用"。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MemoryDeprecation(Base):
    __tablename__ = "memory_deprecations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(String(16))  # episodic / event / profile
    ref_id: Mapped[str] = mapped_column(String(128), index=True)  # mem_id / event_id / profile path
    original_text: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    correction_conversation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    correction_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    action: Mapped[str] = mapped_column(String(16), default="deprecate")  # deprecate / update / audit_only
    new_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    deprecated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
