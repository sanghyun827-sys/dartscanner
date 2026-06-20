import asyncio
import logging
from typing import Optional

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.models import Disclosure, DocumentChunk
from ..config import settings
from .dart_service import DartService
from .gemini_service import GeminiService
from .gcs_service import GCSService

logger = logging.getLogger(__name__)


class RAGService:
    def __init__(
        self,
        dart: DartService,
        gemini: GeminiService,
        gcs: Optional[GCSService] = None,
    ):
        self.dart = dart
        self.gemini = gemini
        self.gcs = gcs

    # ──────────────────────────────────────────────
    # 임베딩 파이프라인
    # ──────────────────────────────────────────────

    async def embed_disclosure(
        self,
        db: AsyncSession,
        rcp_no: str,
        corp_name: str,
        report_nm: str,
    ) -> bool:
        logger.info(f"[EMBED] start: {rcp_no} ({corp_name})")
        try:
            # 처리중 표시
            await _set_embed_status(db, rcp_no, 1)

            # ZIP 다운로드 (GCS 캐시 활용)
            zip_bytes = await self._get_document_bytes(rcp_no)
            if not zip_bytes:
                raise ValueError("Document download returned empty")

            # 파싱
            parsed_doc = self.dart.parse_document_zip(zip_bytes)
            chunks = self.dart.chunk_with_metadata(parsed_doc, settings.chunk_size, settings.chunk_overlap)
            if not chunks:
                raise ValueError("No text extracted from document")

            # Disclosure 레코드 조회
            result = await db.execute(select(Disclosure).where(Disclosure.rcp_no == rcp_no))
            disclosure = result.scalar_one_or_none()
            if not disclosure:
                raise ValueError("Disclosure record not found in DB")

            # 기존 청크 삭제
            await db.execute(text("DELETE FROM document_chunks WHERE rcp_no = :rcp"), {"rcp": rcp_no})
            await db.flush()

            logger.info(f"[EMBED] {len(chunks)} chunks for {rcp_no}")

            sem = asyncio.Semaphore(10)

            async def _embed(chunk: dict) -> tuple[dict, list[float]]:
                async with sem:
                    return chunk, await self.gemini.embed_document(chunk["text"])

            pairs = await asyncio.gather(*[_embed(c) for c in chunks])

            for chunk, embedding in pairs:
                meta = chunk["metadata"]
                db.add(
                    DocumentChunk(
                        disclosure_id=disclosure.id,
                        rcp_no=rcp_no,
                        corp_name=corp_name,
                        report_nm=report_nm,
                        chunk_text=chunk["text"],
                        chunk_index=meta.get("chunk_index", 0),
                        embedding=embedding,
                        meta=meta,
                    )
                )

            await db.commit()
            await _set_embed_status(db, rcp_no, 2)
            logger.info(f"[EMBED] done: {rcp_no}")
            return True

        except Exception as e:
            logger.error(f"[EMBED] failed {rcp_no}: {e}")
            try:
                await _set_embed_status(db, rcp_no, 3)
            except Exception:
                pass
            return False

    async def _get_document_bytes(self, rcp_no: str) -> Optional[bytes]:
        """GCS 캐시 우선, 없으면 DART에서 다운로드 후 업로드"""
        gcs_path = f"dart_docs/{rcp_no}.zip"

        if self.gcs:
            cached = await self.gcs.download(gcs_path)
            if cached:
                logger.debug(f"GCS cache hit: {rcp_no}")
                return cached

        zip_bytes = await self.dart.download_document(rcp_no)
        if zip_bytes and self.gcs:
            try:
                await self.gcs.upload(gcs_path, zip_bytes, "application/zip")
            except Exception as e:
                logger.warning(f"GCS upload failed {rcp_no}: {e}")
        return zip_bytes

    # ──────────────────────────────────────────────
    # 검색 + 답변
    # ──────────────────────────────────────────────

    async def search_chunks(
        self,
        db: AsyncSession,
        query: str,
        corp_name: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        embedding = await self.gemini.embed_query(query)
        vec_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

        if corp_name:
            sql = text("""
                SELECT rcp_no, corp_name, report_nm, chunk_text,
                       1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM document_chunks
                WHERE corp_name ILIKE :corp
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT :k
            """)
            result = await db.execute(sql, {"vec": vec_str, "corp": f"%{corp_name}%", "k": top_k})
        else:
            sql = text("""
                SELECT rcp_no, corp_name, report_nm, chunk_text,
                       1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                FROM document_chunks
                ORDER BY embedding <=> CAST(:vec AS vector)
                LIMIT :k
            """)
            result = await db.execute(sql, {"vec": vec_str, "k": top_k})

        rows = result.fetchall()
        return [
            {
                "rcp_no": r.rcp_no,
                "corp_name": r.corp_name,
                "report_nm": r.report_nm,
                "chunk_text": r.chunk_text,
                "similarity": round(float(r.similarity), 4),
            }
            for r in rows
        ]

    async def answer(
        self,
        db: AsyncSession,
        question: str,
        corp_name: Optional[str] = None,
    ) -> dict:
        chunks = await self.search_chunks(db, question, corp_name, settings.top_k)
        if not chunks:
            return {
                "answer": "관련 공시 자료를 찾을 수 없습니다. 먼저 공시 목록에서 문서를 임베딩해 주세요.",
                "sources": [],
            }

        answer_text = await self.gemini.generate_answer(question, chunks)

        # 중복 제거된 출처
        seen: set[str] = set()
        sources = []
        for c in chunks:
            if c["rcp_no"] not in seen:
                seen.add(c["rcp_no"])
                sources.append(
                    {
                        "rcp_no": c["rcp_no"],
                        "corp_name": c["corp_name"],
                        "report_nm": c["report_nm"],
                        "similarity": c["similarity"],
                    }
                )
        return {"answer": answer_text, "sources": sources}


async def _set_embed_status(db: AsyncSession, rcp_no: str, status: int):
    await db.execute(
        text("UPDATE disclosures SET is_embedded = :s WHERE rcp_no = :r"),
        {"s": status, "r": rcp_no},
    )
    await db.commit()
