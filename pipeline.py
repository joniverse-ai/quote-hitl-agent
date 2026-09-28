"""견적서 자동 회신 파이프라인 (LangGraph).

  extract ─① 체이닝─▶ route ─② 라우팅─┬─▶ handoff (견적 외 문의)
                                      └─▶ plan ─④ 오케스트레이터─▶ worker ×N (Send, 병렬)
       worker: GraphRAG 조회 → ③ 등급 3회 투표(병렬) → 단가 줄 선택
  aggregate ─▶ compose ⇄ evaluate (⑤ 평가자-최적화자) ─▶ gate
  gate ─┬─ 기준 미해당 ─▶ hold (interrupt, 발송 유예) ─┬ auto_send(타이머) ─▶ send
        │                                              ├ send_now ─────────▶ send
        │                                              ├ escalate ─▶ escalate ─▶ approval
        │                                              └ cancel ───────────▶ drop
        └─ 기준 해당 ──▶ approval (interrupt) ─┬ approve ─▶ send
                                               ├ edit ────▶ apply_edit ─▶ send
                                               ├ redo ────▶ plan
                                               └ reject ──▶ drop

규칙: interrupt 가 있는 hold·approval 노드 안에서는 바깥으로 나가는 일을 하지 않는다.
재개하면 그 노드가 처음부터 다시 실행되기 때문이다. 발송은 send 노드에서만.
"""
import json
import operator
import re
import time
from collections import Counter
from datetime import datetime
from typing import Annotated, Optional, TypedDict

from langgraph.config import get_config, get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

import config
import criteria
import kg
import llm
import pricing
import quote_doc


def reset_or_add(a, b):
    """리스트 합치기. None 이 오면 비운다(다시 판정할 때 이전 결과를 지우려고)."""
    if b is None:
        return []
    return (a or []) + b


class State(TypedDict, total=False):
    request: dict                      # {id, client, text}
    extraction: dict
    route: str
    tasks: list
    lines: Annotated[list, reset_or_add]
    grade_votes: Annotated[list, reset_or_add]
    quote: dict
    draft: dict
    evaluation: dict
    compose_attempts: int
    stop_reasons: list
    hold_until: Optional[float]         # 발송 유예 만료 시각 (epoch 초)
    decision: dict
    redo_count: int
    redo_instruction: Optional[str]
    status: str
    events: Annotated[list, operator.add]   # 처리 기록 (REPORT 실행 결과용)


def emit(**event):
    """프런트 애니메이션으로 가는 이벤트. 스트림 밖에서 부르면 조용히 무시."""
    try:
        get_stream_writer()(event)
    except Exception:
        pass


# ── ① 추출 ───────────────────────────────────────────────
def extract(state):
    req = state["request"]
    emit(type="move", actor="manager", to="intake")
    emit(type="say", actor="manager", text=f"{req.get('client', '')}에서 견적 메일 왔어요! 무엇을 원하시는지 읽어볼게요.")
    ex = llm.extract(req)
    names = ", ".join(d["category"] for d in ex["deliverables"]) or "없음"
    emit(type="say", actor="manager", text=f"요청 {len(ex['deliverables'])}건 찾음: {names}")
    return {"extraction": ex, "redo_count": state.get("redo_count", 0)}


# ── ② 라우팅 ─────────────────────────────────────────────
def route(state):
    ex = state["extraction"]
    emit(type="move", actor="manager", to="router")
    r = "quote" if ex["is_quote_request"] and ex["deliverables"] else "not_quote"
    emit(type="say", actor="manager", text=f"견적 요청 맞아요. {config.who('orchestrator')}님, 작업 나눠 주세요!" if r == "quote" else "견적 요청이 아니에요. 일반 문의함으로 넘길게요.")
    return {"route": r}


def handoff(state):
    emit(type="move", actor="manager", to="archive")
    emit(type="done", status="handoff", text="일반 문의함으로 이관")
    return {"status": "handoff"}


# ── ④ 오케스트레이터 ─────────────────────────────────────
CAT_NAMES = {c["id"]: c["name"] for c in kg.categories()}


def plan(state):
    ex = state["extraction"]
    emit(type="move", actor="manager", to="orchestrator")
    tasks = []
    for i, d in enumerate(ex["deliverables"], 1):
        d = dict(d)
        if state.get("redo_instruction"):
            d["grade_signal"] = f"{d['grade_signal']} / 담당자 지시: {state['redo_instruction']}"
        tasks.append({"task_id": f"T{i}", "category": d["category"],
                      "category_name": CAT_NAMES.get(d["category"], d["category"]), "deliverable": d})
    emit(type="say", actor="orchestrator", text=f"확인했어요. {len(tasks)}건으로 나눠서 디자이너들에게 배정할게요.")
    for t in tasks:
        emit(type="dispatch", **{"from": "orchestrator", "to": t["task_id"]},
             label=t["category_name"].split("(")[0].strip(), task=t["deliverable"]["description"],
             designer=config.DESIGNERS.get(t["category"], {"title": "디자이너", "name": t["task_id"], "emoji": "🧑‍🎨"}))
    # 다시 판정이면 이전 줄·투표를 비운다
    return {"tasks": tasks, "lines": None, "grade_votes": None, "compose_attempts": 0, "evaluation": {}}


