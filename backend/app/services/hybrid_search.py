"""
Hybrid Search — Dense + Sparse + RRF Fusion

┌─────────────────────────────────────────────────────────────┐
│  쿼리                                                        │
│    │                                                         │
│    ├─▶ [Dense]  pgvector 코사인 유사도   → 상위 40개        │
│    │                                                         │
│    └─▶ [Sparse] PostgreSQL FTS (simple)                     │
│              └─▶ Python BM25 재랭킹     → 상위 40개        │
│                                                              │
│  Reciprocal Rank Fusion (RRF)                                │
│    score(d) = α · 1/(k + rank_dense) + β · 1/(k + rank_bm25)│
│                                                              │
│  → 최종 상위 K개 반환                                        │
└─────────────────────────────────────────────────────────────┘

BM25 비교
  ┌─────────────────┬──────────────────────────────────────┐
  │ BM25 (키워드)   │ 고유 명사, 종목코드, 정확한 용어 강점 │
  │ Vector (의미)   │ 유의어, 문맥, 질문 의도 파악 강점     │
  └─────────────────┴──────────────────────────────────────┘
"""

import json
import logging
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .gemini_service import GeminiService

logger = logging.getLogger(__name__)

# RRF 상수 (일반적으로 60 사용)
_RRF_K: int = 60


