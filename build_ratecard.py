"""엑셀 견적단가표 → data/rate_card.json

표준견적서 시트의 D~I 열은 등급별 단가다.
  D 신규S · E 신규A · F 파생B · G 파생C · H 수정A · I 수정B
병합 셀은 병합 범위의 모든 등급에 같은 단가를 적용한다(예: CG 시간당 단가).
"""
import json
import re
import unicodedata
from pathlib import Path

import openpyxl

# 엑셀 원본은 회사 정보가 들어 있어 저장소에 올리지 않는다. 프로젝트 폴더의 '*견적단가표*.xlsx' 를 찾는다.
# macOS 는 한글 파일명을 자모 분리형(NFD)으로 저장하므로 비교 전에 NFC 로 맞춘다.
ROOT = Path(__file__).parent
SRC = next((p for p in ROOT.glob("*.xlsx") if "견적단가표" in unicodedata.normalize("NFC", p.name)), None)
OUT = ROOT / "data" / "rate_card.json"

GRADES = ["신규S", "신규A", "파생B", "파생C", "수정A", "수정B"]
GRADE_COLS = "DEFGHI"

# 표준 구조(기획·시안·디자인·편집·카피)를 따르는 카테고리만 먼저 파싱한다.
# 촬영·목업·감리·심의·CG·제작배송은 헤더가 달라서 SPECIAL 로 따로 적는다.
CATEGORY_IDS = {
    "광고 디자인 Level A": "ad_a",
    "광고 디자인 Level B": "ad_b",
    "편집 디자인 Level A": "edit_a",
    "편집 디자인 Level B": "edit_b",
    "POSM/VMD/POP": "posm",
    "패키지 디자인": "package",
    "Web PDP": "web_pdp",
    "Web CARDNEWS": "cardnews",
    "그래픽 소스 개발": "graphic_source",
    "영상 편집": "video",
    "Web 이미지": "web_banner",
}
STOP_TITLES = ("촬영비", "비주얼 목업", "샘플 목업", "기타 서비스", "디자인 심의", "CG/리터칭", "제작/배송")


def clean(v):
    if isinstance(v, float):
        return int(round(v))
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v).strip()
    return v


def merged_value_map(ws):
    """병합 셀 좌표 → 좌상단 값.

    세로 병합(같은 열)은 항목명을 아래 행에 이어 주고,
    가로 병합(같은 행)은 단가를 여러 등급 칸에 펼쳐 준다.
    제목처럼 행·열 모두 넓은 병합은 좌상단 칸 외에는 채우지 않는다.
    """
    m = {}
    for rng in ws.merged_cells.ranges:
        top = ws.cell(rng.min_row, rng.min_col).value
        vertical = rng.min_col == rng.max_col
        horizontal = rng.min_row == rng.max_row
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if vertical or (horizontal and isinstance(top, (int, float))):
                    m[(r, c)] = top
    return m


def cell(ws, merged, r, col_letter):
    c = openpyxl.utils.column_index_from_string(col_letter)
    v = ws.cell(r, c).value
    if v is None:
        v = merged.get((r, c))
    return clean(v)


def raw(ws, r, col_letter):
    return clean(ws[f"{col_letter}{r}"].value)


def parse_standard(ws):
    merged = merged_value_map(ws)
    categories, cat = [], None
    cost_type = item = None
    for r in range(18, ws.max_row + 1):
        a = cell(ws, merged, r, "A")
        b = cell(ws, merged, r, "B")
        c = cell(ws, merged, r, "C")
        note = cell(ws, merged, r, "V")
        prices = {g: cell(ws, merged, r, col) for g, col in zip(GRADES, GRADE_COLS)}
        has_price = any(isinstance(p, int) and p > 0 for p in prices.values())
        text_price = next((p for p in prices.values() if isinstance(p, str) and p), None)

        # 카테고리 제목 행: A 에만 글자가 있고 나머지 칸은 비어 있다
        is_title = raw(ws, r, "A") and not any(raw(ws, r, col) for col in "BCDEFGHIJKLMNOPQRSTU")
        if is_title and a != "Total":
            if any(a.startswith(t) for t in STOP_TITLES):
                break
            cid = next((v for k, v in CATEGORY_IDS.items() if a.startswith(k)), None)
            cat = {"id": cid or f"cat_{len(categories)}", "name": a, "items": []}
            categories.append(cat)
            cost_type = item = None
            continue
        if cat is None or a == "Total":
            continue

        if a:
            cost_type, item = a, None
        if b:
            item = b
        if not (has_price or text_price):
            continue

        # 병합으로 모든 등급에 같은 값이 들어간 경우 → 등급 무관 단가
        vals = [p for p in prices.values() if isinstance(p, int) and p > 0]
        flat = len(set(vals)) == 1 and len(vals) == len(GRADES)
        cat["items"].append({
            "id": f"{cat['id']}.{len(cat['items'])}",
            "cost_type": cost_type,
            "item": item,
            "detail": c,
            "prices": {g: p for g, p in prices.items() if isinstance(p, int) and p > 0},
            "flat": flat,
            "negotiable": bool(text_price and "협의" in text_price) or bool(note and "별도 협의" in str(note) and not has_price),
            "note": note,
            "row": r,
        })
    return categories