def fan_out(state):
    return [Send("worker", {"task": t, "request_text": state["request"]["text"]}) for t in state["tasks"]]


def _forced_grade(text):
    m = re.search(r"(신규S|신규A|파생B|파생C|수정A|수정B)", text or "")
    return m.group(1) if m else None


def worker(payload):
    task = payload["task"]
    tid, d = task["task_id"], task["deliverable"]
    emit(type="say", actor=tid, text=f"{task['category_name'].split('(')[0].strip()} 제가 볼게요. 먼저 등급부터 판단해 볼게요.")

    # ③ 병렬화(투표): 같은 질문 3번 → 다수결, 갈리면 멈춤 기준에 걸린다
    criteria_text = kg.grade_criteria_text(task["category"])
    forced = _forced_grade(d["grade_signal"])
    if forced:
        votes = [forced] * config.VOTES
    else:
        votes = [v["grade"] for v in llm.vote_grades(d, criteria_text, request_text=payload.get("request_text", ""))]
    grade, n = Counter(votes).most_common(1)[0]
    # 막으려는 위험은 '신규/파생/수정' 무리를 헷갈리는 것. 같은 무리 안의 S/A 차이는 불일치로 보지 않는다.
    agreed = len({v[:2] for v in votes}) == 1
    emit(type="vote", actor=tid, votes=votes, grade=grade, agreed=agreed)

    # GraphRAG: 카테고리 → 항목 → 등급 → 판정 기준 → 규칙
    r = kg.retrieve(task["category"], grade)
    lines = pricing.select_lines(task, grade, r)
    # 화면에는 실제로 견적에 들어간 항목의 경로만 그린다 (후보 전체는 너무 많다)
    path = [f"cat:{task['category']}"]
    for l in lines:
        path += [l["node"], f"grade:{grade}", *l["evidence_nodes"]]
    path += [x["id"] for x in r["rules"]]
    emit(type="kg", actor=tid, path=list(dict.fromkeys(path)), candidates=sum(1 for x in r["items"] if x["price"]))
    amount = sum(l["amount"] for l in lines)
    emit(type="report", **{"from": tid, "to": "orchestrator"}, label=f"{len(lines)}줄 · {amount:,}원", grade=grade)
    return {"lines": lines, "grade_votes": [{"task_id": tid, "votes": votes, "grade": grade, "agreed": agreed}]}


def aggregate(state):
    emit(type="move", actor="manager", to="aggregate")
    q = pricing.aggregate(state["lines"])
    emit(type="say", actor="orchestrator", text=f"다들 고마워요. 합치면 공급가 {q['subtotal']:,}원(VAT 별도)이에요. {config.who('manager')}님 문안 부탁해요.")
    return {"quote": q}


# ── ⑤ 평가자-최적화자 ────────────────────────────────────
def compose(state):
    n = state.get("compose_attempts", 0) + 1
    fb = "; ".join(state.get("evaluation", {}).get("issues", [])) or None
    emit(type="say", actor="manager", text="견적서 문안 쓰고 있어요…" if n == 1 else f"QC 의견 반영해서 다시 썼어요 ({n}회차)")
    draft = llm.compose(state["extraction"], state["quote"], fb)
    return {"draft": draft, "compose_attempts": n}


WON = re.compile(r"(\d{1,3}(?:,\d{3})+)\s*원")


def evaluate(state):
    emit(type="move", actor="manager", to="evaluator")
    q, draft = state["quote"], state["draft"]
    issues = []
    if sum(l["amount"] for l in q["lines"]) != q["subtotal"]:
        issues.append("줄 합계와 공급가가 다름")
    for t in state["tasks"]:
        if not pricing.has_core(q["lines"], t["task_id"]):
            issues.append(f"{t['task_id']}({t['category_name'].split('(')[0]}) 핵심 디자인 단가를 찾지 못함")
    allowed = {f"{v:,}" for v in [q["subtotal"], q["vat"], q["total"]] + [l["amount"] for l in q["lines"]] + [l["unit_price"] for l in q["lines"]]}
    text = json.dumps(draft, ensure_ascii=False)
    stray = [m for m in WON.findall(text) if m not in allowed]
    if stray:
        issues.append(f"견적에 없는 금액이 문안에 등장: {stray}")
    if f"{q['subtotal']:,}" not in draft.get("scope_summary", ""):
        issues.append("요약에 공급가 합계가 없음")
    for l in q["lines"]:
        if l["negotiable"] and not any("협의" in x and l["label"].split(" / ")[-1] in x for x in draft.get("excluded", [])):
            issues.append(f"별도 협의 항목 '{l['label']}'이 불포함 범위에 명시되지 않음")
    if not draft.get("included") or not draft.get("excluded"):
        issues.append("포함/불포함 범위가 비어 있음")
    passed = not issues
    emit(type="eval", passed=passed, issues=issues, attempt=state["compose_attempts"])
    emit(type="say", actor="evaluator", text="숫자·범위 다 맞아요. 검수 통과 ✅" if passed else f"잠깐요, {len(issues)}건 걸려요: {issues[0]}")
    return {"evaluation": {"passed": passed, "issues": issues}}


