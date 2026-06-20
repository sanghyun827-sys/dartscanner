"""
크롤 파이프라인 상태 관리 및 실행.
download  → DART ZIP 다운로드 → GCS or 로컬 raw_documents/
parse     → ZIP 로드 → 텍스트 추출 → 임베딩 → pgvector 저장
full      → download + parse 순차 실행
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import text, select

from ..database import AsyncSessionLocal
from ..models.models import Disclosure
from . import quota_service

logger = logging.getLogger(__name__)

RAW_DOCS_DIR = os.environ.get("RAW_DOCS_DIR", "/app/raw_documents")


# ──────────────────────────────────────────────
# 상태 싱글톤
# ──────────────────────────────────────────────

@dataclass
class _State:
    phase: str = "idle"
    running: bool = False
    mode: Optional[str] = None
    total: int = 0
    current: int = 0
    downloaded: int = 0
    dl_skipped: int = 0
    dl_failed: int = 0
    stored: int = 0
    parse_skipped: int = 0
    parse_failed: int = 0
    stop_requested: bool = False


_s = _State()
_task: Optional[asyncio.Task] = None


def get_status() -> dict:
    return {
        "phase": _s.phase,
        "running": _s.running,
        "mode": _s.mode,
        "total": _s.total,
        "current": _s.current,
        "downloaded": _s.downloaded,
        "dl_skipped": _s.dl_skipped,
        "dl_failed": _s.dl_failed,
        "stored": _s.stored,
        "parse_skipped": _s.parse_skipped,
        "parse_failed": _s.parse_failed,
    }


def request_stop():
    global _s
    _s.stop_requested = True
    logger.info("크롤 중지 요청됨")


async def start_crawl(
    mode: str,
    start_date: Optional[str],
    end_date: Optional[str],
    report_nm: Optional[str],
    pblntf_ty_list: Optional[list],
    dart_key: str,
    gemini_key: str,
    gcs_bucket: Optional[str],
    gcs_creds: Optional[str],
    gemini_model: str,
    embedding_model: str,
    chunk_size: int,
    chunk_overlap: int,
):
    global _task, _s
    if _s.running:
        raise RuntimeError("이미 실행 중입니다")

    _s = _State()
    _task = asyncio.create_task(
        _run(
            mode, start_date, end_date, report_nm, pblntf_ty_list,
            dart_key, gemini_key, gcs_bucket, gcs_creds,
            gemini_model, embedding_model, chunk_size, chunk_overlap,
        )
    )


async def _run(
    mode, start_date, end_date, report_nm, pblntf_ty_list,
    dart_key, gemini_key, gcs_bucket, gcs_creds,
    gemini_model, embedding_model, chunk_size, chunk_overlap,
):
    from .dart_service import DartService
    from .gemini_service import GeminiService
    from .gcs_service import GCSService

    global _s
    _s.running = True
    _s.mode = mode

    dart = DartService(dart_key)
    gemini = GeminiService(gemini_key, gemini_model, embedding_model)
    gcs = GCSService(gcs_bucket, gcs_creds) if gcs_bucket else None

    try:
        if mode in ("download", "full"):
            await _download_phase(dart, gcs, start_date, end_date, report_nm, pblntf_ty_list)

        if not _s.stop_requested and mode in ("parse", "full"):
            await _parse_phase(dart, gemini, gcs, chunk_size, chunk_overlap)

        _s.phase = "stopped" if _s.stop_requested else "complete"
        logger.info(f"크롤 완료: dl={_s.downloaded} stored={_s.stored} failed={_s.parse_failed}")
    except Exception as e:
        logger.error(f"크롤 오류: {e}", exc_info=True)
        _s.phase = "error"
    finally:
        _s.running = False


# ──────────────────────────────────────────────
# 다운로드 페이즈
# ──────────────────────────────────────────────

async def _download_phase(dart, gcs, start_date, end_date, report_nm, pblntf_ty_list):
    global _s
    _s.phase = "listing"
    logger.info(f"공시 목록 조회: {start_date} ~ {end_date}")

    all_items = []
    if pblntf_ty_list:
        for ty in pblntf_ty_list:
            if _s.stop_requested:
                break
            items = await _fetch_all(dart, start_date, end_date, report_nm, ty)
            all_items.extend(items)
    else:
        all_items = await _fetch_all(dart, start_date, end_date, report_nm, None)

    _s.total = len(all_items)
    logger.info(f"대상 공시 {_s.total}건")

    if _s.total == 0:
        return

    _s.phase = "downloading"

    async with AsyncSessionLocal() as db:
        for i, item in enumerate(all_items):
            if _s.stop_requested:
                break
            _s.current = i + 1
            rcp_no = item.get("rcept_no", "")
            if not rcp_no:
                continue

            # DB에 저장
            await db.execute(
                text("""
                    INSERT INTO disclosures (rcp_no, corp_code, corp_name, report_nm, rcept_dt, flr_nm, rm)
                    VALUES (:rcp_no, :corp_code, :corp_name, :report_nm, :rcept_dt, :flr_nm, :rm)
                    ON CONFLICT (rcp_no) DO NOTHING
                """),
                {
                    "rcp_no": rcp_no,
                    "corp_code": item.get("corp_code", ""),
                    "corp_name": item.get("corp_name", ""),
                    "report_nm": item.get("report_nm", ""),
                    "rcept_dt": item.get("rcept_dt", ""),
                    "flr_nm": item.get("flr_nm", ""),
                    "rm": item.get("rm", ""),
                },
            )
            await db.commit()

            # 이미 존재하는지 확인
            gcs_path = f"raw_documents/{rcp_no}.zip"
            local_path = os.path.join(RAW_DOCS_DIR, f"{rcp_no}.zip")

            already = (await gcs.exists(gcs_path)) if gcs else os.path.exists(local_path)
            if already:
                _s.dl_skipped += 1
                continue

            # 다운로드
            try:
                zip_bytes = await dart.download_document(rcp_no)
                if not zip_bytes:
                    raise ValueError("빈 응답")

                if gcs:
                    await gcs.upload(gcs_path, zip_bytes, "application/zip")
                else:
                    os.makedirs(RAW_DOCS_DIR, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(zip_bytes)

                _s.downloaded += 1
                logger.info(f"다운로드: {rcp_no} ({item.get('corp_name', '')})")
            except Exception as e:
                _s.dl_failed += 1
                logger.error(f"다운로드 실패 {rcp_no}: {e}")


async def _fetch_all(dart, start_date, end_date, report_nm, pblntf_ty) -> list:
    all_items = []
    page = 1
    while True:
        data = await dart.get_disclosure_list(
            bgn_de=start_date,
            end_de=end_date,
            pblntf_ty=pblntf_ty,
            page_no=page,
            page_count=100,
        )
        if data.get("status") != "000":
            logger.warning(f"DART API 오류: {data.get('message')}")
            break

        items = data.get("list", [])
        if not items:
            break

        if report_nm:
            items = [i for i in items if report_nm.lower() in i.get("report_nm", "").lower()]

        all_items.extend(items)

        total_count = int(data.get("total_count", 0))
        if len(all_items) >= total_count or len(items) < 100:
            break
        page += 1
        await asyncio.sleep(0.15)  # rate limit 방지

    return all_items


# ──────────────────────────────────────────────
# 파싱 / 임베딩 페이즈
# ──────────────────────────────────────────────

async def _parse_phase(dart, gemini, gcs, chunk_size, chunk_overlap):
    global _s
    _s.phase = "parsing"

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Disclosure).where(Disclosure.is_embedded == 0).order_by(Disclosure.rcept_dt.desc())
        )
        disclosures = result.scalars().all()

    _s.total = len(disclosures)
    logger.info(f"임베딩 대상: {_s.total}건")

    _sem = asyncio.Semaphore(10)

    async def _embed(chunk: dict) -> tuple[dict, list[float]]:
        async with _sem:
            return chunk, await gemini.embed_document(chunk["text"])

    for i, disc in enumerate(disclosures):
        if _s.stop_requested:
            break
        _s.current = i + 1

        rcp_no = disc.rcp_no
        gcs_path = f"raw_documents/{rcp_no}.zip"
        local_path = os.path.join(RAW_DOCS_DIR, f"{rcp_no}.zip")

        # ZIP 로드
        zip_bytes: Optional[bytes] = None
        if gcs:
            zip_bytes = await gcs.download(gcs_path)
        if not zip_bytes and os.path.exists(local_path):
            with open(local_path, "rb") as f:
                zip_bytes = f.read()
        if not zip_bytes:
            zip_bytes = await dart.download_document(rcp_no)

        if not zip_bytes:
            _s.parse_failed += 1
            logger.error(f"ZIP 없음: {rcp_no}")
            await _set_status(rcp_no, 3)
            continue

        try:
            # ── 개선된 파싱 파이프라인 ──────────────────
            parsed_doc = dart.parse_document_zip(zip_bytes)
            chunks = dart.chunk_with_metadata(parsed_doc, chunk_size, chunk_overlap)

            if not chunks:
                _s.parse_skipped += 1
                logger.warning(f"텍스트 없음: {rcp_no}")
                await _set_status(rcp_no, 3)
                continue

            await _set_status(rcp_no, 1)  # 처리중

            pairs = await asyncio.gather(*[_embed(c) for c in chunks])

            async with AsyncSessionLocal() as db:
                await db.execute(text("DELETE FROM document_chunks WHERE rcp_no=:r"), {"r": rcp_no})

                for chunk, emb in pairs:
                    meta = chunk["metadata"]
                    vec_str = "[" + ",".join(f"{v:.8f}" for v in emb) + "]"
                    await db.execute(
                        text("""
                            INSERT INTO document_chunks
                            (disclosure_id, rcp_no, corp_name, report_nm, chunk_text, chunk_index, embedding, meta)
                            VALUES (:did, :rcp, :corp, :rpt, :txt, :ci, CAST(:emb AS vector), CAST(:meta AS jsonb))
                        """),
                        {
                            "did": disc.id,
                            "rcp": rcp_no,
                            "corp": disc.corp_name,
                            "rpt": disc.report_nm,
                            "txt": chunk["text"],
                            "ci": meta.get("chunk_index", 0),
                            "emb": vec_str,
                            "meta": json.dumps(meta),
                        },
                    )

                await db.execute(text("UPDATE disclosures SET is_embedded=2 WHERE rcp_no=:r"), {"r": rcp_no})
                await db.commit()

            _s.stored += 1
            logger.info(f"임베딩 완료: {rcp_no} ({disc.corp_name}) "
                        f"→ {len(chunks)} 청크 / {len(parsed_doc.sections)} 섹션")

        except Exception as e:
            _s.parse_failed += 1
            logger.error(f"파싱 실패 {rcp_no}: {e}", exc_info=True)
            await _set_status(rcp_no, 3)


async def _set_status(rcp_no: str, status: int):
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE disclosures SET is_embedded=:s WHERE rcp_no=:r"),
            {"s": status, "r": rcp_no},
        )
        await db.commit()
