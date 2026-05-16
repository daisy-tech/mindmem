"""手机号登录路由 (MVP: 验证码通过 Redis 存储, dev 模式下直接返回)."""
import os
import re
import random
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.db import get_db
from app.models.user import User
from app.auth import create_access_token, get_current_user

router = APIRouter()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DEV_MODE = os.getenv("DEV_MODE", "true").lower() == "true"
SMS_CODE_TTL = 300  # 5min
SMS_COOLDOWN = 60   # 1min 防刷

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


class SendCodeReq(BaseModel):
    phone: str = Field(..., description="11位手机号")


class LoginReq(BaseModel):
    phone: str
    code: str


class UserOut(BaseModel):
    id: str
    phone: str
    nickname: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/phone/send-code")
async def send_code(req: SendCodeReq):
    if not PHONE_RE.match(req.phone):
        raise HTTPException(400, "手机号格式不正确")

    r = await get_redis()
    cd_key = f"sms:cd:{req.phone}"
    if await r.get(cd_key):
        raise HTTPException(429, "请求过于频繁，请稍后再试")

    code = f"{random.randint(0, 999999):06d}"
    await r.setex(f"sms:code:{req.phone}", SMS_CODE_TTL, code)
    await r.setex(cd_key, SMS_COOLDOWN, "1")

    # 真实环境下这里应调用阿里云短信等服务发送 code
    print(f"[SMS] phone={req.phone} code={code}")

    resp = {"ok": True, "expire_in": SMS_CODE_TTL}
    if DEV_MODE:
        resp["dev_code"] = code
    return resp


@router.post("/phone/login")
async def phone_login(req: LoginReq, db: AsyncSession = Depends(get_db)):
    if not PHONE_RE.match(req.phone):
        raise HTTPException(400, "手机号格式不正确")

    r = await get_redis()
    key = f"sms:code:{req.phone}"
    stored = await r.get(key)
    if not stored:
        raise HTTPException(400, "验证码已过期，请重新获取")
    if stored != req.code:
        raise HTTPException(400, "验证码不正确")
    await r.delete(key)

    # find or create user
    result = await db.execute(select(User).where(User.phone == req.phone))
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            id=str(uuid.uuid4()),
            phone=req.phone,
            nickname=f"用户{req.phone[-4:]}",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    token = create_access_token(user.id)
    return {"token": token, "user": UserOut.model_validate(user)}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
