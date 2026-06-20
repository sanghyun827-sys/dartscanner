#!/usr/bin/env python3
"""
파일럿: parent-child 청킹 샘플 출력 (DB/API 불필요)

사용법:
  # raw_documents/ 폴더에서 자동 탐색 (최대 3개)
  python pilot_chunking.py

  # ZIP 파일 경로 직접 지정
  python pilot_chunking.py /app/raw_documents/20240101000001.zip

  # 여러 파일
  python pilot_chunking.py file1.zip file2.zip
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.services.dart_service import DartService

RAW_DOCS_DIR = os.environ.get("RAW_DOCS_DIR", "/app/raw_documents")
PARENT_THRESHOLD = 1000
TABLE_HARD_SPLIT = 5000
CHILD_SIZE = 800


def _preview(text: str, width: int = 100) -> str:
    return text[:width].replace("\n", " ↵ ")


def report_one(zip_path: str, dart: DartService) -> None:
    rcp_no = os.path.basename(zip_path).replace(".zip", "")

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    doc = dart.parse_document_zip(zip_bytes)
    groups = dart.chunk_with_parent_child(
        doc,
        child_size=CHILD_SIZE,
        overlap=150,
        parent_threshold=PARENT_THRESHOLD,
        table_hard_split_threshold=TABLE_HARD_SPLIT,
    )
    old_chunks = dart.chunk_with_metadata(doc, CHILD_SIZE, 150)

    # ── 통계 ──────────────────────────────────
    table_groups  = [g for g in groups if g["chunk_type"] == "table"]
    para_groups   = [g for g in groups if g["chunk_type"] == "paragraph"]
    tbl_self      = [g for g in table_groups if not g["children"]]
    tbl_split     = [g for g in table_groups if g["children"]]
    para_self     = [g for g in para_groups  if not g["children"]]
    para_split    = [g for g in para_groups  if g["children"]]
    embed_count   = sum(max(1, len(g["children"])) for g in groups)

    print(f"\n{'='*65}")
    print(f"  rcp_no : {rcp_no}")
    print(f"  문서제목: {doc.title or '(없음)'}")
    print(f"  소스파일: {', '.join(doc.source_files)}")
    print(f"{'='*65}")
    print(f"  섹션 수          : {len(doc.sections)}")
    print(f"  [비교] 구 청크 수: {len(old_chunks):>4}  →  신 임베딩 대상: {embed_count}")
    print(f"\n  부모 그룹 총 {len(groups)}개")
    print(f"    표 그룹  : {len(table_groups):>3}  (부모=자식: {len(tbl_self)}, 행그룹분할: {len(tbl_split)})")
    print(f"    서술 그룹: {len(para_groups):>3}  (부모=자식: {len(para_self)}, 시멘틱분할: {len(para_split)})")

    # ── 표 샘플 ───────────────────────────────
    print(f"\n{'─'*65}")
    print("  [표 부모 샘플 — 최대 4개]")
    for g in table_groups[:4]:
        sec   = (g["metadata"].get("section") or "(헤더없음)")[:35]
        plen  = len(g["parent_text"])
        nc    = len(g["children"])
        kind  = f"부모=자식 ({plen}자)" if nc == 0 else f"행그룹분할 자식{nc}개 ({plen}자)"
        print(f"\n  [{sec}] {kind}")
        print(f"    미리보기: {_preview(g['parent_text'])}")
        for j, c in enumerate(g["children"][:2]):
            print(f"    └ 자식#{j+1} ({len(c['text'])}자): {_preview(c['text'], 80)}")
        if len(g["children"]) > 2:
            print(f"    └ ... 외 {len(g['children'])-2}개")

    # ── 서술 분할 샘플 ────────────────────────
    if para_split:
        print(f"\n{'─'*65}")
        print("  [서술 분할 샘플 — 최대 2개]")
        for g in para_split[:2]:
            sec  = (g["metadata"].get("section") or "(헤더없음)")[:35]
            plen = len(g["parent_text"])
            nc   = len(g["children"])
            print(f"\n  [{sec}] 부모 {plen}자 → 자식 {nc}개")
            print(f"    부모 미리보기: {_preview(g['parent_text'])}")
            for j, c in enumerate(g["children"][:2]):
                print(f"    └ 자식#{j+1} ({len(c['text'])}자): {_preview(c['text'], 80)}")

    # ── 단위 표기 흡수 확인 ───────────────────
    unit_absorbed = [
        g for g in table_groups
        if "단위" in g["parent_text"] and "단위" not in (g["parent_text"].split("단위")[0])[-5:]
    ]
    # 더 정확한 체크: 표 앞에 "단위:" 패턴이 있는지
    import re
    _unit_re = re.compile(r"\(?단위\s*[:：]")
    unit_groups = [g for g in table_groups if _unit_re.search(g["parent_text"])]
    print(f"\n{'─'*65}")
    print(f"  단위 표기 흡수 확인: 표 부모 {len(table_groups)}개 중 {len(unit_groups)}개에 단위 표기 포함")
    for g in unit_groups[:3]:
        sec  = (g["metadata"].get("section") or "(헤더없음)")[:35]
        # 단위 표기 줄 추출
        lines = g["parent_text"].split("\n")
        unit_line = next((l.strip() for l in lines if _unit_re.search(l)), "")
        print(f"    [{sec}]: {unit_line}")


def main():
    dart = DartService(api_key="")
    zip_paths: list[str] = []

    for arg in sys.argv[1:]:
        if os.path.exists(arg):
            zip_paths.append(arg)
        else:
            print(f"  경고: 파일 없음 → {arg}")

    if not zip_paths:
        if os.path.isdir(RAW_DOCS_DIR):
            files = sorted(f for f in os.listdir(RAW_DOCS_DIR) if f.endswith(".zip"))
            zip_paths = [os.path.join(RAW_DOCS_DIR, f) for f in files[:3]]

    if not zip_paths:
        print(f"\nZIP 파일 없음.")
        print(f"  • {RAW_DOCS_DIR}/ 에 ZIP을 넣거나")
        print(f"  • python pilot_chunking.py <path>.zip 으로 직접 지정하세요.")
        return

    print(f"\n파일럿 대상 {len(zip_paths)}건: {[os.path.basename(p) for p in zip_paths]}")

    for path in zip_paths:
        try:
            report_one(path, dart)
        except Exception as e:
            import traceback
            print(f"\n  오류 ({path}): {e}")
            traceback.print_exc()

    print(f"\n{'='*65}")
    print("파일럿 완료.")


if __name__ == "__main__":
    main()
