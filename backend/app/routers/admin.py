"""
admin.html 이 호출하는 모든 엔드포인트 (prefix 없음).
/stats, /crawl, /crawl/status, /crawl/stop, /logs,
/disclosures, /settings, /scheduler/daily,
/gpu-vm/*, /dart/quota
"""

import math
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func as sa_func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.models import Disclosure, DocumentChunk
from ..config import settings as cfg
from ..services import (
    crawl_service,
    log_service,
    quota_service,
    settings_service,
)
from ..services.gpu_vm_service import GpuVmService

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


# ──────────────────────────────────────────────
# GPU VM (lazy singleton)
# ──────────────────────────────────────────────

def _gpu_vm() -> GpuVmService:
    return GpuVmService(cfg.gcp_project_id, cfg.gcp_zone, cfg.gcp_instance_name)


# ══════════════════════════════════════════════
# 통계
# ══════════════════════════════════════════════

@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    disc_cnt = await db.scalar(select(sa_func.count(Disclosure.id)))
    chunk_cnt = await db.scalar(select(sa_func.count(DocumentChunk.id)))
    last_dt = await db.scalar(select(sa_func.max(Disclosure.created_at)))
    return {
        "total_disclosures": disc_cnt or 0,
        "total_documents": chunk_cnt or 0,
        "last_updated": last_dt.isoformat() if last_dt else None,
    }


# ══════════════════════════════════════════════
# 크롤링
# ══════════════════════════════════════════════

@router.post("/crawl")
async def start_crawl(body: dict = Body(...)):
    mode = body.get("mode", "full")
    if crawl_service.get_status()["running"]:
        raise HTTPException(409, "이미 크롤링이 실행 중입니다")

    await crawl_service.start_crawl(
        mode=mode,
        start_date=body.get("start_date"),
        end_date=body.get("end_date"),
        report_nm=body.get("report_nm"),
        pblntf_ty_list=body.get("pblntf_ty_list"),
        dart_key=cfg.dart_api_key,
        gemini_key=cfg.gemini_api_key,
        gcs_bucket=cfg.gcs_bucket_name,
        gcs_creds=cfg.gcs_credentials_path,
        gemini_model=cfg.gemini_model,
        embedding_model=cfg.embedding_model,
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
    )
    return {"message": f"{mode} 크롤링 시작"}


@router.get("/crawl/status")
async def crawl_status():
    return crawl_service.get_status()


@router.post("/crawl/stop")
async def crawl_stop():
    status = crawl_service.get_status()
    if not status["running"]:
        raise HTTPException(409, "실행 중인 크롤링이 없습니다")
    crawl_service.request_stop()
    return {"message": "중지 요청됨"}


# ══════════════════════════════════════════════
# 로그
# ══════════════════════════════════════════════

@router.get("/logs")
async def get_logs():
    return {"logs": log_service.get_logs()}


# ══════════════════════════════════════════════
# 공시 목록 (DB 내)
# ══════════════════════════════════════════════

