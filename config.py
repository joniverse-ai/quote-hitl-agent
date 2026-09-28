"""모델·임계값·경로. 멈춤 기준 값은 여기서만 바꾼다."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# QUOTE_DATA_DIR 로 데이터 폴더를 바꿀 수 있다 (시연 기록·캡처를 운영 데이터와 분리할 때)
DATA = Path(os.getenv("QUOTE_DATA_DIR", ROOT / "data"))
RATE_CARD = DATA / "rate_card.json"
REQUESTS = DATA / "requests.json"
CHECKPOINT_DB = DATA / "checkpoints.sqlite"
OUTBOX = DATA / "outbox"          # '발송'된 견적서가 쌓이는 곳 (실제 메일 대신)
THREADS = DATA / "threads.json"   # 처리한 요청 목록 (대기함·결과 집계용)

# OPENAI_API_KEY 가 없으면 규칙 기반(mock)으로 돈다. 결과는 매번 같다.
USE_LLM = bool(os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("QUOTE_MODEL", "gpt-4o-mini")

# ── 멈춤 기준 ────────────────────────────────────────
TOTAL_LIMIT = 5_000_000           # VAT 별도 공급가가 이 값을 넘으면 멈춘다
SPECIAL_TERMS = ["할인", "네고", "급하게", "급히", "긴급", "무제한 수정", "무한 수정", "무료로"]
# 단가표로 자동 산출하지 않는 작업. 엑셀에서 따로 정산하는 섹션(촬영·목업/샘플·감리·심의·CG 리터칭·제작/배송)과
# '별도 적용'으로 적힌 항목(외국어 카피·일러스트·서체)에서 가져왔다. 규칙 모드에서만 쓰고, LLM 모드는 문맥으로 찾는다.
OUT_OF_SCOPE_TERMS = ["촬영", "스타일리스트", "목업", "샘플", "인쇄", "출력", "양산", "배송", "감리", "심의",
                      "리터칭", "보정", "일러스트", "캘리그라피", "서체", "번역", "영문", "일문", "중문"]
VOTES = 3                         # 등급 판정 투표 횟수
MAX_COMPOSE_ATTEMPTS = 2          # 평가자-최적화자 재작성 상한
MAX_REDO = 2                      # 담당자 '다시 판정' 상한

# 기준에 걸리지 않은 건도 바로 보내지 않고 이 시간 동안 발송 대기열에 둔다.
# 담당자가 취소·보류하지 않으면 시간이 지나 자동 발송된다. 데모 10초 / 실운영 600초(10분)
HOLD_SECONDS = int(os.getenv("QUOTE_HOLD_SECONDS", "10"))

VAT_RATE = 0.10
QUOTE_VALID_DAYS = 30
COMPANY = {
    "name": "JONIVERSE",
    "address": "",                # 견적서에 찍을 주소 (필요하면 입력)
    "payment": "세금계산서 접수 후 60일 이내 현금 결제, VAT 별도",
}

# ── 사무실 사람들 (화면·대화에 쓰는 이름) ──────────────────
ROLES = {
    "manager": {"title": "AE", "name": "조이", "emoji": "👩‍💼", "desc": "클라이언트 응대·견적 담당"},
    "orchestrator": {"title": "AD", "name": "레오", "emoji": "🧑‍🎨", "desc": "아트디렉터 · 작업 분배"},
    "evaluator": {"title": "QC", "name": "미오", "emoji": "🧐", "desc": "견적 검수"},
    "approver": {"title": "", "name": "실장님", "emoji": "🧑‍💼", "desc": "최종 결재 (사람)"},
}
# 카테고리별 담당 디자이너 (워커)
DESIGNERS = {
    "package": {"title": "패키지 디자이너", "name": "루나", "emoji": "👩‍🎨"},
    "edit_a": {"title": "편집 디자이너", "name": "해리", "emoji": "🧑‍💻"},
    "edit_b": {"title": "편집 디자이너", "name": "해리", "emoji": "🧑‍💻"},
    "web_pdp": {"title": "웹 디자이너", "name": "제이", "emoji": "👨‍💻"},
    "web_banner": {"title": "웹 디자이너", "name": "제이", "emoji": "👨‍💻"},
    "cardnews": {"title": "콘텐츠 디자이너", "name": "소피", "emoji": "👩‍💻"},
    "video": {"title": "모션 디자이너", "name": "모모", "emoji": "🧑‍🎤"},
    "ad_a": {"title": "광고 디자이너", "name": "니코", "emoji": "👨‍🎨"},
    "ad_b": {"title": "광고 디자이너", "name": "니코", "emoji": "👨‍🎨"},
    "posm": {"title": "POP 디자이너", "name": "보리", "emoji": "👷‍♀️"},
    "graphic_source": {"title": "그래픽 디자이너", "name": "테오", "emoji": "🧑‍🎨"},
}


def who(role):
    r = ROLES[role]
    return f"{r['title']} {r['name']}".strip()
