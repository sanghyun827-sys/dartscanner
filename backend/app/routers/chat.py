from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas.schemas import ChatRequest, ChatResponse, ChatSource
from ..services.gemini_service import GeminiService
from ..services.hybrid_search import run_hybrid_search
from ..config import settings

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    """Hybrid RAG 기반 공시 질문 답변"""
    gemini = GeminiService(settings.gemini_api_key, settings.gemini_model, settings.embedding_model)

    top_k = request.top_k if request.top_k is not None else settings.top_k
    chunks = await run_hybrid_search(
        db,
        request.question,
        gemini,
        corp_name=request.corp_name,
        top_k=top_k,
        alpha=settings.hybrid_alpha,
        candidate_k=settings.hybrid_candidate_k,
    )

    if not chunks:
        return ChatResponse(
            answer="관련 공시 자료를 찾을 수 없습니다. 먼저 공시 목록에서 문서를 임베딩해 주세요.",
            sources=[],
        )

    answer_text = await gemini.generate_answer(request.question, chunks)

    seen: set[str] = set()
    sources: list[ChatSource] = []
    for c in chunks:
        if c["rcp_no"] not in seen:
            seen.add(c["rcp_no"])
            sources.append(ChatSource(
                rcp_no=c["rcp_no"],
                corp_name=c["corp_name"],
                report_nm=c["report_nm"],
                similarity=c.get("rrf_score", c.get("score", 0.0)),
            ))

    return ChatResponse(answer=answer_text, sources=sources)
