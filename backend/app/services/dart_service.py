"""
DART 문서 수집 및 파싱 파이프라인

흐름:
  ZIP bytes
    → _select_files()          HTML/XML 파일 우선순위 정렬
    → _parse_html()            태그 정규화 (표→마크다운, 헤딩→#, 목록→-)
    → _clean_noise()           노이즈 제거 (페이지번호, 반복 헤더, 법적고지 등)
    → _extract_metadata()      메타데이터 추출 (제목, 날짜, 섹션 구조)
    → chunk_with_metadata()    의미 단위 청킹 (섹션→문단→문장 계층 분리)
"""

import httpx
import zipfile
import io
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup, Tag, NavigableString

from . import quota_service

logger = logging.getLogger(__name__)
DART_BASE_URL = "https://opendart.fss.or.kr/api"

# ──────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────

@dataclass
class ParsedSection:
    header: str           # ## 재무상태표
    level: int            # 1=##  2=###  0=본문(헤더 없음)
    content: str          # 마크다운 정제 본문
    has_table: bool = False
    source_file: str = ""


@dataclass
class ParsedDocument:
    title: str = ""
    report_date: str = ""
    sections: list[ParsedSection] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# DartService
# ──────────────────────────────────────────────

class DartService:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60.0, follow_redirects=True)

    # ── API 호출 ───────────────────────────────

    async def get_disclosure_list(
        self,
        corp_code: Optional[str] = None,
        bgn_de: Optional[str] = None,
        end_de: Optional[str] = None,
        pblntf_ty: Optional[str] = None,
        page_no: int = 1,
        page_count: int = 20,
    ) -> dict:
        params = {
            "crtfc_key": self.api_key,
            "page_no": page_no,
            "page_count": min(page_count, 100),
        }
        if corp_code:
            params["corp_code"] = corp_code
        if bgn_de:
            params["bgn_de"] = bgn_de
        if end_de:
            params["end_de"] = end_de
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty

        async with self._client() as client:
            quota_service.increment()
            resp = await client.get(f"{DART_BASE_URL}/list.json", params=params)
            resp.raise_for_status()
            return resp.json()

    async def download_corp_codes(self) -> bytes:
        async with self._client() as client:
            resp = await client.get(
                f"{DART_BASE_URL}/corpCode.xml",
                params={"crtfc_key": self.api_key},
            )
            resp.raise_for_status()
            return resp.content

    async def download_document(self, rcp_no: str) -> Optional[bytes]:
        async with self._client() as client:
            try:
                quota_service.increment()
                resp = await client.get(
                    f"{DART_BASE_URL}/document.json",
                    params={"crtfc_key": self.api_key, "rcp_no": rcp_no},
                )
                resp.raise_for_status()
                if resp.content[:4] == b"PK\x03\x04":
                    return resp.content
                ct = resp.headers.get("content-type", "")
                if "zip" in ct or "octet" in ct:
                    return resp.content
                logger.warning(f"Unexpected content-type {rcp_no}: {ct}")
                return resp.content
            except Exception as e:
                logger.error(f"Document download failed {rcp_no}: {e}")
                return None

    # ── 기업 코드 파싱 ─────────────────────────

    def parse_corp_codes_xml(self, zip_bytes: bytes) -> list[dict]:
        companies = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".xml"):
                        with zf.open(name) as f:
                            soup = BeautifulSoup(f.read(), "lxml-xml")
                            for item in soup.find_all("list"):
                                corp_code = _tag_text(item, "corp_code")
                                corp_name = _tag_text(item, "corp_name")
                                if corp_code and corp_name:
                                    companies.append({
                                        "corp_code": corp_code,
                                        "corp_name": corp_name,
                                        "stock_code": _tag_text(item, "stock_code") or "",
                                        "modify_date": _tag_text(item, "modify_date") or "",
                                    })
        except Exception as e:
            logger.error(f"Corp codes parse error: {e}")
        return companies

    # ════════════════════════════════════════════
    # 공시 문서 파싱 (핵심)
    # ════════════════════════════════════════════

    def parse_document_zip(self, zip_bytes: bytes) -> ParsedDocument:
        """ZIP → ParsedDocument (구조화된 마크다운 + 메타데이터)"""
        doc = ParsedDocument()
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                names = _select_files(zf.namelist())
                for fname in names[:10]:
                    try:
                        with zf.open(fname) as f:
                            raw = f.read()
                        sections = self._parse_file(raw, fname)
                        if sections:
                            doc.sections.extend(sections)
                            doc.source_files.append(fname)
                            if not doc.title:
                                doc.title = _infer_title(sections)
                    except Exception as e:
                        logger.debug(f"File parse error ({fname}): {e}")
        except zipfile.BadZipFile:
            logger.error("Invalid ZIP")
        return doc

    def _parse_file(self, raw: bytes, filename: str) -> list[ParsedSection]:
        """단일 파일(HTML/XML) → 섹션 목록"""
        enc = _detect_encoding(raw)
        try:
            if filename.lower().endswith((".html", ".htm")):
                return self._parse_html(raw, enc, filename)
            else:
                return self._parse_xml(raw, enc, filename)
        except Exception as e:
            logger.debug(f"Parse error ({filename}): {e}")
            return []

    # ── HTML 파싱 ──────────────────────────────

    def _parse_html(self, raw: bytes, enc: str, filename: str) -> list[ParsedSection]:
        soup = BeautifulSoup(raw, "html.parser", from_encoding=enc)

        # 1. 노이즈 태그 제거
        for tag in soup(["script", "style", "head", "meta", "link",
                         "noscript", "iframe", "object"]):
            tag.decompose()

        # 2. 본문 영역 추출 (없으면 body 전체)
        body = soup.find("body") or soup

        # 3. 구조를 마크다운 문자열로 변환
        md = _node_to_markdown(body)

        # 4. 노이즈 라인 제거
        md = _clean_noise(md)

        if len(md.strip()) < 80:
            return []

        # 5. 메타데이터 추출
        title = _extract_html_title(soup)
        report_date = _extract_date(md)

        # 6. 헤더 기준으로 섹션 분리
        sections = _split_into_sections(md, filename, title)
        return sections

    # ── XML 파싱 ──────────────────────────────

    def _parse_xml(self, raw: bytes, enc: str, filename: str) -> list[ParsedSection]:
        soup = BeautifulSoup(raw, "lxml-xml", from_encoding=enc)
        text = soup.get_text(separator="\n", strip=True)
        text = _clean_noise(text)
        if len(text.strip()) < 80:
            return []
        return [ParsedSection(header="", level=0, content=text,
                              has_table=False, source_file=filename)]

    # ════════════════════════════════════════════
    # 청킹 (의미 단위)
    # ════════════════════════════════════════════

    def chunk_with_metadata(
        self,
        doc: ParsedDocument,
        chunk_size: int = 800,
        overlap: int = 150,
    ) -> list[dict]:
        """
        ParsedDocument → [{"text": str, "metadata": dict}, ...]
        계층 우선순위: 섹션 > 문단 > 문장
        """
        result: list[dict] = []

        for sec in doc.sections:
            if not sec.content.strip():
                continue

            prefix = f"{sec.header}\n\n" if sec.header else ""
            sub_chunks = _semantic_split(sec.content, chunk_size, overlap)

            for sc in sub_chunks:
                text = (prefix + sc).strip()
                if len(text) < 40:
                    continue
                result.append({
                    "text": text,
                    "metadata": {
                        "title": doc.title,
                        "section": sec.header,
                        "source_file": sec.source_file,
                        "has_table": sec.has_table,
                    },
                })

        # chunk_index / total 추가
        total = len(result)
        for i, item in enumerate(result):
            item["metadata"]["chunk_index"] = i
            item["metadata"]["total_chunks"] = total

        return result

    # ── 하위 호환 래퍼 ─────────────────────────

    def extract_text_from_document_zip(self, zip_bytes: bytes) -> str:
        """하위 호환: 전체 텍스트 문자열 반환"""
        doc = self.parse_document_zip(zip_bytes)
        parts = [sec.content for sec in doc.sections if sec.content.strip()]
        return "\n\n".join(parts)

    def chunk_text(self, text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
        """하위 호환: 문자열 청크 목록 반환"""
        if not text:
            return []
        return [item["text"] for item in _semantic_split_simple(text, chunk_size, overlap)]

    def chunk_with_parent_child(
        self,
        doc: ParsedDocument,
        child_size: int = 800,
        overlap: int = 150,
        parent_threshold: int = 1000,
        table_hard_split_threshold: int = 5000,
    ) -> list[dict]:
        """
        ParsedDocument → 부모-자식 청크 그룹 목록.

        반환 형식:
          {
            "parent_text": str,          # LLM 컨텍스트용 전체 텍스트
            "chunk_type": "table"|"paragraph",
            "has_table": bool,
            "metadata": dict,
            "children": list[{"text", "metadata"}]
          }

        children == []  → parent_text 자체를 임베딩 (부모=자식)
        children != []  → parent_text는 LLM 주입용, children 각각 임베딩
        """
        all_groups: list[dict] = []

        for sec in doc.sections:
            if not sec.content.strip():
                continue

            prefix = f"{sec.header}\n\n" if sec.header else ""
            blocks = _classify_blocks(sec.content)
            raw_groups = _build_parent_groups(blocks, sec.header, sec.source_file, doc.title)

            for g in raw_groups:
                parent_text = (prefix + g["parent_text"]).strip()
                g["parent_text"] = parent_text
                parent_len = len(parent_text)

                if parent_len <= parent_threshold:
                    # 부모 = 자식 (임베딩 대상, 단일 행)
                    g["children"] = []

                elif g["chunk_type"] == "table":
                    if parent_len <= table_hard_split_threshold:
                        # 표: 크기 무관 통째로 보존 (헤더-행 분리 불가 원칙)
                        g["children"] = []
                    else:
                        # 초대형 표: 헤더 복제 행 그룹 분할
                        sub_tables = _split_table_with_header(g["parent_text"], child_size * 2)
                        g["children"] = [
                            {"text": st.strip(), "metadata": {**g["metadata"]}}
                            for st in sub_tables
                            if len(st.strip()) >= 40
                        ]

                else:
                    # 서술 문단: 시멘틱 분할
                    sub_chunks = _semantic_split(g["parent_text"], child_size, overlap)
                    g["children"] = [
                        {"text": sc.strip(), "metadata": {**g["metadata"]}}
                        for sc in sub_chunks
                        if len(sc.strip()) >= 40
                    ]
                    # 분할 실패 안전망
                    if not g["children"]:
                        g["children"] = []

                all_groups.append(g)

        # chunk_index / total 채우기 (임베딩 대상에만)
        embed_items: list[dict] = []
        for g in all_groups:
            if not g["children"]:
                embed_items.append(g)
            else:
                embed_items.extend(g["children"])

        total = len(embed_items)
        for i, t in enumerate(embed_items):
            t["metadata"]["chunk_index"] = i
            t["metadata"]["total_chunks"] = total

        return all_groups


# ══════════════════════════════════════════════
# HTML → 마크다운 변환
# ══════════════════════════════════════════════

_HEADING_MAP = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}

