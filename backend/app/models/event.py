from typing import Optional
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Float, Integer, String, Text
from app.db import Base


class UserEvent(Base):
    __tablename__ = "user_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)  # plan/experience/achievement/pain_point/feedback/status_change
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    related_json: Mapped[str] = mapped_column(Text, default="[]")
    occurred_at: Mapped[Optional[str]] = mapped_column(String(10))       # YYYY-MM-DD, nullable
    detected_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_referenced_at: Mapped[Optional[str]] = mapped_column(String(32))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="active")    # active/superseded/expired/archived
    mention_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
