import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers import dart, chat, admin, search
from .services import log_service, settings_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# APScheduler (선택적)
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    _SCHED_AVAILABLE = True
except ImportError:
    scheduler = None
    _SCHED_AVAILABLE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 테이블 생성
    await init_db()

    # 메모리 로그 캡처 시작
    log_service.setup()

    # 스케줄러
    if _SCHED_AVAILABLE and scheduler:
        s = settings_service.load()
        if s.get("scheduler_enabled"):
            from apscheduler.triggers.cron import CronTrigger
            from .services import crawl_service
            from .config import settings as cfg

            async def _daily():
                date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
                try:
                    await crawl_service.start_crawl(
                        mode="full", start_date=date, end_date=date,
                        report_nm=None, pblntf_ty_list=None,
                        dart_key=cfg.dart_api_key, gemini_key=cfg.gemini_api_key,
                        gcs_bucket=cfg.gcs_bucket_name, gcs_creds=cfg.gcs_credentials_path,
                        gemini_model=cfg.gemini_model, embedding_model=cfg.embedding_model,
                        chunk_size=cfg.chunk_size, chunk_overlap=cfg.chunk_overlap,
                    )
                except Exception as e:
                    logging.getLogger(__name__).error(f"스케줄 크롤 실패: {e}")

            scheduler.add_job(
                _daily,
                CronTrigger(
                    hour=s.get("scheduler_hour", 2),
                    minute=s.get("scheduler_minute", 0),
                    timezone="Asia/Seoul",
                ),
                id="daily_crawl",
            )
            scheduler.start()

    yield

    if _SCHED_AVAILABLE and scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="DART Scanner API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우터 등록 ──────────────────────────────
# 새 루트 수준 라우터 (admin.html / index.html 전용)
app.include_router(admin.router)
app.include_router(search.router)

# 기존 /api/* 라우터 (이전 호환)
app.include_router(dart.router, prefix="/api")
app.include_router(chat.router, prefix="/api")


@app.get("/api/health")
async def health():
    from .config import settings
    return {
        "status": "ok",
        "dart_key_set": bool(settings.dart_api_key),
        "gemini_key_set": bool(settings.gemini_api_key),
        "gcs_configured": bool(settings.gcs_bucket_name),
        "gcp_vm_configured": bool(settings.gcp_project_id),
    }


# ── 프론트엔드 정적 파일 (마지막에 마운트) ──
_fe_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))
if os.path.isdir(_fe_dir):
    app.mount("/", StaticFiles(directory=_fe_dir, html=True), name="frontend")
