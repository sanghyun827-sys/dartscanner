import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import AsyncSessionLocal, get_db
from ..models.models import Company, Disclosure
from ..schemas.schemas import EmbedStatusResponse
from ..services.dart_service import DartService
from ..services.gemini_service import GeminiService
from ..services.gcs_service import GCSService
from ..services.rag_service import RAGService
from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dart", tags=["dart"])

# ──────────────────────────────────────────────
# 의존성
# ──────────────────────────────────────────────

def _dart() -> DartService:
    return DartService(settings.dart_api_key)


def _gemini() -> GeminiService:
    return GeminiService(
        settings.gemini_api_key,
        settings.gemini_model,
        settings.embedding_model,
    )


def _gcs() -> Optional[GCSService]:
    if settings.gcs_bucket_name:
        return GCSService(settings.gcs_bucket_name, settings.gcs_credentials_path)
    return None


# ──────────────────────────────────────────────
# 기업 코드
# ──────────────────────────────────────────────

@router.get("/companies/search")
async def search_companies(q: str, db: AsyncSession = Depends(get_db)):
    """기업명 검색 (DB에서)"""
    result = await db.execute(
        select(Company).where(Company.corp_name.ilike(f"%{q}%")).limit(20)
    )
    companies = result.scalars().all()
    return [{"corp_code": c.corp_code, "corp_name": c.corp_name, "stock_code": c.stock_code} for c in companies]


@router.post("/companies/sync")
async def sync_companies(background_tasks: BackgroundTasks):
    """전체 기업 코드를 DART에서 다운로드해 DB에 저장 (백그라운드)"""
    background_tasks.add_task(_sync_companies_bg, settings.dart_api_key)
    return {"message": "기업 코드 동기화를 시작했습니다. 수분 소요될 수 있습니다."}


async def _sync_companies_bg(api_key: str):
    dart = DartService(api_key)
    try:
        zip_bytes = await dart.download_corp_codes()
        companies = dart.parse_corp_codes_xml(zip_bytes)
        logger.info(f"Syncing {len(companies)} companies")

        async with AsyncSessionLocal() as db:
            for batch_start in range(0, len(companies), 500):
                batch = companies[batch_start:batch_start + 500]
                for c in batch:
                    await db.execute(
                        text("""
                            INSERT INTO companies (corp_code, corp_name, stock_code, modify_date)
                            VALUES (:corp_code, :corp_name, :stock_code, :modify_date)
                            ON CONFLICT (corp_code) DO UPDATE
                            SET corp_name=EXCLUDED.corp_name,
                                stock_code=EXCLUDED.stock_code,
                                modify_date=EXCLUDED.modify_date
                        """),
                        c,
                    )
                await db.commit()
        logger.info("Company sync complete")
    except Exception as e:
        logger.error(f"Company sync failed: {e}")


# ──────────────────────────────────────────────
# 공시 목록
# ──────────────────────────────────────────────

