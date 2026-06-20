from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # DART
    dart_api_key: str = ""

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    embedding_model: str = "models/text-embedding-004"
    embedding_dim: int = 768

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://dart:dart@db:5432/dartscanner"

    # GCS (선택)
    gcs_bucket_name: Optional[str] = None
    gcs_credentials_path: Optional[str] = None

    # GCP VM (선택 - GPU 파싱용)
    gcp_project_id: Optional[str] = None
    gcp_zone: Optional[str] = None
    gcp_instance_name: Optional[str] = None

    # RAG 튜닝
    chunk_size: int = 800
    chunk_overlap: int = 150
    top_k: int = 10

    # Parent-child 청킹
    # 한국어 600토큰 ≈ 1000자 (글자수 근사)
    parent_threshold: int = 1000
    # 이 이상이면 표도 행 그룹 분할 허용 (헤더 복제)
    table_hard_split_threshold: int = 5000

    # Hybrid Search 튜닝
    # alpha: 벡터 가중치 (0=BM25만, 1=벡터만)
    hybrid_alpha: float = 0.7
    # 각 검색 방식에서 가져올 후보 수 (top_k 보다 크게)
    hybrid_candidate_k: int = 40

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
