import asyncio
import io
import re
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .gemini_service import GeminiService
from .dart_service import _semantic_split
from ..models.models import IFRSChunk

logger = logging.getLogger(__name__)

# IFRS 조문 번호 패턴: 12. / 12A. / IN1. / AG3. / BCZ4. / IE2. / B1. 등
_PARA_NUM = re.compile(
    r"^(?:IN|AG|BC|BCZ|IE|IG|B|C|D|E|F)?\d+[A-Z]?\.",
    re.MULTILINE,
)


def _split_ifrs_paragraphs(text: str, max_size: int = 1200) -> list[str]:
    """
    IFRS 조문 번호(12., 12A., IN1., AG3. 등)를 기준으로 청크 분리.
    조문 번호를 찾지 못하면 _semantic_split 폴백.
    조문이 max_size 초과 시 문장 단위로 추가 분리.
    """
    lines = text.splitlines()
    chunks: list[str] = []
    buf_lines: list[str] = []

    def flush():
        chunk = "\n".join(buf_lines).strip()
        if len(chunk) >= 50:  # 너무 짧은 단편 제외
            if len(chunk) <= max_size:
                chunks.append(chunk)
            else:
                # 조문이 너무 길면 문장 단위로 추가 분리
                sub = _split_by_sentences(chunk, max_size)
                chunks.extend(sub)
        buf_lines.clear()

    for line in lines:
        if _PARA_NUM.match(line.strip()):
            if buf_lines:
                flush()
        buf_lines.append(line)

    if buf_lines:
        flush()

    # 조문 번호가 전혀 없으면 일반 청킹 폴백
    if not chunks:
        logger.warning("조문 번호 패턴 미검출 — 일반 청킹으로 폴백")
        return _semantic_split(text, max_size, overlap=0)

    return chunks


def _split_by_sentences(text: str, max_size: int) -> list[str]:
    """문장 종결 기준 분리 (조문이 너무 길 때 사용)."""
    _end = re.compile(r"(?<=[.!?。？！다요])\s+")
    sentences = [s.strip() for s in _end.split(text) if s.strip()]
    chunks, buf = [], ""
    for sent in sentences:
        cand = (buf + " " + sent).strip() if buf else sent
        if len(cand) <= max_size:
            buf = cand
        else:
            if buf:
                chunks.append(buf)
            buf = sent
    if buf:
        chunks.append(buf)
    return chunks or [text]


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
        chunk_size: int = 1200,
        overlap: int = 0,
    ) -> int:
        text_content = self.parse_pdf(pdf_bytes)
        if not text_content.strip():
            raise ValueError("PDF에서 텍스트를 추출할 수 없습니다")

        chunks = _split_ifrs_paragraphs(text_content, max_size=chunk_size)
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
