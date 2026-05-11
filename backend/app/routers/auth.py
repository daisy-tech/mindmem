from fastapi import APIRouter

router = APIRouter()


@router.post("/login")
async def login():
    # MVP 阶段简化，实际应验证用户名密码并返回 JWT
    return {"token": "demo-token", "user_id": "user_001"}


@router.post("/register")
async def register():
    return {"token": "demo-token", "user_id": "user_001"}