def after_evaluate(state):
    if state["evaluation"]["passed"] or state["compose_attempts"] >= config.MAX_COMPOSE_ATTEMPTS:
        return "gate"
    return "compose"


# ── 멈춤 판정 ────────────────────────────────────────────
def gate(state):
    emit(type="move", actor="manager", to="gate")
    reasons = criteria.check(state)
    hold_until = None
    if reasons:
        emit(type="stop", reasons=reasons)
        emit(type="say", actor="manager", text=f"이 건은 실장님 결재가 필요해요: {reasons[0]['label']}")
    else:
        # 유예 만료 시각은 여기서 정해 상태에 남긴다 (hold 노드는 재개 때 다시 실행되므로)
        hold_until = time.time() + config.HOLD_SECONDS
        emit(type="say", actor="manager", text=f"결재 기준에 걸리는 건 없어요. 우편함에 넣고 {config.HOLD_SECONDS}초 뒤 발송할게요.")
        emit(type="hold", hold_until=hold_until, seconds=config.HOLD_SECONDS)
    return {"stop_reasons": reasons, "hold_until": hold_until}


def after_gate(state):
    return "approval" if state["stop_reasons"] else "hold"


def hold(state):
    """발송 유예. 담당자가 아무것도 안 하면 서버 타이머가 auto_send 로 재개한다."""
    q = state["quote"]
    answer = interrupt({
        "kind": "hold",
        "hold_until": state["hold_until"],
        "request": state["request"],
        "subtotal": q["subtotal"],
        "total": q["total"],
        "if_sent": f"{state['request'].get('client')}에게 공급가 {q['subtotal']:,}원 견적서가 발송됩니다.",
    })
    return {"decision": answer}


def after_hold(state):
    return {"auto_send": "send", "send_now": "send", "escalate": "escalate", "cancel": "drop"}.get(
        state["decision"].get("action"), "drop")


def escalate(state):
    """유예 중 담당자가 보류 → 승인 대기로. 기준이 놓친 사례로 남는다."""
    reason = criteria.manual_hold(state["decision"].get("reason"))
    emit(type="say", actor="manager", text="실장님이 보류하셨어요. 결재함으로 옮길게요.")
    emit(type="stop", reasons=[reason])
    return {"stop_reasons": [reason]}


def approval_payload(state):
    """승인 화면 내용: 요청 원문 · 핵심 정보 · AI 판정과 근거 · 멈춘 이유 · 통과 시 일어나는 일."""
    q = state["quote"]
    return {
        "kind": "approval",
        "request": state["request"],
        "extraction": state["extraction"],
        "quote": q,
        "draft": state["draft"],
        "grade_votes": state["grade_votes"],
        "stop_reasons": state["stop_reasons"],
        "evaluation": state["evaluation"],
        "if_approved": f"{state['request'].get('client')}에게 공급가 {q['subtotal']:,}원(VAT 포함 {q['total']:,}원) 견적서가 메일로 발송됩니다.",
        "redo_left": config.MAX_REDO - state.get("redo_count", 0),
    }


def approval(state):
    # 여기서는 아무것도 바깥으로 내보내지 않는다 (재개 시 이 노드가 다시 실행됨)
    answer = interrupt(approval_payload(state))
    return {"decision": answer}


def after_approval(state):
    action = state["decision"].get("action")
    if action == "redo" and state.get("redo_count", 0) < config.MAX_REDO:
        return "prepare_redo"
    return {"approve": "send", "edit": "apply_edit", "reject": "drop"}.get(action, "drop")


def prepare_redo(state):
    return {"redo_count": state.get("redo_count", 0) + 1, "redo_instruction": state["decision"].get("instruction", "")}


