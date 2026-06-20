from pydantic import BaseModel
from typing import Optional, List, Any


class CompanyOut(BaseModel):
    corp_code: str
    corp_name: str
    stock_code: Optional[str] = None


class DisclosureItem(BaseModel):
    rcp_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: str
    flr_nm: Optional[str] = None
    rm: Optional[str] = None
    is_embedded: int = 0


class DisclosureListResponse(BaseModel):
    items: List[DisclosureItem]
    total_count: int
    page_no: int
    message: Optional[str] = None


class EmbedStatusResponse(BaseModel):
    rcp_no: str
    is_embedded: int
    status_text: str


class ChatRequest(BaseModel):
    question: str
    corp_name: Optional[str] = None
    top_k: Optional[int] = None   # None이면 config 기본값(10) 사용


class ChatSource(BaseModel):
    rcp_no: str
    corp_name: str
    report_nm: str
    similarity: float


class ChatResponse(BaseModel):
    answer: str
    sources: List[ChatSource]
