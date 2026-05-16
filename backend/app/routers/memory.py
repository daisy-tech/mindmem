from fastapi import APIRouter, Depends
from app.auth import get_current_user
from app.models.user import User
from app.services.mem0_engine import get_mem0

router = APIRouter()


@router.get("")
async def get_memories(user: User = Depends(get_current_user)):
    return get_mem0().get_all(user.id)


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, user: User = Depends(get_current_user)):
    return get_mem0().delete(memory_id, user.id)
