from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db
from app.routers import auth, chat, memory, profile, conversations, events, eval

app = FastAPI(title="MemoBot API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    await init_db()


app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
app.include_router(profile.router, prefix="/api/profile", tags=["profile"])
app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
app.include_router(eval.router, prefix="/api/eval", tags=["eval"])


@app.get("/health")
async def health():
    return {"status": "ok"}