# 제거할 불필요 클래스 키워드
_NOISE_CLASSES = {
    "nav", "navigation", "breadcrumb", "sidebar", "menu", "footer",
    "header", "pagination", "copyright", "print", "toc", "contents",
}


def _node_to_markdown(node, depth: int = 0) -> str:
    """BeautifulSoup 노드 → 마크다운 문자열 (재귀)"""
    if isinstance(node, NavigableString):
        return str(node)

    if not isinstance(node, Tag):
        return ""

    tag = node.name.lower() if node.name else ""

    # 노이즈 클래스 건너뜀
    cls = " ".join(node.get("class", [])).lower()
    if any(nc in cls for nc in _NOISE_CLASSES):
        return ""

    # ── 변환 규칙 ──────────────────────────────

    # 헤딩 → # 마크다운
    if tag in _HEADING_MAP:
        text = node.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        if len(text) > 2:
            return f"\n\n{_HEADING_MAP[tag]} {text}\n\n"
        return ""

    # 테이블 → 마크다운 표
    if tag == "table":
        md_table = _table_to_markdown(node)
        return f"\n\n{md_table}\n\n" if md_table else ""

    # 순서 있는 목록
    if tag == "ol":
        items = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            content = _node_to_markdown(li, depth + 1).strip()
            if content:
                items.append(f"{i}. {content}")
        return "\n" + "\n".join(items) + "\n" if items else ""

    # 순서 없는 목록
    if tag == "ul":
        items = []
        for li in node.find_all("li", recursive=False):
            content = _node_to_markdown(li, depth + 1).strip()
            if content:
                items.append(f"- {content}")
        return "\n" + "\n".join(items) + "\n" if items else ""

    # 단락
    if tag == "p":
        inner = _children_to_markdown(node, depth)
        inner = inner.strip()
        return f"\n\n{inner}\n\n" if inner else ""

    # 줄바꿈
    if tag == "br":
        return "\n"

    # 강조 (굵게) — 수치 강조 유지
    if tag in ("b", "strong"):
        inner = _children_to_markdown(node, depth).strip()
        return f"**{inner}**" if inner else ""

    # 이탤릭
    if tag in ("i", "em"):
        inner = _children_to_markdown(node, depth).strip()
        return f"_{inner}_" if inner else ""

    # 구분선
    if tag == "hr":
        return "\n\n---\n\n"

    # div, span, td, th 등 — 자식 재귀 처리
    return _children_to_markdown(node, depth)