@router.get("/disclosures")
async def get_disclosures(
    corp_code: Optional[str] = None,
    corp_name: Optional[str] = None,
    bgn_de: Optional[str] = None,
    end_de: Optional[str] = None,
    pblntf_ty: Optional[str] = None,
    page_no: int = 1,
    page_count: int = 20,
    db: AsyncSession = Depends(get_db),
    dart: DartService = Depends(_dart),
):
    # corp_name으로 corp_code 조회
    if corp_name and not corp_code:
        res = await db.execute(
            select(Company).where(Company.corp_name.ilike(f"%{corp_name}%")).limit(1)
        )
        co = res.scalar_one_or_none()
        if co:
            corp_code = co.corp_code

    data = await dart.get_disclosure_list(
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        pblntf_ty=pblntf_ty,
        page_no=page_no,
        page_count=page_count,
    )

    if data.get("status") != "000":
        return {
            "items": [],
            "total_count": 0,
            "page_no": page_no,
            "message": data.get("message", ""),
        }

    items = data.get("list", [])

    # DB에 없으면 저장 (upsert)
    for item in items:
        await db.execute(
            text("""
                INSERT INTO disclosures (rcp_no, corp_code, corp_name, report_nm, rcept_dt, flr_nm, rm)
                VALUES (:rcp_no, :corp_code, :corp_name, :report_nm, :rcept_dt, :flr_nm, :rm)
                ON CONFLICT (rcp_no) DO NOTHING
            """),
            {
                "rcp_no": item.get("rcept_no", ""),
                "corp_code": item.get("corp_code", ""),
                "corp_name": item.get("corp_name", ""),
                "report_nm": item.get("report_nm", ""),
                "rcept_dt": item.get("rcept_dt", ""),
                "flr_nm": item.get("flr_nm", ""),
                "rm": item.get("rm", ""),
            },
        )
    await db.commit()

    # 임베딩 상태 조회
    rcp_nos = [i.get("rcept_no", "") for i in items]
    embed_status: dict[str, int] = {}
    if rcp_nos:
        res = await db.execute(
            select(Disclosure.rcp_no, Disclosure.is_embedded).where(Disclosure.rcp_no.in_(rcp_nos))
        )
        embed_status = {r.rcp_no: r.is_embedded for r in res}

    enriched = [
        {**item, "rcp_no": item.get("rcept_no", ""), "is_embedded": embed_status.get(item.get("rcept_no", ""), 0)}
        for item in items
    ]

    return {
        "items": enriched,
        "total_count": data.get("total_count", 0),
        "page_no": page_no,
    }


# ──────────────────────────────────────────────
# 임베딩
# ──────────────────────────────────────────────

@router.post("/disclosures/{rcp_no}/embed")
async def embed_disclosure(
    rcp_no: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Disclosure).where(Disclosure.rcp_no == rcp_no))
    disclosure = res.scalar_one_or_none()
    if not disclosure:
        raise HTTPException(404, "공시를 찾을 수 없습니다. 먼저 공시 목록을 검색하세요.")

    if disclosure.is_embedded == 1:
        return {"message": "이미 처리 중입니다.", "rcp_no": rcp_no}

    background_tasks.add_task(
        _embed_bg,
        rcp_no,
        disclosure.corp_name,
        disclosure.report_nm,
        settings.dart_api_key,
        settings.gemini_api_key,
        settings.gcs_bucket_name,
        settings.gcs_credentials_path,
    )
    return {"message": "임베딩을 시작했습니다.", "rcp_no": rcp_no}


async def _embed_bg(
    rcp_no: str,
    corp_name: str,
    report_nm: str,
    dart_key: str,
    gemini_key: str,
    gcs_bucket: Optional[str],
    gcs_creds: Optional[str],
):
    dart = DartService(dart_key)
    gemini = GeminiService(gemini_key, settings.gemini_model, settings.embedding_model)
    gcs = GCSService(gcs_bucket, gcs_creds) if gcs_bucket else None
    rag = RAGService(dart, gemini, gcs)

    async with AsyncSessionLocal() as db:
        await rag.embed_disclosure(db, rcp_no, corp_name, report_nm)


@router.get("/disclosures/{rcp_no}/status", response_model=EmbedStatusResponse)
async def get_embed_status(rcp_no: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Disclosure).where(Disclosure.rcp_no == rcp_no))
    d = res.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "공시를 찾을 수 없습니다.")

    status_map = {0: "대기", 1: "처리중", 2: "완료", 3: "실패"}
    return EmbedStatusResponse(
        rcp_no=rcp_no,
        is_embedded=d.is_embedded,
        status_text=status_map.get(d.is_embedded, "알 수 없음"),
    )


@router.get("/embedded")
async def list_embedded(db: AsyncSession = Depends(get_db)):
    """임베딩 완료된 공시 목록"""
    res = await db.execute(
        select(Disclosure).where(Disclosure.is_embedded == 2).order_by(Disclosure.rcept_dt.desc()).limit(100)
    )
    disclosures = res.scalars().all()
    return [
        {
            "rcp_no": d.rcp_no,
            "corp_name": d.corp_name,
            "report_nm": d.report_nm,
            "rcept_dt": d.rcept_dt,
        }
        for d in disclosures
    ]
