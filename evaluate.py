"""멈춤 기준 검증: 정답 라벨이 붙은 요청으로 기준 조합마다 개입률·놓침·헛멈춤을 센다.

  python evaluate.py                 # 기준을 만들 때 쓴 세트(31건)
  python evaluate.py holdout         # 기준을 정한 뒤 따로 쓴 검증 세트(10건)
  python evaluate.py holdout --mock  # API 키가 있어도 규칙 모드로
결과는 docs/criteria_eval_{dev|holdout}_{mock|llm}.md

파이프라인은 건마다 한 번만 돌리고(멈춤 판단 직전까지), 거기서 모은 재료로
여러 기준 조합을 다시 채점한다. 같은 데이터로 비교해야 기준끼리 공정하게 비교된다.

  개입률  : 전체 중 사람에게 간 비율
  놓침    : 사람이 봤어야 하는데 자동으로 나간 건 (가장 비싼 실수)
  헛멈춤  : 볼 필요가 없는데 사람에게 간 건 (많으면 내용을 안 보고 승인하게 된다)
"""
import json
import sqlite3
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import config
import pipeline

SETS = {
    "dev": (config.DATA / "eval_set.json", "기준을 만들 때 쓴 세트"),
    "holdout": (config.DATA / "eval_holdout.json", "기준을 정한 뒤 따로 쓴 검증 세트"),
}


def features(item, app):
    """파이프라인을 멈춤 판단 직전까지 돌리고 기준 재료를 모은다."""
    cfg = {"configurable": {"thread_id": f"eval-{item['id']}"}}
    req = {k: item[k] for k in ("id", "client", "text")}
    app.invoke({"request": req}, cfg)
    v = app.get_state(cfg).values
    q = v.get("quote") or {}
    return {
        "id": item["id"],
        "client": item["client"],
        "should_stop": item["should_stop"],
        "why": item["why"],
        "is_quote": item.get("is_quote", True),           # 정답: 견적 요청인가
        "quote_request": v.get("route") == "quote",       # 파이프라인: 견적 요청으로 분류했나
        "subtotal": q.get("subtotal", 0),
        "negotiable": any(l["negotiable"] for l in q.get("lines", [])),
        "grade_split": any(not g["agreed"] for g in v.get("grade_votes") or []),
        "special_terms": bool((v.get("extraction") or {}).get("special_terms")),
        "eval_failed": bool(v.get("evaluation")) and not v["evaluation"].get("passed"),
        "out_of_scope": bool((v.get("extraction") or {}).get("out_of_scope")),
        "grades": ",".join(g["grade"] for g in v.get("grade_votes") or []),
    }


def amount(limit):
    return lambda f: f["subtotal"] > limit


def any_of(*preds):
    return lambda f: any(p(f) for p in preds)


NEG = lambda f: f["negotiable"]
SPLIT = lambda f: f["grade_split"]
TERMS = lambda f: f["special_terms"]
EVALF = lambda f: f["eval_failed"]
SCOPE = lambda f: f["out_of_scope"]

# 위에서 아래로 갈수록 기준이 더해진다. ★ = 현재 파이프라인(criteria.py)과 같은 조합
CRITERIA_SETS = [
    ("기준 없음 (전부 자동)", lambda f: False),
    ("금액 1,000만 원 초과", amount(10_000_000)),
    ("금액 500만 원 초과", amount(config.TOTAL_LIMIT)),
    ("금액 500만 + 별도 협의", any_of(amount(config.TOTAL_LIMIT), NEG)),
    ("금액 500만 + 별도 협의 + 투표 불일치", any_of(amount(config.TOTAL_LIMIT), NEG, SPLIT)),
    ("+ 표준 밖 조건 + 검수 실패 (범위 밖 추가 전)", any_of(amount(config.TOTAL_LIMIT), NEG, SPLIT, TERMS, EVALF)),
    ("★ 현재: + 범위 밖 작업", any_of(amount(config.TOTAL_LIMIT), NEG, SPLIT, TERMS, EVALF, SCOPE)),
    ("현재에서 금액만 1,000만으로 완화", any_of(amount(10_000_000), NEG, SPLIT, TERMS, EVALF, SCOPE)),
    ("전부 멈춤", lambda f: True),
]


