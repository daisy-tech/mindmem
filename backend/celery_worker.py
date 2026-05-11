import os
from celery import Celery
from app.services.mem0_engine import Mem0Engine

celery_app = Celery(
    "memobot",
    broker=os.getenv("REDIS_URL", "redis://redis:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://redis:6379/0"),
)

# Single shared engine per worker process (avoid re-initializing per task)
_engine: Mem0Engine | None = None


def _get_engine() -> Mem0Engine:
    global _engine
    if _engine is None:
        _engine = Mem0Engine()
    return _engine


@celery_app.task
def extract_and_store_memory(user_id: str, messages: list):
    """对话结束后，让 Mem0 自动提取记忆"""
    _get_engine().add(messages, user_id=user_id)