def _children_to_markdown(node: Tag, depth: int) -> str:
    return "".join(_node_to_markdown(child, depth) for child in node.children)


# ── 테이블 → 마크다운 표 ──────────────────────

def _table_to_markdown(table: Tag) -> str:
    """<table> → 마크다운 파이프 표"""
    # 헤더 행 추출
    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        tr = thead.find("tr")
        if tr:
            headers = [_cell_text(c) for c in tr.find_all(["th", "td"])]

    # 데이터 행 수집
    tbody = table.find("tbody") or table
    all_trs = tbody.find_all("tr", recursive=False)
    if not all_trs:
        all_trs = [tr for tr in table.find_all("tr") if not tr.find_parent("thead")]

    data_rows: list[list[str]] = []
    for tr in all_trs:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = [_cell_text(c) for c in cells]

        # 첫 행을 헤더로 쓸지 판단
        if not headers and all(c.name == "th" for c in cells):
            headers = row
            continue

        data_rows.append(row)

    if not headers and data_rows:
        headers = data_rows.pop(0)

    if not headers:
        return ""

    # 빈 표 건너뜀
    if not data_rows:
        return ""

    n = len(headers)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * n) + " |",
    ]
    for row in data_rows:
        # 열 수 맞추기
        padded = (row + [""] * n)[:n]
        lines.append("| " + " | ".join(padded) + " |")

    return "\n".join(lines)