def measure(rows, pred):
    """견적 요청만 대상으로 센다 (일반 문의 이관은 바깥으로 나가지 않으므로 제외)."""
    stopped, missed, false_stop = [], [], []
    for f in rows:
        if not f["quote_request"]:
            continue
        did = pred(f)
        if did:
            stopped.append(f["id"])
            if not f["should_stop"]:
                false_stop.append(f["id"])
        elif f["should_stop"]:
            missed.append(f["id"])
    return stopped, missed, false_stop


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--mock" in sys.argv:
        config.USE_LLM = False
    which = args[0] if args else "dev"
    src, desc = SETS[which]
    OUT = config.ROOT / "docs" / f"criteria_eval_{which}_{'llm' if config.USE_LLM else 'mock'}.md"
    data = json.loads(src.read_text())
    items = data["items"]
    app = pipeline.build(SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
    rows = [features(it, app) for it in items]
    quotes = [f for f in rows if f["quote_request"]]
    handoff = [f["id"] for f in rows if not f["quote_request"]]
    need = sum(f["should_stop"] for f in quotes)
    misrouted = [f for f in rows if f["is_quote"] != f["quote_request"]]

    mode = f"OpenAI {config.MODEL}" if config.USE_LLM else "mock(규칙 기반)"
    lines = [
        f"# 멈춤 기준 검증 결과 — {desc}", "",
        f"- 요청 {len(rows)}건 (견적 요청 {len(quotes)}건, 일반 문의 이관 {len(handoff)}건: {', '.join(handoff) or '없음'})",
        f"- 정답: 사람이 봐야 하는 건 {need}건 / 자동으로 나가도 되는 건 {len(quotes) - need}건",
        f"- 추출·등급 판단: {mode}",
        "- 정답 원칙: " + " · ".join(f"{k} {v.split(' — ')[0]}" for k, v in data["label_rule"].items() if k.startswith("R")),
        "- ② 라우팅 오류: " + (", ".join(
            f"{f['id']}({'견적 요청인데 일반 문의로 이관' if f['is_quote'] else '일반 문의인데 견적으로 처리'})" for f in misrouted) or "없음")
        + " — 이관된 건은 바깥으로 나가지 않아 아래 표의 채점에서 빠진다",
        "",
        "| 기준 조합 | 개입률 | 놓침 | 헛멈춤 | 놓친 건 | 헛멈춤 건 |",
        "|---|---|---|---|---|---|",
    ]
    results = []
    for name, pred in CRITERIA_SETS:
        s, m, fs = measure(rows, pred)
        results.append((name, s, m, fs))
        lines.append(f"| {name} | {len(s)}/{len(quotes)} ({len(s) / len(quotes):.0%}) | {len(m)} | {len(fs)} | {', '.join(m) or '-'} | {', '.join(fs) or '-'} |")

    lines += ["", "## 건별 재료", "",
              "| 건 | 고객 | 정답 | 이유 | 공급가 | 등급 | 별도협의 | 투표불일치 | 조건요구 | 범위밖 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    ox = lambda b: "●" if b else ""
    for f in rows:
        if not f["quote_request"]:
            lines.append(f"| {f['id']} | {f['client']} | 이관 | - | - | - | | | | |")
            continue
        lines.append(f"| {f['id']} | {f['client']} | {'멈춤' if f['should_stop'] else '자동'} | {','.join(f['why']) or '-'} | "
                     f"{f['subtotal']:,} | {f['grades']} | {ox(f['negotiable'])} | {ox(f['grade_split'])} | {ox(f['special_terms'])} | {ox(f['out_of_scope'])} |")

    md = "\n".join(lines) + "\n"
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(md)
    print(md)
    print(f"→ {OUT.relative_to(config.ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
