"""SQLAlchemy async DB setup for user accounts."""
import os
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DB_PATH = os.getenv("USER_DB_PATH", "/app/data/memobot.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session


async def init_db():
    # Import models so metadata is populated before create_all
    from app.models import (  # noqa: F401
        user,
        profile,
        conversation,
        event,
        deprecation,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 迁移：为旧数据库添加 action 列（列已存在时忽略错误）
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE memory_audit_log ADD COLUMN action VARCHAR(32) DEFAULT ''"
            )
        except Exception:
            pass  # 列已存在，忽略