def parse_grade_criteria(ws):
    """등급별 기준 시트: 항목별로 등급 설명 문장을 모은다."""
    merged = merged_value_map(ws)
    out, section, item = [], None, None
    for r in range(6, ws.max_row + 1):
        vals = [cell(ws, merged, r, col) for col in "ABCDEFGHIJ"]
        if not any(vals):
            continue
        a, b, c = vals[0], vals[1], vals[2]
        section = a or section
        item = b or item
        texts = [v for v in vals[3:9] if isinstance(v, str) and v]
        if not texts:
            continue
        out.append({"section": section, "item": item, "detail": c,
                    "grades": vals[3:9], "note": vals[9], "row": r})
    return out


# 여러 항목에 걸치는 규칙. 시트 비고란에서 옮겨 적었다(출처 행 표시).
RULES = [
    {"id": "outsourcing_fee", "text": "외주 항목은 진행비(외주 항목비 × 10%)를 가산한다", "rate": 0.10,
     "applies_to": ["촬영", "샘플 제작", "외주"], "source": "표준견적서 촬영비·샘플 목업 / 등급별 기준 외주"},
    {"id": "stock_70", "text": "스톡이미지는 전체 페이지의 70%를 기본 수량으로 한다", "ratio": 0.70,
     "applies_to": ["edit_a", "cardnews"], "source": "표준견적서 편집 A·카드뉴스 스톡이미지 비고"},
    {"id": "stock_50_banner", "text": "웹 배너 스톡이미지는 페이지 수의 50%를 기본 적용한다", "ratio": 0.50,
     "applies_to": ["web_banner"], "source": "표준견적서 Web 이미지/배너 스톡이미지 비고"},
    {"id": "partial_edit", "text": "50% 이하 부분 수정은 단가의 30%, 50% 이상 수정은 100%를 인정한다",
     "applies_to": ["edit_a", "package", "cardnews"], "source": "표준견적서 편집비 비고"},
    {"id": "derivative_min", "text": "파생 등급은 기존 대비 50% 이상 개선일 때만 인정한다(50% 이하는 인정하지 않음)",
     "applies_to": ["edit_a", "cardnews"], "source": "표준견적서 크리에이티브료 비고"},
    {"id": "license_1y", "text": "이미지·영상 라이선스는 1년 계약이 기본이며 다른 기간은 별도 협의",
     "applies_to": ["video", "라이센스"], "source": "등급별 기준 외주 비고"},
    {"id": "actual_cost", "text": "출력·도무송·후가공·배송·소품 등 제작 관련 비용은 실비 정산한다",
     "applies_to": ["샘플 제작", "촬영", "배송"], "source": "샘플 제작 기준 / 촬영비"},
    {"id": "vat", "text": "모든 금액은 VAT 별도", "rate": 0.10, "applies_to": ["*"], "source": "표준견적서 결제조건"},
    {"id": "payment", "text": "세금계산서 접수 후 60일 이내 현금 결제", "applies_to": ["*"], "source": "표준견적서 결제조건"},
    {"id": "negotiable", "text": "단가가 '별도 협의'로 적힌 항목은 자동 산출하지 않는다",
     "applies_to": ["*"], "source": "표준견적서 각 항목 비고"},
]


def main():
    if SRC is None:
        raise SystemExit("프로젝트 폴더에 '*견적단가표*.xlsx' 가 없습니다. data/rate_card.json 은 이미 들어 있으니 그대로 쓰면 됩니다.")
    wb = openpyxl.load_workbook(SRC, data_only=True)
    data = {
        "source": "견적단가표 엑셀 (표준견적서 · 등급별 기준 · 샘플 제작 기준 시트)",
        "grades": GRADES,
        "grade_groups": {"신규": ["신규S", "신규A"], "파생": ["파생B", "파생C"], "수정": ["수정A", "수정B"]},
        "categories": parse_standard(wb["표준견적서"]),
        "grade_criteria": parse_grade_criteria(wb["등급별 기준"]),
        "rules": RULES,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    n_items = sum(len(c["items"]) for c in data["categories"])
    print(f"카테고리 {len(data['categories'])}개 · 항목 {n_items}개 · 등급기준 {len(data['grade_criteria'])}행 · 규칙 {len(RULES)}개 → {OUT}")


if __name__ == "__main__":
    main()