def _cell_text(cell: Tag) -> str:
    txt = cell.get_text(" ", strip=True)
    # 파이프 문자 이스케이프, 줄바꿈 제거
    return re.sub(r"\s+", " ", txt).replace("|", "\\|").replace("\n", " ")[:120]


# ══════════════════════════════════════════════
# 노이즈 제거
# ══════════════════════════════════════════════

# DART 문서 공통 노이즈 패턴
_NOISE_PATTERNS = [
    re.compile(r"^[-─━\s]{3,}$"),                        # 구분선만 있는 행
    re.compile(r"^[-\s]*\d{1,3}\s*[-\s]*$"),             # 페이지 번호: - 5 -
    re.compile(r"^page\s+\d+\s*(of\s+\d+)?$", re.I),     # Page 3 of 10
    re.compile(r"^\s*\d+\s*$"),                           # 숫자만 있는 행 (페이지)
    re.compile(r"^※\s*(이\s+보고서|본\s+보고서|금융감독원)"),  # 법적 고지 시작
    re.compile(r"^(이\s+문서|본\s+문서|전자공시시스템)"),
    re.compile(r"^(작성일|제출일|수정일)\s*[:：]"),
    re.compile(r"^DART\s+전자공시시스템"),
]

# 동일 문자 반복 라인 (헤더/푸터 장식)
_REPEAT_CHAR = re.compile(r"^(.)\1{4,}$")

# 단위 표기 탐지 — "(단위: 백만원)", "단위 : 천원", "(단위:원)" 등
# 표 헤더 셀 내 단위는 파이프 행이므로 여기 매칭되지 않음
_UNIT_LINE_RE = re.compile(
    r"^\s*"           # 행 시작 공백
    r"\(?"            # 여는 괄호 선택
    r"단위"           # 키워드
    r"\s*[:：]\s*"    # 반각/전각 콜론 (앞뒤 공백 허용)
    r"[^\)\n\r]+"     # 단위 텍스트 (닫는 괄호·줄바꿈 이전까지)
    r"\)?"            # 닫는 괄호 선택
    r"\s*$"           # 행 끝
)