@router.get("/disclosures")
async def list_disclosures(
    page: int = 1,
    page_size: int = 20,
    corp_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    chunk_sq = (
        select(DocumentChunk.rcp_no, sa_func.count(DocumentChunk.id).label("chunk_count"))
        .group_by(DocumentChunk.rcp_no)
        .subquery()
    )

    q = (
        select(Disclosure, sa_func.coalesce(chunk_sq.c.chunk_count, 0).label("chunk_count"))
        .outerjoin(chunk_sq, Disclosure.rcp_no == chunk_sq.c.rcp_no)
        .where(Disclosure.is_embedded == 2)
    )
    if corp_name:
        q = q.where(Disclosure.corp_name.ilike(f"%{corp_name}%"))
    if date_from:
        q = q.where(Disclosure.rcept_dt >= date_from.replace("-", ""))
    if date_to:
        q = q.where(Disclosure.rcept_dt <= date_to.replace("-", ""))

    total = await db.scalar(select(sa_func.count()).select_from(q.subquery()))
    total = total or 0
    total_pages = max(1, math.ceil(total / page_size))

    rows = (
        await db.execute(
            q.order_by(Disclosure.rcept_dt.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = [
        {
            "corp_name": r.Disclosure.corp_name,
            "report_nm": r.Disclosure.report_nm,
            "rcept_dt": r.Disclosure.rcept_dt,
            "rcept_no": r.Disclosure.rcp_no,
            "chunk_count": r.chunk_count,
            "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.Disclosure.rcp_no}",
        }
        for r in rows
    ]
    return {"items": items, "page": page, "total_pages": total_pages, "total": total}


@router.delete("/disclosures/{rcept_no}")
async def delete_disclosure(rcept_no: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Disclosure).where(Disclosure.rcp_no == rcept_no))
    disc = res.scalar_one_or_none()
    if not disc:
        raise HTTPException(404, "공시를 찾을 수 없습니다")
    await db.delete(disc)
    await db.commit()
    return {"message": "삭제되었습니다"}


# ══════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════

@router.get("/settings")
async def get_settings():
    return settings_service.load()


@router.post("/settings")
async def update_settings(body: dict = Body(...)):
    settings_service.save(body)
    _apply_scheduler(body)
    return {"message": "저장되었습니다"}


@router.post("/settings/test")
async def test_notify():
    s = settings_service.load()
    results: dict[str, bool] = {}

    if s.get("notify_email") and s.get("email_address"):
        results["email"] = False  # TODO: 이메일 전송 구현
    if s.get("notify_slack"):
        results["slack"] = False  # TODO: Slack Webhook 구현
    if s.get("notify_kakao") and s.get("kakao_token"):
        results["kakao"] = False  # TODO: 카카오 메시지 구현

    if not results:
        return {"results": {}, "message": "활성화된 알림 채널이 없습니다"}
    return {"results": results}


# ══════════════════════════════════════════════
# 스케줄러 즉시 실행
# ══════════════════════════════════════════════

@router.post("/scheduler/daily")
async def run_daily_now(background_tasks: BackgroundTasks):
    if crawl_service.get_status()["running"]:
        raise HTTPException(409, "이미 크롤링이 실행 중입니다")

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    background_tasks.add_task(_daily_bg, yesterday)
    return {"message": "일일 배치 시작", "date": yesterday}


async def _daily_bg(date: str):
    try:
        await crawl_service.start_crawl(
            mode="full",
            start_date=date,
            end_date=date,
            report_nm=None,
            pblntf_ty_list=None,
            dart_key=cfg.dart_api_key,
            gemini_key=cfg.gemini_api_key,
            gcs_bucket=cfg.gcs_bucket_name,
            gcs_creds=cfg.gcs_credentials_path,
            gemini_model=cfg.gemini_model,
            embedding_model=cfg.embedding_model,
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
        )
    except Exception as e:
        logger.error(f"일일 배치 실패: {e}")


# ══════════════════════════════════════════════
# GPU VM
# ══════════════════════════════════════════════

@router.get("/gpu-vm/status")
async def gpu_status():
    return await _gpu_vm().status()


@router.post("/gpu-vm/start")
async def gpu_start():
    try:
        return await _gpu_vm().start()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/gpu-vm/stop")
async def gpu_stop():
    try:
        return await _gpu_vm().stop()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════
# DART API 쿼타
# ══════════════════════════════════════════════

@router.get("/dart/quota")
async def get_quota():
    return quota_service.get()


@router.post("/dart/quota")
async def update_quota(body: dict = Body(...)):
    limit = body.get("limit")
    if not limit or int(limit) <= 0:
        raise HTTPException(400, "올바른 한도를 입력하세요")
    quota_service.set_limit(int(limit))
    return {"message": f"일일 한도가 {limit}으로 변경되었습니다"}


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────

def _apply_scheduler(s: dict):
    """APScheduler 잡 업데이트"""
    try:
        from ..main import scheduler  # type: ignore
        scheduler.remove_job("daily_crawl", jobstore=None)
    except Exception:
        pass

    if s.get("scheduler_enabled"):
        try:
            from apscheduler.triggers.cron import CronTrigger
            from ..main import scheduler  # type: ignore
            scheduler.add_job(
                lambda: asyncio.ensure_future(_daily_bg(
                    (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
                )),
                CronTrigger(
                    hour=s.get("scheduler_hour", 2),
                    minute=s.get("scheduler_minute", 0),
                    timezone="Asia/Seoul",
                ),
                id="daily_crawl",
                replace_existing=True,
            )
            logger.info(f"스케줄러 등록: {s.get('scheduler_hour')}:{s.get('scheduler_minute'):02d} KST")
        except Exception as e:
            logger.error(f"스케줄러 등록 실패: {e}")


import asyncio  # noqa: E402 (needed for _apply_scheduler)


# ══════════════════════════════════════════════
# IFRS 기준서
# ══════════════════════════════════════════════

@router.post("/ifrs/upload")
async def upload_ifrs(
    standard_name: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다")
    pdf_bytes = await file.read()
    from ..services.gemini_service import GeminiService
    from ..services.ifrs_service import IFRSService
    gemini = GeminiService(cfg.gemini_api_key, cfg.gemini_model, cfg.embedding_model)
    service = IFRSService(gemini)
    try:
        count = await service.embed_and_store(db, standard_name, file.filename, pdf_bytes)
        return {"message": f"{count}개 청크 임베딩 완료", "standard_name": standard_name, "chunk_count": count}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/ifrs/standards")
async def list_ifrs_standards(db: AsyncSession = Depends(get_db)):
    from ..models.models import IFRSChunk
    result = await db.execute(
        select(
            IFRSChunk.standard_name,
            IFRSChunk.filename,
            sa_func.count(IFRSChunk.id).label("chunk_count"),
        )
        .group_by(IFRSChunk.standard_name, IFRSChunk.filename)
        .order_by(IFRSChunk.standard_name)
    )
    return [
        {"standard_name": r.standard_name, "filename": r.filename, "chunk_count": r.chunk_count}
        for r in result.all()
    ]


@router.delete("/ifrs/standards/{filename:path}")
async def delete_ifrs_standard(filename: str, db: AsyncSession = Depends(get_db)):
    await db.execute(text("DELETE FROM ifrs_chunks WHERE filename = :f"), {"f": filename})
    await db.commit()
    return {"message": "삭제됐습니다"}
