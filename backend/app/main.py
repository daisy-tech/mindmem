from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import chat, memory, auth

app = FastAPI(title="MemoBot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(memory.router, prefix="/api/memory", tags=["memory"])


@app.get("/health")
async def health():
    return {"status": "ok"}