def _clean_noise(text: str) -> str:
    """노이즈 라인 제거 + 과도한 공백 정리"""
    lines = text.split("\n")
    cleaned = []
    prev_blank = 0

    for line in lines:
        stripped = line.strip()

        # 완전히 빈 줄 연속 3개 이상 → 최대 2개
        if not stripped:
            prev_blank += 1
            if prev_blank <= 2:
                cleaned.append("")
            continue
        prev_blank = 0

        # 노이즈 패턴 제거
        if any(p.match(stripped) for p in _NOISE_PATTERNS):
            continue

        # 반복 문자 (===, ----, ~~~~)
        if _REPEAT_CHAR.match(stripped):
            continue

        # 아주 짧은 라인 중 의미 없는 것 (단일 특수문자)
        if len(stripped) == 1 and not stripped.isalnum():
            continue

        cleaned.append(line.rstrip())

    result = "\n".join(cleaned)
    # 3개 이상 연속 빈줄 → 2개로
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ══════════════════════════════════════════════
# 메타데이터 추출
# ══════════════════════════════════════════════

_DATE_PATTERN = re.compile(
    r"(20\d{2})[.\-년\s/](\d{1,2})[.\-월\s/](\d{1,2})"
)


def _extract_html_title(soup: BeautifulSoup) -> str:
    """HTML에서 문서 제목 추출"""
    # 1. <title> 태그
    title_tag = soup.find("title")
    if title_tag:
        t = title_tag.get_text(strip=True)
        if t and len(t) > 2:
            return t

    # 2. 첫 번째 <h1>/<h2>
    for tag in ("h1", "h2"):
        el = soup.find(tag)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 2:
                return t
    return ""


def _extract_date(text: str) -> str:
    """텍스트에서 날짜 추출 (YYYYMMDD)"""
    m = _DATE_PATTERN.search(text)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return ""


def _infer_title(sections: list[ParsedSection]) -> str:
    """섹션에서 문서 제목 추론"""
    for sec in sections:
        if sec.header and len(sec.header) > 2:
            return sec.header.lstrip("#").strip()
    return ""


# ══════════════════════════════════════════════
# 섹션 분리
# ══════════════════════════════════════════════

_HEADER_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)


def _split_into_sections(md: str, source_file: str, doc_title: str) -> list[ParsedSection]:
    """마크다운 텍스트 → 헤더 기준 섹션 목록"""
    matches = list(_HEADER_RE.finditer(md))

    if not matches:
        # 헤더 없는 문서 → 단일 섹션
        content = md.strip()
        if content:
            return [ParsedSection(
                header=doc_title,
                level=0,
                content=content,
                has_table="|" in content,
                source_file=source_file,
            )]
        return []

    sections: list[ParsedSection] = []

    # 첫 헤더 앞의 내용 (서문)
    if matches[0].start() > 0:
        preamble = md[:matches[0].start()].strip()
        if len(preamble) > 40:
            sections.append(ParsedSection(
                header=doc_title or "",
                level=0,
                content=preamble,
                has_table="|" in preamble,
                source_file=source_file,
            ))

    for i, m in enumerate(matches):
        level = len(m.group(1))   # # 개수
        header = m.group(2).strip()

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        content = md[start:end].strip()

        if not content and not header:
            continue

        sections.append(ParsedSection(
            header=header,
            level=level,
            content=content,
            has_table="|" in content,
            source_file=source_file,
        ))

    return sections


# ══════════════════════════════════════════════
# 의미 단위 청킹
# ══════════════════════════════════════════════

# 한국어/영어 문장 종결 패턴
_SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s+|(?<=다\.)\s+|(?<=요\.)\s+")


