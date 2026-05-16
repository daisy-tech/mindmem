"""JWT + current-user dependency."""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.user import User

JWT_SECRET = os.getenv("JWT_SECRET", "memobot-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    user_id = decode_token(credentials.credentials)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 无效或已过期")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return user


async def get_user_from_token(token: str, db: AsyncSession) -> Optional[User]:
    """Used by SSE GET endpoint where token is a query param."""
    user_id = decode_token(token)
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
