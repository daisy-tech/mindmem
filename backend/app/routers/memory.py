from fastapi import APIRouter
from app.services.mem0_engine import Mem0Engine

router = APIRouter()
mem0 = Mem0Engine()


@router.get("/{user_id}")
async def get_memories(user_id: str):
    return mem0.get_all(user_id)


@router.delete("/{user_id}/{memory_id}")
async def delete_memory(user_id: str, memory_id: str):
    return mem0.delete(memory_id, user_id)