def _semantic_split(text: str, max_size: int, overlap: int) -> list[str]:
    """
    1차: 이중 개행(문단) 기준 분리
    2차: 문단이 여전히 크면 문장 기준 분리
    3차: 문장도 크면 강제 분리 (글자수)
    오버랩: 앞 청크 끝 N 글자를 다음 청크 앞에 붙임
    """
    if len(text) <= max_size:
        return [text]

    # 1차: 문단 분리
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    buf = ""

    for para in paragraphs:
        # 문단 자체가 max_size 초과
        if len(para) > max_size:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            # 2차: 문장 분리
            sentence_chunks = _split_by_sentence(para, max_size, overlap)
            chunks.extend(sentence_chunks)
            continue

        candidate = buf + ("\n\n" if buf else "") + para
        if len(candidate) <= max_size:
            buf = candidate
        else:
            if buf:
                chunks.append(buf.strip())
            buf = para

    if buf.strip():
        chunks.append(buf.strip())

    # 오버랩 추가
    if overlap > 0 and len(chunks) > 1:
        overlapped: list[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            overlapped.append(tail + "\n\n" + chunks[i])
        return overlapped

    return chunks


def _split_by_sentence(text: str, max_size: int, overlap: int) -> list[str]:
    """문장 기준 분리 (문단보다 작은 단위)"""
    sentences = _SENTENCE_END.split(text)
    chunks: list[str] = []
    buf = ""

    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue

        # 문장 하나가 너무 크면 강제 분리
        if len(sent) > max_size:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_force_split(sent, max_size, overlap))
            continue

        candidate = buf + (" " if buf else "") + sent
        if len(candidate) <= max_size:
            buf = candidate
        else:
            if buf:
                chunks.append(buf.strip())
            buf = sent

    if buf.strip():
        chunks.append(buf.strip())

    return chunks if chunks else [text]


