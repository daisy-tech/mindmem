import json
import logging

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models.user import User
from app.services.eval_runner import (
    DRAFTS_DIR,
    REPORTS_DIR,
    delete_draft,
    fail_eval_job,
    get_job_state,
    list_drafts,
    list_reports,
    load_draft,
    load_report,
    prepare_eval_job,
    run_eval_job,
    run_single_query,
    save_draft,
    _load_suite,
)
from app.services.eval_persona import (
    get_persona_data_sync,
    resolve_eval_user_id,
    seed_persona_sync,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class StartEvalRequest(BaseModel):
    run_type: Literal["smoke", "full"] = "smoke"
    run_chat: bool = False


class ImportReportRequest(BaseModel):
    report: dict


class QueryHistoryMessage(BaseModel):
    role: str
    content: str


class QueryExpect(BaseModel):
    intent: str | None = None
    optional_intents: list[str] | None = None
    must_activate_keywords: list[str] | None = None
    forbidden_phrases_in_reply: list[str] | None = None
    forbidden_phrases_in_system: list[str] | None = None


class SingleQueryRequest(BaseModel):
    message: str = Field(..., min_length=1)
    history: list[QueryHistoryMessage] = Field(default_factory=list)
    personality: Literal["introvert", "balanced", "extrovert"] | None = None
    run_chat: bool = False
    expect: QueryExpect | None = None


class SaveDraftRequest(BaseModel):
    draft_id: str | None = None
    title: str | None = None
    input: dict
    result: dict = Field(default_factory=dict)


async def _run_eval_background(
    run_type: str, run_chat: bool, run_id: str, triggered_by_user_id: str
):
    from app.db import SessionLocal

    async with SessionLocal() as db:
        try:
            suite = _load_suite(run_type)
            persona_ref = suite.get("persona_ref") or "persona_a_zhang"
            eval_user_id = await resolve_eval_user_id(db)
            await run_eval_job(
                eval_user_id=eval_user_id,
                db=db,
                run_type=run_type,
                run_chat=run_chat,
                run_id=run_id,
                persona_ref=persona_ref,
                triggered_by_user_id=triggered_by_user_id,
            )
        except Exception as e:
            logger.exception("background eval failed: %s", e)
            if not get_job_state().get("error"):
                fail_eval_job(str(e))


@router.get("/status")
async def eval_status(user: User = Depends(get_current_user)):
    return get_job_state()


@router.get("/runs")
async def eval_list_runs(user: User = Depends(get_current_user)):
    return {"runs": list_reports()}


@router.get("/runs/{run_id}")
async def eval_get_run(run_id: str, user: User = Depends(get_current_user)):
    try:
        return load_report(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "报告不存在")


@router.post("/runs")
async def eval_start_run(
    body: StartEvalRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    try:
        run_id, total = prepare_eval_job(body.run_type)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(
            500,
            f"评测 case 文件缺失，请确认 backend/eval/smoke_cases.json 已部署: {e}",
        )
    except Exception as e:
        logger.exception("prepare eval failed")
        raise HTTPException(500, f"启动评测失败: {e}")

    background_tasks.add_task(
        _run_eval_background,
        body.run_type,
        body.run_chat,
        run_id,
        user.id,
    )
    suite = _load_suite(body.run_type)
    return {
        "ok": True,
        "run_id": run_id,
        "total": total,
        "message": "评测已开始",
        "run_type": body.run_type,
        "run_chat": body.run_chat,
        "persona_ref": suite.get("persona_ref") or "persona_a_zhang",
    }


@router.post("/runs/import")
async def eval_import_report(
    body: ImportReportRequest,
    user: User = Depends(get_current_user),
):
    report = body.report
    if not isinstance(report, dict) or not report:
        raise HTTPException(400, "report 不能为空")

    run_id = report.get("run_id") or f"import_{user.id[:8]}"
    run_id = "".join(c for c in str(run_id) if c.isalnum() or c in "_-")
    dest = REPORTS_DIR / run_id
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return {"ok": True, "run_id": run_id}


@router.get("/persona/data")
async def eval_get_persona_data(user: User = Depends(get_current_user)):
    """查看 eval persona（老张）当前库内原始数据。"""
    import asyncio

    try:
        return await asyncio.to_thread(get_persona_data_sync, "persona_a_zhang")
    except Exception as e:
        logger.exception("get persona data failed")
        raise HTTPException(500, f"读取失败: {e}")


@router.post("/persona/seed")
async def eval_seed_persona(user: User = Depends(get_current_user)):
    """手动灌库：重置合成用户 A（persona_a_zhang）四层记忆。"""
    import asyncio

    try:
        result = await asyncio.to_thread(seed_persona_sync, "persona_a_zhang")
        return result
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.exception("seed persona failed")
        raise HTTPException(500, f"灌库失败: {e}")


@router.post("/query")
async def eval_single_query(
    body: SingleQueryRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """单条 query 调试：立即返回 prompt_meta，可选 Chat 回复。"""
    try:
        history = [m.model_dump() for m in body.history]
        expect = body.expect.model_dump(exclude_none=True) if body.expect else None
        return await run_single_query(
            user_id=user.id,
            db=db,
            message=body.message.strip(),
            history=history,
            personality=body.personality,
            run_chat=body.run_chat,
            expect=expect,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("single query failed")
        raise HTTPException(500, f"运行失败: {e}")


@router.get("/drafts")
async def eval_list_drafts(user: User = Depends(get_current_user)):
    return {"drafts": list_drafts(user.id)}


@router.get("/drafts/{draft_id}")
async def eval_get_draft(draft_id: str, user: User = Depends(get_current_user)):
    try:
        return load_draft(user.id, draft_id)
    except FileNotFoundError:
        raise HTTPException(404, "草稿不存在")


@router.post("/drafts")
async def eval_save_draft(
    body: SaveDraftRequest,
    user: User = Depends(get_current_user),
):
    if not body.input.get("message"):
        raise HTTPException(400, "input.message 不能为空")
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    return save_draft(
        user.id,
        {
            "draft_id": body.draft_id,
            "title": body.title,
            "input": body.input,
            "result": body.result,
        },
    )


@router.delete("/drafts/{draft_id}")
async def eval_delete_draft(
    draft_id: str,
    user: User = Depends(get_current_user),
):
    if not delete_draft(user.id, draft_id):
        raise HTTPException(404, "草稿不存在")
    return {"ok": True}
