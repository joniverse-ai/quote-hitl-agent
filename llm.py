"""LLM 이 맡는 세 가지 일: 요구사항 추출 · 등급 투표 · 견적 문안 작성.

OPENAI_API_KEY 가 없으면 같은 모양의 결과를 규칙으로 만든다(mock).
계산(단가 선택·합산)은 여기서 하지 않는다. pipeline.py 의 코드가 한다.
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

import config
import kg

CATEGORY_IDS = tuple(c["id"] for c in kg.categories())
Grade = Literal["신규S", "신규A", "파생B", "파생C", "수정A", "수정B"]
OPTIONS = ("카피", "스톡", "CG", "구조개발", "애니메이션", "교정", "콘티", "성우", "자막", "BGM")


class Deliverable(BaseModel):
    category: Literal[CATEGORY_IDS] = Field(description="단가표 카테고리 id")
    description: str = Field(description="이 산출물이 무엇인지 한 줄")
    quantity: int = Field(1, description="같은 산출물의 개수(종·편·건)")
    pages: Optional[int] = Field(None, description="페이지·장·컷 수. 없으면 null")
    duration_sec: Optional[int] = Field(None, description="영상 길이(초). 영상이 아니면 null")
    format: Optional[str] = Field(None, description="판형·형태 (A4, 3단 접지, 현수막 등)")
    options: list[str] = Field(default_factory=list, description=f"요청에 명시된 추가 작업. 다음 중에서: {OPTIONS}")
    grade_signal: str = Field(description="신규/파생/수정 판단에 쓸 수 있는 요청 원문 구절")


class Extraction(BaseModel):
    is_quote_request: bool = Field(description="디자인 작업 견적을 요청하는 메일인가")
    client: str
    summary: str = Field(description="요청 요약 한 문장")
    deliverables: list[Deliverable]
    deadline: Optional[str] = None
    special_terms: list[str] = Field(default_factory=list, description="할인·급행·무제한 수정 등 표준 조건 밖의 요구 원문")
    out_of_scope: list[str] = Field(default_factory=list, description=(
        "디자인 단가표로 자동 산출할 수 없는 작업 요청 원문: 사진·제품 촬영, 인쇄·출력·제작 수량, 목업·샘플 제작, "
        "인쇄 감리, 디자인 심의, 리터칭·누끼, 일러스트, 서체, 외국어 번역/버전, 배송 등"))


class Vote(BaseModel):
    grade: Grade
    reason: str = Field(description="판정 기준 문장 중 어느 것에 해당하는지")


class QuoteText(BaseModel):
    greeting: str
    scope_summary: str
    included: list[str]
    excluded: list[str]
    schedule_note: str


def _chat(temperature=0.0):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=config.MODEL, temperature=temperature)


# ── ① 요구사항 추출 ─────────────────────────────────────
# 단가표 카테고리 이름만으로는 품목을 헷갈린다(예: 현수막 → 광고로 분류). 대표 품목을 함께 준다.
CATEGORY_HINTS = {
    "ad_a": "포스터, 키비주얼(KV), 광고 이미지",
    "ad_b": "잡지·신문 지면광고, 온라인 광고 배너(매체 집행용)",
    "edit_a": "카탈로그, 브로슈어, 회사소개서, 연차보고서 등 여러 페이지 책자",
    "edit_b": "리플렛, 전단, 접지 DM, 세일즈가이드, 매뉴얼",
    "posm": "매장 POP, 현수막, 와이드 배너, 태그, 스탠드·평면 구조물, 입체 구조물, VMD",
    "package": "제품 패키지(박스·용기) 그래픽, 패키지 구조 개발",
    "web_pdp": "쇼핑몰 상세페이지, 랜딩페이지",
    "cardnews": "SNS 카드뉴스(여러 장 이어지는 이미지)",
    "graphic_source": "타이포그래픽·그래픽 소스 개발",
    "video": "영상 편집, 모션 영상(길이·편수)",
    "web_banner": "웹·SNS용 이미지와 배너(자사 채널용), 움직이는 배너",
}
CATEGORY_GUIDE = "\n".join(f"- {c['id']}: {c['name']} — {CATEGORY_HINTS.get(c['id'], '')}" for c in kg.categories())

EXTRACT_RULES = """디자인 스튜디오로 온 메일에서 견적 요구사항을 뽑는다. 없는 값은 지어내지 말고 null 또는 빈 목록.
- is_quote_request: 디자인 작업을 맡기려 하거나 비용을 묻는 메일이면 true ('견적'이라는 단어가 없어도). 파일 요청·세금계산서 문의처럼 작업 의뢰가 아니면 false.
- deliverables: 산출물마다 하나씩. category 는 아래 목록에서 품목 설명이 가장 맞는 것.
- grade_signal: 신규/파생/수정 판단 근거가 되는 구절을 메일 원문에서 그대로 옮겨 적는다 (예: '신규로', '기존 디자인에서 사이즈만', '문구만 변경'). 근거가 없으면 '언급 없음'.
- pages: 한 산출물 안의 페이지·장·컷 수 (카드뉴스 8장, 상세페이지 5페이지, 브로슈어 32페이지 → pages, quantity 는 1).
- quantity: 서로 다른 디자인의 개수(종·편·SKU). 인쇄 부수·제작 수량(예: 몇 부, 몇 개 제작)은 quantity 가 아니다 — 그런 요청은 out_of_scope 에 적는다.
- options: 메일에 명시된 추가 작업을 목록 이름 그대로(카피·스톡·CG·구조개발·애니메이션·교정·콘티·성우·자막·BGM). 요청 전체에 걸린 추가 작업(예: '카피도 같이')은 관련 산출물마다 넣는다.
- special_terms: 표준 조건 밖의 요구 원문. 가격을 낮춰 달라는 요구(할인·네고 등 돌려 말한 표현 포함), 일정 단축, 무제한 수정, 무료 요구.
- out_of_scope: 디자인 단가표로 계산할 수 없는 실행 작업 요청 원문. 촬영, 인쇄·출력·양산 등 제작, 목업·샘플 제작, 인쇄·제작 감리, 디자인 심의, 이미지 보정·리터칭, 외국어 번역·버전, 일러스트, 서체, 배송 — 다른 말로 표현한 경우도 포함."""


def extract(request):
    if config.USE_LLM:
        prompt = (
            f"{EXTRACT_RULES}\n\n카테고리 목록:\n{CATEGORY_GUIDE}\n\n"
            f"보낸 곳: {request.get('client', '')}\n메일:\n{request['text']}"
        )
        ex = _chat().with_structured_output(Extraction).invoke(prompt).model_dump()
        for d in ex["deliverables"]:          # '카피라이팅' → '카피' 처럼 단가 계산이 쓰는 이름으로 맞춘다
            d["options"] = list(dict.fromkeys(o for raw in d["options"] for o in OPTIONS if o in raw or raw in o))
        # 규칙 안전망: LLM 은 돌려 말한 표현을, 키워드는 확실한 표현을 잡는다. 둘을 합친다.
        # (예: '급하게 필요합니다'를 LLM 은 희망 일정으로만 읽었다)
        text = request["text"]
        ex["special_terms"] += [w for w in config.SPECIAL_TERMS if w in text and not any(w in s for s in ex["special_terms"])]
        ex["out_of_scope"] += [w for w in config.OUT_OF_SCOPE_TERMS if w in text and not any(w in s for s in ex["out_of_scope"])]
        return ex
    return _mock_extract(request)


KEYWORDS = [
    ("package", ["패키지", "박스", "용기"]),
    ("web_pdp", ["상세페이지", "랜딩", "PDP"]),
    ("cardnews", ["카드뉴스"]),
    ("edit_b", ["리플렛", "리플릿", "전단", "DM", "매뉴얼", "세일즈가이드"]),
    ("edit_a", ["카탈로그", "브로슈어", "브로셔", "사보"]),
    ("ad_b", ["잡지", "지면", "신문"]),
    ("ad_a", ["포스터", "키비주얼", "KV"]),
    ("web_banner", ["배너", "웹 이미지", "SNS 이미지"]),
    ("video", ["영상", "모션", "유튜브"]),
    ("posm", ["POP", "POSM", "현수막", "태그", "스탠드", "VMD"]),
    ("graphic_source", ["타이포", "그래픽 소스"]),
]
OPTION_WORDS = {"카피": ["카피", "문구 작성", "헤드라인"], "스톡": ["스톡", "이미지 구매"], "CG": ["합성", "CG", "리터칭"],
                "구조개발": ["구조"], "애니메이션": ["움직이는", "애니메이션", "GIF"], "교정": ["교정", "교열", "원고"],
                "콘티": ["콘티"], "성우": ["성우", "내레이션", "나레이션"], "자막": ["자막"], "BGM": ["BGM", "음악"]}


def _mock_extract(request):
    text = request["text"]
    special = [w for w in config.SPECIAL_TERMS if w in text]
    # 문장 단위로 쪼개고, 카테고리가 안 나오는 문장은 앞에서 언급된 카테고리에 붙인다
    parts = [p.strip() for p in re.split(r"[.?!\n]|그리고|또한", text) if p.strip()]
    found, current = {}, None
    for part in parts:
        hit = next((cid for cid, words in KEYWORDS if any(w in part for w in words)), None)
        current = hit or current
        if current:
            found[current] = f"{found.get(current, '')} {part}".strip()
    deliverables = []
    for cid, part in found.items():
        pages = re.search(r"(\d+)\s*(p|P|페이지|장|컷|쪽)", part)
        qty = re.search(r"(\d+)\s*(종|편|개|건|SKU)", part)
        dur = re.search(r"(\d+)\s*(초|분)", part)
        duration = int(dur.group(1)) * (60 if dur and dur.group(2) == "분" else 1) if dur else None
        deliverables.append({
            "category": cid, "description": part[:60],
            "quantity": int(qty.group(1)) if qty else 1,
            "pages": int(pages.group(1)) if pages else None,
            "duration_sec": duration,
            "format": next((f for f in ["A4", "B5", "신문5단", "신문4단", "3단 접지", "2단 접지", "양면", "현수막", "태그", "스탠드", "입체"] if f in part), None),
            "options": [o for o, ws in OPTION_WORDS.items() if any(w in part for w in ws)],
            "grade_signal": part[:80],
        })
    return {
        "is_quote_request": bool(deliverables) and any(w in text for w in ["견적", "비용", "금액", "얼마", "의뢰", "제작", "만들", "진행", "개발"]),
        "client": request.get("client", "미상"),
        "summary": next((s for s in re.split(r"(?<=[.?!])\s", text.strip()) if "안녕하세요" not in s), text)[:70],
        "deliverables": deliverables,
        "deadline": (re.search(r"(\d+월\s*\d+일|\d+주|다음 주|이번 달)", text) or [None])[0],
        "special_terms": special,
        "out_of_scope": [t for t in config.OUT_OF_SCOPE_TERMS if t in text],
    }


# ── ③ 등급 투표 (병렬화: voting) ─────────────────────────
GRADE_RULES = """등급은 세 무리로 나뉜다.
- 신규(S·A): 새 컨셉·새 디자인을 만든다. S 는 조사·분석과 시안 3종 이상, A 는 클라이언트 브리프 기준 신규 개발.
- 파생(B·C): 기존 디자인을 바탕으로 요소·레이아웃을 바꾸거나 시리즈로 확장한다(50% 이상 변경). C 는 사이즈 변경처럼 변경 폭이 작은 쪽.
- 수정(A·B): 기존 디자인에서 문구만(A) 또는 컬러만(B) 바꾼다.
메일에 기존 디자인을 활용한다는 말이 없으면 신규로 본다."""


def vote_grades(deliverable, criteria_text, n=config.VOTES, request_text=""):
    """같은 질문을 n 번 던져 등급을 고른다. 갈리면 확신이 낮다는 뜻."""
    if config.USE_LLM:
        prompt = (
            f"디자인 견적 등급을 하나 골라라.\n{GRADE_RULES}\n\n"
            f"단가표의 등급별 판정 기준:\n{criteria_text}\n\n"
            f"메일 원문: {request_text}\n"
            f"이번 산출물: {deliverable['description']}\n근거 구절: {deliverable['grade_signal']}"
        )
        chain = _chat(temperature=0.8).with_structured_output(Vote)
        return [v.model_dump() for v in chain.batch([prompt] * n)]   # batch = 동시 호출
    return _mock_votes(deliverable, n)


def _mock_votes(d, n):
    s = d["grade_signal"] + " " + d["description"]
    new = any(w in s for w in ["신규", "새로", "처음", "런칭", "론칭", "리뉴얼", "새 브랜드"])
    derive = any(w in s for w in ["기존", "시리즈", "파생", "사이즈만", "사이즈 변경", "확장", "추가 SKU", "맛 추가"])
    minor = any(w in s for w in ["문구만", "컬러만", "색만", "오타", "텍스트만", "날짜만"])
    if minor:
        g = "수정B" if "컬러" in s or "색만" in s else "수정A"
        votes = [g] * n
    elif new and derive:              # 신호가 섞이면 투표가 갈린다
        votes = ["신규A", "파생B", "신규A"][:n]
    elif derive:
        votes = ["파생C" if "사이즈" in s else "파생B"] * n
    else:
        votes = ["신규S" if any(w in s for w in ["브랜딩 전략", "시장 조사", "3종 이상"]) else "신규A"] * n
    return [{"grade": g, "reason": "(mock) 요청 구절의 신규/파생/수정 신호"} for g in votes]


# ── ⑤ 견적 문안 (평가자-최적화자의 최적화자 쪽) ─────────────
BASE_EXCLUDED = ["인쇄·제작 실비", "촬영", "폰트·이미지 라이선스 연장(1년 초과)"]


def _must_exclude(extraction, quote):
    """불포함 범위에 반드시 들어가야 할 항목: 기본 항목 + 별도 협의 + 범위 밖 작업."""
    neg = [f"{l['label'].split(' / ')[-1]} (별도 협의)" for l in quote["lines"] if l["negotiable"]]
    scope = [f"요청하신 '{t}' 작업 (별도 견적)" for t in extraction.get("out_of_scope", [])]
    return BASE_EXCLUDED + neg + scope


def compose(extraction, quote, feedback=None):
    if config.USE_LLM:
        lines = "\n".join(f"- {l['label']} ({l['grade']}) × {l['qty']} = {l['amount']:,}원" for l in quote["lines"])
        must = _must_exclude(extraction, quote)
        prompt = (
            f"디자인 스튜디오 {config.COMPANY['name']}의 AE(클라이언트 담당)로서 견적서 본문을 작성하라.\n"
            "반드시 지킬 것 (QC 가 이 항목을 검사한다):\n"
            f"1. scope_summary 에 '공급가 합계 {quote['subtotal']:,}원(VAT 별도)' 문구를 그대로 넣는다.\n"
            "2. 금액은 아래 숫자만 쓰고, 새로 계산하거나 다른 금액을 쓰지 않는다.\n"
            "3. included 에는 작업별로 무엇을 하는지 한 줄씩 적는다 (비우지 않는다).\n"
            f"4. excluded 에는 다음 항목을 문구 그대로 모두 넣는다: {must}\n"
            f"5. schedule_note 에는 희망 일정({extraction.get('deadline') or '협의'})과 견적 유효기간 {config.QUOTE_VALID_DAYS}일을 적는다.\n\n"
            f"고객: {extraction['client']}\n요청 요약: {extraction['summary']}\n항목:\n{lines}\n"
            + (f"\n지난 초안에 대한 QC 의견 (반드시 고칠 것): {feedback}" if feedback else "")
        )
        return _chat(0.3).with_structured_output(QuoteText).invoke(prompt).model_dump()
    return _mock_compose(extraction, quote)


def _mock_compose(ex, q):
    cats = list(dict.fromkeys(l["category_name"].split("(")[0].strip() for l in q["lines"]))
    neg = [l["label"] for l in q["lines"] if l["negotiable"]]
    included = []
    for tid in dict.fromkeys(l["task_id"] for l in q["lines"]):
        ls = [l for l in q["lines"] if l["task_id"] == tid and not l["negotiable"]]
        if not ls:
            continue
        grade = next((l["grade"] for l in ls if l["grade"] != "공통"), ls[0]["grade"])
        kinds = list(dict.fromkeys(l["label"].split(" / ")[-1] for l in ls))
        included.append(f"{ls[0]['category_name'].split('(')[0].strip()} ({grade}): {', '.join(kinds)}")
    return {
        "greeting": f"{ex['client']} 담당자님, 안녕하세요. {config.COMPANY['name']}입니다. 문의 주신 건의 견적을 보내드립니다.",
        "scope_summary": f"{', '.join(cats)} 작업, 공급가 합계 {q['subtotal']:,}원(VAT 별도)입니다.",
        "included": included,
        "excluded": _must_exclude(ex, q),
        "schedule_note": f"희망 일정: {ex.get('deadline') or '협의'} / 견적 유효기간 {config.QUOTE_VALID_DAYS}일",
    }
