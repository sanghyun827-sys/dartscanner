-- pgvector 확장
CREATE EXTENSION IF NOT EXISTS vector;

-- 기업 코드 테이블
CREATE TABLE IF NOT EXISTS companies (
    id          SERIAL PRIMARY KEY,
    corp_code   VARCHAR(8)  UNIQUE NOT NULL,
    corp_name   VARCHAR(255) NOT NULL,
    stock_code  VARCHAR(6),
    modify_date VARCHAR(8)
);
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies (corp_name);

-- 공시 테이블
CREATE TABLE IF NOT EXISTS disclosures (
    id          SERIAL PRIMARY KEY,
    rcp_no      VARCHAR(14)  UNIQUE NOT NULL,
    corp_code   VARCHAR(8)   NOT NULL,
    corp_name   VARCHAR(255),
    report_nm   VARCHAR(500),
    rcept_dt    VARCHAR(8),
    flr_nm      VARCHAR(255),
    rm          VARCHAR(20),
    is_embedded SMALLINT     DEFAULT 0,
    created_at  TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_disclosures_corp_code ON disclosures (corp_code);
CREATE INDEX IF NOT EXISTS idx_disclosures_rcept_dt  ON disclosures (rcept_dt);

-- 문서 청크 + 벡터 테이블
CREATE TABLE IF NOT EXISTS document_chunks (
    id             SERIAL PRIMARY KEY,
    disclosure_id  INTEGER REFERENCES disclosures(id) ON DELETE CASCADE,
    rcp_no         VARCHAR(14)  NOT NULL,
    corp_name      VARCHAR(255),
    report_nm      VARCHAR(500),
    chunk_text     TEXT         NOT NULL,
    chunk_index    INTEGER,
    embedding      vector(768),
    meta           JSONB,
    created_at     TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chunks_rcp_no    ON document_chunks (rcp_no);
CREATE INDEX IF NOT EXISTS idx_chunks_corp_name ON document_chunks (corp_name);

-- HNSW 벡터 인덱스 (코사인 유사도)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