class HybridSearchService:
    """
    Parameters
    ----------
    alpha : float
        벡터 검색 가중치 (0 ~ 1).
        alpha=0.7 → 벡터 70% + BM25 30%.
        - 의미 검색이 중요할 때: 0.8+
        - 정확한 키워드 매칭이 중요할 때: 0.5 이하
    candidate_k : int
        각 검색 방식에서 가져올 후보 수 (기본 40).
        top_k 보다 크게 설정해야 RRF 효과가 있음.
    """

    def __init__(
        self,
        gemini: GeminiService,
        alpha: float = 0.7,
        candidate_k: int = 40,
    ):
        self.gemini = gemini
        self.alpha = alpha
        self.candidate_k = candidate_k

    # ════════════════════════════════════════════
    # 공개 인터페이스
    # ════════════════════════════════════════════

    async def search(
        self,
        db: AsyncSession,
        query: str,
        corp_name: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Hybrid search → RRF 통합 결과 반환.
        각 결과 dict 구조:
          rcp_no, corp_name, report_nm, chunk_text,
          rcept_dt, section, has_table,
          dense_score, bm25_score, rrf_score
        """
        k = self.candidate_k

        # 1. Dense 검색 (벡터)
        dense = await self._dense(db, query, corp_name, k)

        # 2. Sparse 검색 (BM25)
        sparse = await self._sparse(db, query, corp_name, k)

        # 3. RRF 통합
        merged = _rrf_merge(dense, sparse, self.alpha, top_k)

        logger.debug(
            "Hybrid search: dense=%d sparse=%d merged=%d query='%s'",
            len(dense), len(sparse), len(merged), query[:40],
        )
        return merged

    # ════════════════════════════════════════════
    # Dense 검색 (pgvector cosine)
    # ════════════════════════════════════════════

    async def _dense(
        self,
        db: AsyncSession,
        query: str,
        corp_name: Optional[str],
        k: int,
    ) -> list[dict]:
        emb = await self.gemini.embed_query(query)
        vec_str = "[" + ",".join(f"{v:.8f}" for v in emb) + "]"

        params: dict = {"vec": vec_str, "k": k}
        corp_clause = ""
        if corp_name:
            corp_clause = "AND dc.corp_name ILIKE :corp"
            params["corp"] = f"%{corp_name}%"

        sql = text(f"""
            SELECT dc.rcp_no,
                   dc.corp_name,
                   dc.report_nm,
                   dc.chunk_text,
                   dc.meta,
                   d.rcept_dt,
                   1 - (dc.embedding <=> :vec::vector) AS score
            FROM   document_chunks dc
            JOIN   disclosures d ON dc.rcp_no = d.rcp_no
            WHERE  dc.embedding IS NOT NULL
                   {corp_clause}
            ORDER  BY dc.embedding <=> :vec::vector
            LIMIT  :k
        """)

        rows = (await db.execute(sql, params)).fetchall()
        return [_row_to_dict(r, r.score) for r in rows]

    # ════════════════════════════════════════════
    # Sparse 검색 (PostgreSQL FTS → Python BM25)
    # ════════════════════════════════════════════

    async def _sparse(
        self,
        db: AsyncSession,
        query: str,
        corp_name: Optional[str],
        k: int,
    ) -> list[dict]:
        """
        1단계: PostgreSQL FTS로 키워드 포함 후보 추출
        2단계: Python BM25Okapi로 정밀 재랭킹
        """
        safe_q = _sanitize_for_fts(query)
        if not safe_q:
            return []

        # 1단계 — PostgreSQL FTS 후보 추출 (BM25 풀보다 더 많이 가져옴)
        candidates = await self._fts_candidates(db, safe_q, corp_name, k * 3)

        if not candidates:
            return []

        # 2단계 — Python BM25 재랭킹
        texts = [c["chunk_text"] for c in candidates]
        bm25_scores = _bm25_score(query, texts)

        for c, score in zip(candidates, bm25_scores):
            c["score"] = float(score)

        # BM25 점수 내림차순 정렬 후 상위 k개
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:k]

    async def _fts_candidates(
        self,
        db: AsyncSession,
        safe_q: str,
        corp_name: Optional[str],
        k: int,
    ) -> list[dict]:
        """PostgreSQL FTS + trigram 중 결과 많은 쪽 사용"""
        params: dict = {"q": safe_q, "k": k}
        corp_clause = ""
        if corp_name:
            corp_clause = "AND dc.corp_name ILIKE :corp"
            params["corp"] = f"%{corp_name}%"

        # ① FTS (simple config) — 단어 단위 매칭
        fts_sql = text(f"""
            SELECT dc.rcp_no,
                   dc.corp_name,
                   dc.report_nm,
                   dc.chunk_text,
                   dc.meta,
                   d.rcept_dt,
                   ts_rank_cd(
                       to_tsvector('simple', dc.chunk_text),
                       plainto_tsquery('simple', :q)
                   ) AS score
            FROM   document_chunks dc
            JOIN   disclosures d ON dc.rcp_no = d.rcp_no
            WHERE  to_tsvector('simple', dc.chunk_text)
                   @@ plainto_tsquery('simple', :q)
                   {corp_clause}
            ORDER  BY score DESC
            LIMIT  :k
        """)
        try:
            fts_rows = (await db.execute(fts_sql, params)).fetchall()
        except Exception as e:
            logger.warning("FTS query failed: %s", e)
            fts_rows = []

        if len(fts_rows) >= 5:
            return [_row_to_dict(r, r.score) for r in fts_rows]

        # ② Trigram (pg_trgm) — FTS 결과 부족 시 폴백
        # 특히 짧은 한국어 쿼리, 고유명사, 오타에 강함
        trgm_sql = text(f"""
            SELECT dc.rcp_no,
                   dc.corp_name,
                   dc.report_nm,
                   dc.chunk_text,
                   dc.meta,
                   d.rcept_dt,
                   similarity(dc.chunk_text, :q) AS score
            FROM   document_chunks dc
            JOIN   disclosures d ON dc.rcp_no = d.rcp_no
            WHERE  dc.chunk_text % :q
                   {corp_clause}
            ORDER  BY score DESC
            LIMIT  :k
        """)
        try:
            trgm_rows = (await db.execute(trgm_sql, params)).fetchall()
            if trgm_rows:
                combined = list(fts_rows) + [r for r in trgm_rows
                                              if r.rcp_no not in {f.rcp_no for f in fts_rows}]
                return [_row_to_dict(r, r.score) for r in combined[:k]]
        except Exception as e:
            logger.debug("Trigram query failed (pg_trgm not enabled?): %s", e)

        return [_row_to_dict(r, r.score) for r in fts_rows]


# ══════════════════════════════════════════════
# RRF 통합
# ══════════════════════════════════════════════

def _rrf_merge(
    dense: list[dict],
    sparse: list[dict],
    alpha: float,
    top_k: int,
) -> list[dict]:
    """
    Reciprocal Rank Fusion
    RRF(d) = α · Σ 1/(k + rank_dense(d))
           + β · Σ 1/(k + rank_sparse(d))
    β = 1 - α
    """
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}

    beta = 1.0 - alpha

    for rank, doc in enumerate(dense):
        uid = _uid(doc)
        scores[uid] = scores.get(uid, 0.0) + alpha / (_RRF_K + rank + 1)
        doc["dense_score"] = round(doc.get("score", 0.0), 4)
        doc["bm25_score"] = 0.0
        docs[uid] = doc

    for rank, doc in enumerate(sparse):
        uid = _uid(doc)
        scores[uid] = scores.get(uid, 0.0) + beta / (_RRF_K + rank + 1)
        if uid in docs:
            docs[uid]["bm25_score"] = round(doc.get("score", 0.0), 4)
        else:
            doc["dense_score"] = 0.0
            doc["bm25_score"] = round(doc.get("score", 0.0), 4)
            docs[uid] = doc

    sorted_uids = sorted(scores, key=lambda u: scores[u], reverse=True)

    result = []
    for uid in sorted_uids[:top_k]:
        d = docs[uid]
        d["rrf_score"] = round(scores[uid], 6)
        result.append(d)

    return result


# ══════════════════════════════════════════════
# Python BM25
# ══════════════════════════════════════════════

def _bm25_score(query: str, texts: list[str]) -> list[float]:
    """
    rank_bm25 라이브러리 사용. 없으면 균등 점수 반환.
    한국어 토크나이저: 공백 분리 + 2글자 이상 토큰 유지.
    """
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except ImportError:
        logger.debug("rank_bm25 not installed — using uniform BM25 scores")
        return [1.0] * len(texts)

    def _tokenize(t: str) -> list[str]:
        # 공백/구두점 분리, 2글자 이상 토큰만 유지
        tokens = re.split(r"[\s\.,;:!?()「」『』【】\[\]]+", t)
        return [tok for tok in tokens if len(tok) >= 2]

    try:
        corpus = [_tokenize(t) for t in texts]
        bm25 = BM25Okapi(corpus, k1=1.5, b=0.75)
        scores = bm25.get_scores(_tokenize(query))
        # numpy array → python list
        return [float(s) for s in scores]
    except Exception as e:
        logger.warning("BM25 scoring error: %s", e)
        return [1.0] * len(texts)


# ══════════════════════════════════════════════
# 헬퍼
# ══════════════════════════════════════════════

def _row_to_dict(row, score: float) -> dict:
    meta: dict = {}
    try:
        if row.meta:
            meta = json.loads(row.meta) if isinstance(row.meta, str) else (row.meta or {})
    except Exception:
        pass
    return {
        "rcp_no":    row.rcp_no,
        "corp_name": row.corp_name,
        "report_nm": row.report_nm,
        "chunk_text": row.chunk_text,
        "rcept_dt":  row.rcept_dt or "",
        "score":     float(score),
        "section":   meta.get("section", ""),
        "has_table": meta.get("has_table", False),
    }


def _uid(doc: dict) -> str:
    """청크 고유 ID: 접수번호 + 텍스트 앞 60자"""
    return f"{doc['rcp_no']}::{doc['chunk_text'][:60]}"


def _sanitize_for_fts(query: str) -> str:
    """
    plainto_tsquery 에 안전하게 전달할 수 있도록 정제.
    특수문자 제거, 한글/영문/숫자/공백만 유지.
    """
    cleaned = re.sub(r"[^\w\s가-힣]", " ", query, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 너무 짧으면 빈 문자열 반환 (FTS 쿼리 실패 방지)
    return cleaned if len(cleaned) >= 2 else ""


# ══════════════════════════════════════════════
# 공용 진입점 — /search 와 /chat 양쪽에서 호출
# ══════════════════════════════════════════════

async def run_hybrid_search(
    db: AsyncSession,
    query: str,
    gemini: GeminiService,
    corp_name: Optional[str] = None,
    top_k: int = 5,
    alpha: float = 0.7,
    candidate_k: int = 40,
) -> list[dict]:
    """Dense + BM25 + RRF 검색. 나중에 리랭커를 붙일 때 이 함수만 수정."""
    svc = HybridSearchService(gemini, alpha, candidate_k)
    return await svc.search(db, query, corp_name, top_k)
