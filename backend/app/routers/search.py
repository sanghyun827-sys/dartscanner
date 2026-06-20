"""
index.html 의 POST /search 처리.
mode: 'dart' (Hybrid RAG) | 'ifrs' (LLM 지식 기반)
debate_mode: True → 반박 분석
"""

import re
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import settings as cfg
from ..services.gemini_service import GeminiService
from ..services.hybrid_search import run_hybrid_search

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    mode: str = "dart"          # 'dart' | 'ifrs'
    debate_mode: bool = False


@router.post("/search")
async def search(req: SearchRequest, db: AsyncSession = Depends(get_db)):
    gemini = GeminiService(cfg.gemini_api_key, cfg.gemini_model, cfg.embedding_model)

    if req.mode == "ifrs":
        return await _ifrs_search(req, gemini, db)

    return await _dart_search(req, gemini, db)


# ══════════════════════════════════════════════
# DART 모드 — Hybrid RAG
# ══════════════════════════════════════════════

async def _dart_search(req: SearchRequest, gemini: GeminiService, db: AsyncSession) -> dict:
    chunks = await run_hybrid_search(
        db, req.query, gemini,
        corp_name=None,
        top_k=req.top_k,
        alpha=cfg.hybrid_alpha,
        candidate_k=cfg.hybrid_candidate_k,
    )

    if not chunks:
        return {
            "answer": "관련 공시 자료를 찾을 수 없습니다. "
                      "관리자 페이지에서 공시를 먼저 크롤링·임베딩해 주세요.",
            "disclosures": [],
        }

    # 답변 생성
    if req.debate_mode:
        answer = await gemini.generate_debate(req.query, chunks)
    else:
        answer = await gemini.generate_answer(req.query, chunks)

    # 중복 제거 출처 목록 (점수 높은 순)
    seen: dict = {}
    for c in chunks:
        rcp = c["rcp_no"]
        if rcp not in seen:
            seen[rcp] = {
                "rcp_no":    rcp,
                "corp_name": c["corp_name"],
                "report_nm": c["report_nm"],
                "rcept_dt":  c["rcept_dt"] or "",
                "score":     c.get("rrf_score", c.get("score", 0)),
                "dart_url":  f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}",
            }

    return {"answer": answer, "disclosures": list(seen.values())}


# ══════════════════════════════════════════════
# IFRS 모드 — RAG (기준서 임베딩 우선, fallback LLM)
# ══════════════════════════════════════════════

async def _ifrs_search(req: SearchRequest, gemini: GeminiService, db: AsyncSession) -> dict:
    from sqlalchemy import text as sql_text

    embedding = await gemini.embed_query(req.query)
    vec_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    result = await db.execute(
        sql_text("""
            SELECT standard_name, chunk_text,
                   1 - (embedding <=> :vec::vector) AS similarity
            FROM ifrs_chunks
            ORDER BY embedding <=> :vec::vector
            LIMIT :k
        """),
        {"vec": vec_str, "k": req.top_k},
    )
    chunks = result.fetchall()

    if not chunks:
        return await _ifrs_llm_fallback(req, gemini)

    ctx = "\n\n".join(f"[{r.standard_name}]\n{r.chunk_text}" for r in chunks)
    prompt = f"""당신은 IFRS(국제회계기준) 전문가입니다.
아래 기준서 내용을 바탕으로 질문에 한국어로 상세히 답변하세요.

## 관련 기준서 내용
{ctx}

## 질문
{req.query}

## 답변 지침
- 반드시 제공된 기준서 내용에 근거하여 답변하세요
- IFRS/IAS 기준서 및 조문 번호를 인용하세요
- 한국어로 답변하세요"""

    response = await asyncio.to_thread(gemini._chat_model.generate_content, prompt)
    answer_text = response.text

    return {
        "answer": answer_text,
        "ifrs_references": _extract_ifrs_refs(answer_text),
        "ifrs_sources": [
            {
                "standard_name": r.standard_name,
                "chunk_text": r.chunk_text,
                "similarity": round(float(r.similarity), 4),
            }
            for r in chunks
        ],
        "disclosures": [],
    }


async def _ifrs_llm_fallback(req: SearchRequest, gemini: GeminiService) -> dict:
    prompt = f"""당신은 IFRS(국제회계기준) 전문가입니다.
아래 질문에 한국어로 상세히 답변하고, 관련 IFRS/IAS 기준서 조문을 구체적으로 인용하세요.

질문: {req.query}

답변 형식:
1. 핵심 답변 (2~3문단)
2. 관련 IFRS/IAS 기준서 및 주요 조문 번호
3. 실무 적용 시 주의사항"""

    response = await asyncio.to_thread(gemini._chat_model.generate_content, prompt)
    answer_text = response.text

    return {
        "answer": answer_text,
        "ifrs_references": _extract_ifrs_refs(answer_text),
        "disclosures": [],
    }


def _extract_ifrs_refs(text: str) -> list:
    found: dict[str, dict] = {}
    for m in re.finditer(r"(IFRS|IAS)\s+(\d+[A-Z]?)", text, re.IGNORECASE):
        std = f"{m.group(1).upper()} {m.group(2)}"
        if std not in found:
            found[std] = {
                "standard":  std,
                "paragraph": "관련 조문",
                "title":     f"{std} 기준서",
                "summary":   f"{std} 관련 내용입니다. 전문은 KASB 공식 사이트를 참조하세요.",
                "kasb_url":  "https://www.kasb.or.kr/",
            }
    return list(found.values())[:4]
