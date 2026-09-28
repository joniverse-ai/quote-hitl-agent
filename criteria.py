"""멈춤 기준. 모두 계산 가능한 형태이고, 각 기준이 막는 위험을 함께 적는다.

check(state) 가 돌려주는 목록이 비어 있으면 자동 발송, 하나라도 있으면 멈춘다.
"""
import config

CRITERIA = {
    "total_limit": {
        "label": f"공급가 {config.TOTAL_LIMIT:,}원 초과",
        "risk": "큰 금액을 잘못 제시하면 손해가 크거나 수주를 놓친다",
    },
    "negotiable": {
        "label": "'별도 협의' 항목 포함",
        "risk": "단가표에 금액이 없는 항목을 AI가 임의로 매기거나 빠뜨린다",
    },
    "grade_split": {
        "label": "등급 판정 투표 불일치",
        "risk": "신규/파생을 헷갈리면 같은 작업의 금액이 1.5~2배 달라진다",
    },
    "special_terms": {
        "label": "표준 밖 조건 요구 (할인·급행·무제한 수정)",
        "risk": "단가표에 없는 약속을 자동으로 수락하게 된다",
    },
    "out_of_scope": {
        "label": "범위 밖 작업 포함 (촬영·인쇄·목업·감리 등)",
        "risk": "자동 산출하지 않는 작업이 조용히 빠진 채 견적이 나간다 (검증에서 3건 발견)",
    },
    "eval_failed": {
        "label": "검수 미통과",
        "risk": "계산 오류·누락이 있는 견적이 그대로 나간다",
    },
    "human_redo": {
        "label": "담당자 재판정 요청 건",
        "risk": "사람이 한 번 개입한 건은 결과도 사람이 확인해야 한다",
    },
    # 자동 기준이 아니라 발송 유예 중에 담당자가 직접 보류한 경우
    "manual_hold": {
        "label": "발송 유예 중 담당자 보류",
        "risk": "기준이 놓친 건을 사람이 발견했다 (놓침 사례로 기록해 기준 개선에 쓴다)",
    },
}


def manual_hold(detail):
    c = CRITERIA["manual_hold"]
    return {"code": "manual_hold", "label": c["label"], "risk": c["risk"], "detail": detail or "사유 미기재"}


def check(state):
    reasons = []
    quote = state.get("quote") or {}
    ex = state.get("extraction") or {}

    if quote.get("subtotal", 0) > config.TOTAL_LIMIT:
        reasons.append(("total_limit", f"공급가 {quote['subtotal']:,}원"))
    neg = [l["label"] for l in quote.get("lines", []) if l["negotiable"]]
    if neg:
        reasons.append(("negotiable", ", ".join(neg)))
    split = [v for v in state.get("grade_votes", []) if not v["agreed"]]
    if split:
        reasons.append(("grade_split", "; ".join(f"{v['task_id']}: {'/'.join(v['votes'])}" for v in split)))
    if ex.get("special_terms"):
        reasons.append(("special_terms", ", ".join(ex["special_terms"])))
    if ex.get("out_of_scope"):
        reasons.append(("out_of_scope", ", ".join(ex["out_of_scope"])))
    ev = state.get("evaluation") or {}
    if ev and not ev.get("passed"):
        reasons.append(("eval_failed", "; ".join(ev.get("issues", []))))
    if state.get("redo_count", 0) > 0:
        reasons.append(("human_redo", state.get("redo_instruction") or ""))

    return [{"code": c, "label": CRITERIA[c]["label"], "risk": CRITERIA[c]["risk"], "detail": d} for c, d in reasons]