def _force_split(text: str, max_size: int, overlap: int) -> list[str]:
    """단어 경계 기준 강제 분리"""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        # 단어 경계 탐색
        if end < len(text):
            boundary = text.rfind(" ", start + max_size // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(text[start:end].strip())
        start = max(start + 1, end - overlap)
    return chunks


def _semantic_split_simple(text: str, max_size: int, overlap: int) -> list[dict]:
    """하위 호환용: 텍스트 → [{text}] 목록"""
    return [{"text": t} for t in _semantic_split(text, max_size, overlap)]


# ══════════════════════════════════════════════
# 부모-자식 청킹 헬퍼
# ══════════════════════════════════════════════

def _is_table_block(text: str) -> bool:
    """파이프 행이 2줄 이상 연속 → 마크다운 표 블록"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return sum(1 for l in lines if l.startswith("|") and l.endswith("|")) >= 2


def _is_unit_block(text: str) -> bool:
    """단독 줄 단위 표기 블록 여부 — 표 헤더 셀 내 단위는 파이프 행이므로 제외"""
    lines = [l for l in text.split("\n") if l.strip()]
    return len(lines) == 1 and bool(_UNIT_LINE_RE.match(lines[0].strip()))


def _classify_blocks(text: str) -> list[dict]:
    """마크다운 섹션 content → [{"type": "table"|"unit"|"paragraph", "text": str}]"""
    result: list[dict] = []
    for raw in re.split(r"\n{2,}", text):
        block = raw.strip()
        if not block:
            continue
        if _is_table_block(block):
            result.append({"type": "table", "text": block})
        elif _is_unit_block(block):
            result.append({"type": "unit", "text": block})
        else:
            result.append({"type": "paragraph", "text": block})
    return result


def _build_parent_groups(
    blocks: list[dict],
    section: str,
    source_file: str,
    doc_title: str,
) -> list[dict]:
    """
    블록 목록 → 부모 그룹 목록 (children 미포함, parent_text만 구성).

    규칙:
    - UNIT → 직후 TABLE에만 귀속 (연속 표 중 첫 번째에만)
    - TABLE 직전 PARAGRAPH 1개까지 같은 부모에 흡수
    - 연속 TABLE은 각각 독립 부모 (두 번째 표에는 단위 표기 미귀속)
    """
    groups: list[dict] = []
    pending_para: Optional[str] = None
    pending_unit: Optional[str] = None
    n = len(blocks)

    def _make_meta(has_table: bool) -> dict:
        return {
            "title": doc_title,
            "section": section,
            "source_file": source_file,
            "has_table": has_table,
        }

    i = 0
    while i < n:
        b = blocks[i]

        if b["type"] == "paragraph":
            if pending_para is not None:
                groups.append({
                    "parent_text": pending_para,
                    "chunk_type": "paragraph",
                    "has_table": False,
                    "metadata": _make_meta(False),
                })
            pending_para = b["text"]
            pending_unit = None
            i += 1

        elif b["type"] == "unit":
            next_is_table = (i + 1 < n and blocks[i + 1]["type"] == "table")
            if next_is_table:
                pending_unit = b["text"]
                i += 1
            else:
                # 표 없이 단위 표기만 → 문단 취급
                if pending_para is not None:
                    groups.append({
                        "parent_text": pending_para,
                        "chunk_type": "paragraph",
                        "has_table": False,
                        "metadata": _make_meta(False),
                    })
                pending_para = b["text"]
                pending_unit = None
                i += 1

        elif b["type"] == "table":
            parts: list[str] = []
            if pending_para is not None:
                parts.append(pending_para)
                pending_para = None
            if pending_unit is not None:
                parts.append(pending_unit)
                pending_unit = None
            parts.append(b["text"])

            groups.append({
                "parent_text": "\n\n".join(parts),
                "chunk_type": "table",
                "has_table": True,
                "metadata": _make_meta(True),
            })
            i += 1

    # 미소비 pending_para 처리
    if pending_para is not None:
        groups.append({
            "parent_text": pending_para,
            "chunk_type": "paragraph",
            "has_table": False,
            "metadata": _make_meta(False),
        })

    return groups


def _split_table_with_header(table_text: str, max_chars: int) -> list[str]:
    """
    초대형 표(> TABLE_HARD_SPLIT_THRESHOLD) 행 그룹 분할.
    각 자식 청크에 헤더 행 + 구분 행 복제.
    """
    lines = [l for l in table_text.split("\n") if l.strip()]
    header_lines: list[str] = []
    data_lines: list[str] = []
    past_separator = False

    for line in lines:
        if not past_separator:
            header_lines.append(line)
            if re.match(r"^\|[\s\-:|]+\|$", line.strip()):
                past_separator = True
        else:
            data_lines.append(line)

    if not data_lines or not header_lines:
        return [table_text]

    header_text = "\n".join(header_lines)
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = len(header_text) + 1

    for line in data_lines:
        line_len = len(line) + 1
        if buf_len + line_len > max_chars and buf:
            chunks.append(header_text + "\n" + "\n".join(buf))
            buf = [line]
            buf_len = len(header_text) + 1 + line_len
        else:
            buf.append(line)
            buf_len += line_len

    if buf:
        chunks.append(header_text + "\n" + "\n".join(buf))

    return chunks or [table_text]


# ══════════════════════════════════════════════
# 파일 선택
# ══════════════════════════════════════════════

def _select_files(names: list[str]) -> list[str]:
    """
    우선순위:
    1. .html / .htm (본문 문서)
    2. .xml (보조 데이터)
    .xsd / .css / .js 제외
    """
    skip_exts = {".xsd", ".css", ".js", ".png", ".jpg", ".gif", ".xbrl"}
    html_files = []
    xml_files = []
    for n in names:
        lo = n.lower()
        if any(lo.endswith(e) for e in skip_exts):
            continue
        if lo.endswith((".html", ".htm")):
            html_files.append(n)
        elif lo.endswith(".xml"):
            xml_files.append(n)
    # 파일명 정렬 (보통 0001.html 이 본문)
    html_files.sort()
    xml_files.sort()
    return html_files + xml_files


# ══════════════════════════════════════════════
# 내부 유틸
# ══════════════════════════════════════════════

def _tag_text(tag, name: str) -> Optional[str]:
    el = tag.find(name)
    return el.get_text(strip=True) if el else None


def _detect_encoding(raw: bytes) -> str:
    """HTML 메타 charset 우선, 없으면 시도"""
    # <meta charset="..."> 또는 <meta http-equiv="Content-Type">
    snippet = raw[:4096]
    for pattern in (
        rb'charset\s*=\s*["\']?\s*([a-zA-Z0-9_-]+)',
        rb'encoding\s*=\s*["\']([a-zA-Z0-9_-]+)',
    ):
        m = re.search(pattern, snippet, re.I)
        if m:
            enc = m.group(1).decode("ascii", errors="ignore").lower()
            enc = enc.replace("ks_c_5601-1987", "cp949").replace("euc_kr", "euc-kr")
            try:
                raw[:2000].decode(enc)
                return enc
            except Exception:
                pass

    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            raw[:2000].decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"
