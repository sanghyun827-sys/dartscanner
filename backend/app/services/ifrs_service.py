import asyncio
import io
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .gemini_service import GeminiService
from .dart_service import _semantic_split
from ..models.models import IFRSChunk

logger = logging.getLogger(__name__)


class IFRSService:
    def __init__(self, gemini: GeminiService):
        self.gemini = gemini

    def parse_pdf(self, pdf_bytes: bytes) -> str:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(p for p in pages if p.strip())

    async def embed_and_store(
        self,
        db: AsyncSession,
        standard_name: str,
        filename: str,
        pdf_bytes: bytes,
        chunk_size: int = 800,
        overlap: int = 150,
    ) -> int:
        text_content = self.parse_pdf(pdf_bytes)
        if not text_content.strip():
            raise ValueError("PDF에서 텍스트를 추출할 수 없습니다")

        chunks = _semantic_split(text_content, chunk_size, overlap)
        if not chunks:
            raise ValueError("청킹 결과가 없습니다")

        sem = asyncio.Semaphore(10)

        async def _embed(chunk: str) -> tuple[str, list[float]]:
            async with sem:
                return chunk, await self.gemini.embed_document(chunk)

        pairs = await asyncio.gather(*[_embed(c) for c in chunks])

        await db.execute(
            text("DELETE FROM ifrs_chunks WHERE filename = :f"),
            {"f": filename},
        )

        for i, (chunk, emb) in enumerate(pairs):
            db.add(IFRSChunk(
                standard_name=standard_name,
                filename=filename,
                chunk_text=chunk,
                chunk_index=i,
                embedding=emb,
            ))

        await db.commit()
        logger.info(f"IFRS 임베딩 완료: {standard_name} ({filename}) → {len(chunks)} 청크")
        return len(chunks)