def apply_edit(state):
    """수정 후 승인: 줄별 수량·단가를 담당자 값으로 바꾸고 다시 합산."""
    edits = {e["line_id"]: e for e in state["decision"].get("lines", [])}
    lines = []
    for l in state["quote"]["lines"]:
        e = edits.get(l["line_id"])
        if e and e.get("remove"):
            continue
        if e:
            l = {**l, "unit_price": int(e.get("unit_price", l["unit_price"])), "qty": int(e.get("qty", l["qty"])), "edited": True}
            l["amount"] = l["unit_price"] * l["qty"]
        lines.append(l)
    q = pricing.aggregate(lines)
    emit(type="say", actor="manager", text=f"실장님 수정 반영했어요. 공급가 {q['subtotal']:,}원")
    return {"quote": q}


# ── 바깥으로 나가는 유일한 곳 ─────────────────────────────
def approved_by(state):
    """auto: 유예 만료로 자동 발송 · human: 담당자가 승인/수정/즉시 발송"""
    action = (state.get("decision") or {}).get("action")
    return "auto" if action in (None, "auto_send") else "human"


def send(state):
    """실제 메일 대신 outbox 에 견적서(HTML)와 기록(JSON)을 남긴다."""
    emit(type="move", actor="manager", to="outbox")
    q = state["quote"]
    tid = get_config()["configurable"]["thread_id"]
    doc = quote_doc.build_doc(state, thread_id=tid)
    record = {**doc, "approved_by": approved_by(state), "decision": state.get("decision"),
              "sent_at": datetime.now().isoformat(timespec="seconds")}
    config.OUTBOX.mkdir(parents=True, exist_ok=True)
    (config.OUTBOX / f"{tid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    (config.OUTBOX / f"{tid}.html").write_text(quote_doc.render(doc))
    emit(type="done", status="sent", text=f"견적서 {doc['quote_no']} 발송 완료 · {q['total']:,}원(VAT 포함)", quote=tid)
    return {"status": "sent"}


def drop(state):
    emit(type="move", actor="manager", to="archive")
    d = state.get("decision") or {}
    reason = d.get("reason") or ("발송 유예 중 담당자가 취소" if d.get("action") == "cancel" else "")
    emit(type="done", status="rejected", text=f"반려 · {reason}")
    return {"status": "rejected"}


def build(checkpointer):
    g = StateGraph(State)
    for name, fn in [("extract", extract), ("route", route), ("handoff", handoff), ("plan", plan),
                     ("worker", worker), ("aggregate", aggregate), ("compose", compose), ("evaluate", evaluate),
                     ("gate", gate), ("hold", hold), ("escalate", escalate),
                     ("approval", approval), ("prepare_redo", prepare_redo),
                     ("apply_edit", apply_edit), ("send", send), ("drop", drop)]:
        g.add_node(name, fn)
    g.add_edge(START, "extract")
    g.add_edge("extract", "route")
    g.add_conditional_edges("route", lambda s: "plan" if s["route"] == "quote" else "handoff", ["plan", "handoff"])
    g.add_conditional_edges("plan", fan_out, ["worker"])
    g.add_edge("worker", "aggregate")
    g.add_edge("aggregate", "compose")
    g.add_edge("compose", "evaluate")
    g.add_conditional_edges("evaluate", after_evaluate, ["compose", "gate"])
    g.add_conditional_edges("gate", after_gate, ["approval", "hold"])
    g.add_conditional_edges("hold", after_hold, ["send", "escalate", "drop"])
    g.add_edge("escalate", "approval")
    g.add_conditional_edges("approval", after_approval, ["send", "apply_edit", "prepare_redo", "drop"])
    g.add_edge("prepare_redo", "plan")
    g.add_edge("apply_edit", "send")
    for n in ("send", "drop", "handoff"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    # 샘플 요청 전체를 돌려 본다. 승인 대기 건은 멈춘 채로 표에 표시되고,
    # 발송 유예 건은 타이머를 기다리지 않고 바로 auto_send 로 재개한다.
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    config.DATA.mkdir(exist_ok=True)
    app = build(SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
    reqs = json.loads(config.REQUESTS.read_text())
    print(f"LLM: {'OpenAI ' + config.MODEL if config.USE_LLM else 'mock(규칙 기반)'}\n")
    for r in reqs:
        cfg = {"configurable": {"thread_id": r["id"]}}
        out = app.invoke({"request": r}, cfg)
        if "hold" in app.get_state(cfg).next:
            out = app.invoke(Command(resume={"action": "auto_send"}), cfg)
        snap = app.get_state(cfg)
        status = "⏸ 대기" if snap.next else out.get("status")
        q = out.get("quote") or {}
        why = ", ".join(x["label"] for x in out.get("stop_reasons") or [])
        grades = ",".join(f"{v['grade']}{'' if v['agreed'] else '?'}" for v in out.get("grade_votes") or [])
        print(f"{r['id']:4} {status:8} {q.get('subtotal', 0):>12,}원  [{grades}]  {why}")
