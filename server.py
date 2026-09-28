"""웹 데모 서버. uvicorn server:app --reload

파이프라인을 백그라운드 스레드에서 돌리고, 노드가 내보내는 이벤트를
SSE(/api/events/{id})로 브라우저에 흘려 보낸다. 브라우저는 이 이벤트로 캐릭터를 움직인다.
대기 건은 SQLite 체크포인터에 남으므로 서버를 껐다 켜도 이어서 승인할 수 있다.
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

import config
import criteria
import kg
import mail_inbox
import pipeline
import quote_doc

config.DATA.mkdir(exist_ok=True)
_conn = sqlite3.connect(config.CHECKPOINT_DB, check_same_thread=False)
graph = pipeline.build(SqliteSaver(_conn))

_lock = threading.Lock()
_events: dict[str, list] = {}      # thread_id → 이벤트 기록 (이번 서버 실행 동안)
_running: set[str] = set()


def _load_threads():
    return json.loads(config.THREADS.read_text()) if config.THREADS.exists() else {}


def _save_threads(t):
    config.THREADS.write_text(json.dumps(t, ensure_ascii=False, indent=1))


def _push(tid, ev):
    with _lock:
        _events.setdefault(tid, []).append(ev)


def _start(tid, payload):
    """_running 표시를 먼저 하고 스레드를 띄운다 (타이머와 버튼이 동시에 재개하는 것을 막는다)."""
    with _lock:
        if tid in _running:
            return False
        _running.add(tid)
    threading.Thread(target=_run, args=(tid, payload), daemon=True).start()
    return True


def _run(tid, payload):
    cfg = {"configurable": {"thread_id": tid}}
    try:
        for mode, chunk in graph.stream(payload, cfg, stream_mode=["custom", "updates"]):
            if mode == "custom":
                _push(tid, chunk)
            elif "__interrupt__" in chunk:
                value = chunk["__interrupt__"][0].value
                if value.get("kind") != "hold":      # 유예는 gate 가 보낸 hold 이벤트로 충분
                    _push(tid, {"type": "interrupt", "payload": value})
    except Exception as e:  # 데모 화면에 오류를 보여 주고 서버는 계속 돈다
        _push(tid, {"type": "error", "text": f"{type(e).__name__}: {e}"})
    _running.discard(tid)
    _push(tid, {"type": "end", "status": _status(tid)})


def _status(tid):
    if tid in _running:
        return "running"
    snap = graph.get_state({"configurable": {"thread_id": tid}})
    if snap.next:
        if "approval" in snap.next:
            return "waiting"
        if "hold" in snap.next:
            return "holding"
        return "running"
    return snap.values.get("status", "unknown")


def _hold_timer():
    """유예가 끝난 건을 auto_send 로 재개한다. 서버를 재시작해도 목록을 다시 훑으므로 이어진다."""
    while True:
        now = time.time()
        for tid in list(_load_threads()):
            if tid in _running:
                continue
            snap = graph.get_state({"configurable": {"thread_id": tid}})
            until = (snap.values or {}).get("hold_until")
            if "hold" in (snap.next or ()) and until and until <= now:
                _start(tid, Command(resume={"action": "auto_send"}))
        time.sleep(1)


threading.Thread(target=_hold_timer, daemon=True).start()


app = FastAPI(title="JONIVERSE 견적 데스크")
app.mount("/static", StaticFiles(directory=config.ROOT / "web"), name="static")


@app.get("/")
def index():
    return FileResponse(config.ROOT / "web" / "index.html")


@app.get("/api/meta")
def meta():
    return {"llm": config.MODEL if config.USE_LLM else "mock", "criteria": criteria.CRITERIA,
            "total_limit": config.TOTAL_LIMIT, "hold_seconds": config.HOLD_SECONDS,
            "company": config.COMPANY["name"], "roles": config.ROLES, "designers": config.DESIGNERS}


@app.get("/api/samples")
def samples():
    return json.loads(config.REQUESTS.read_text())


@app.get("/api/graph")
def graph_view():
    return kg.export_for_view()


def _register_and_start(req):
    tid = f"{req['id']}-{uuid.uuid4().hex[:4]}"
    threads = _load_threads()
    threads[tid] = {"request": req, "created": datetime.now().isoformat(timespec="seconds")}
    _save_threads(threads)
    _start(tid, {"request": req})
    return tid


class RunIn(BaseModel):
    request_id: str | None = None
    mail_id: str | None = None
    client: str | None = None
    contact: str | None = None         # 고객 담당자 (선택)
    text: str | None = None


@app.post("/api/run")
def run(body: RunIn):
    if body.mail_id:
        return {"thread_id": _process_mail(body.mail_id)}
    if body.request_id:
        req = next((r for r in samples() if r["id"] == body.request_id), None)
        if not req:
            raise HTTPException(404, "샘플 없음")
        req = {k: v for k, v in req.items() if k != "should_stop"}
    elif body.text and body.text.strip():
        req = {"id": f"C{uuid.uuid4().hex[:4].upper()}", "client": (body.client or "").strip() or "직접 입력",
               "text": body.text.strip()}
        if body.contact and body.contact.strip():
            req["contact"] = body.contact.strip()
    else:
        raise HTTPException(400, "request_id 또는 text 가 필요합니다")
    return {"thread_id": _register_and_start(req)}


# ── 메일 수신함 ─────────────────────────────────────────
def _process_mail(mail_id):
    m = next((x for x in mail_inbox.load_mails() if x["mail_id"] == mail_id), None)
    if not m:
        raise HTTPException(404, "메일이 없습니다")
    if m.get("thread_id") and _status(m["thread_id"]) not in ("unknown",):
        raise HTTPException(409, "이미 견적 처리한 메일입니다")
    tid = _register_and_start(mail_inbox.to_request(m))
    mail_inbox.link(mail_id, tid)
    return tid


def _fetch_and_maybe_process():
    new = mail_inbox.fetch()
    if mail_inbox.load_settings()["auto_process"]:
        for m in new:
            _process_mail(m["mail_id"])
    return new


def _mail_poller():
    """자동 가져오기가 켜져 있으면 poll_minutes 마다 메일을 가져온다."""
    last = 0.0
    while True:
        s = mail_inbox.load_settings()
        if s["auto_fetch"] and time.time() - last >= s["poll_minutes"] * 60:
            last = time.time()
            try:
                _fetch_and_maybe_process()
            except Exception as e:
                print(f"[mail] 자동 가져오기 실패: {e}")
        time.sleep(5)


threading.Thread(target=_mail_poller, daemon=True).start()


@app.get("/api/mail/settings")
def mail_settings():
    return mail_inbox.public_settings()


@app.post("/api/mail/settings")
def mail_settings_save(data: dict):
    data.pop("password", None)            # 비밀번호는 저장하지 않는다 (.env 의 MAIL_PASSWORD 만 사용)
    mail_inbox.save_settings(data)
    return mail_inbox.public_settings()


@app.post("/api/mail/test")
def mail_test():
    return mail_inbox.test_connection()


@app.post("/api/mail/fetch")
def mail_fetch():
    try:
        new = _fetch_and_maybe_process()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"new": len(new)}


@app.get("/api/mail")
def mail_list():
    out = []
    for m in mail_inbox.load_mails():
        status = _status(m["thread_id"]) if m.get("thread_id") else "new"
        out.append({k: m[k] for k in ("mail_id", "from_name", "from_addr", "subject", "received", "source", "thread_id")}
                   | {"preview": m["body"][:80], "status": status})
    s = mail_inbox.load_settings()
    return {"mails": out, "source": s["source"], "username": s["username"], "last_fetch": s.get("last_fetch"),
            "auto_fetch": s["auto_fetch"], "auto_process": s["auto_process"], "poll_minutes": s["poll_minutes"]}


@app.get("/api/events/{tid}")
def events(tid: str, since: int = 0):
    def gen():
        i = since
        idle = 0
        limit = (config.HOLD_SECONDS + 60) * 10
        while idle < limit:          # 유예 시간 + 60초 동안 새 이벤트가 없으면 닫는다
            with _lock:
                batch = _events.get(tid, [])[i:]
            for ev in batch:
                i += 1
                yield f"data: {json.dumps({'i': i, **ev}, ensure_ascii=False)}\n\n"
                if ev["type"] == "end":
                    return
            idle = 0 if batch else idle + 1
            time.sleep(0.1)
    return StreamingResponse(gen(), media_type="text/event-stream")


# 데모 화면의 견적서 뷰어(iframe) 안에서는 자체 인쇄 버튼을 숨기고 여백을 줄인다
EMBED_CSS = "<style>.bar{display:none!important} body{background:transparent!important} .page{margin:12px auto!important}</style>"


@app.get("/quote/{tid}", response_class=HTMLResponse)
def quote_page(tid: str, embed: bool = False):
    """발송된 견적서. 아직 발송 전이면 현재 상태로 만든 미리보기(DRAFT)."""
    sent = config.OUTBOX / f"{tid}.html"
    if sent.exists():
        html = sent.read_text()
    else:
        snap = graph.get_state({"configurable": {"thread_id": tid}})
        if not snap.values or not snap.values.get("quote"):
            raise HTTPException(404, "견적이 아직 없습니다")
        html = quote_doc.render(quote_doc.build_doc(snap.values, thread_id=tid), draft_mark=True)
    return html.replace("</head>", EMBED_CSS + "</head>", 1) if embed else html


@app.get("/api/threads")
def threads():
    return {"now": time.time(), "threads": _thread_rows()}


def _thread_rows():
    out = []
    for tid, t in sorted(_load_threads().items(), key=lambda kv: kv[1]["created"], reverse=True):
        snap = graph.get_state({"configurable": {"thread_id": tid}})
        v = snap.values or {}
        out.append({
            "thread_id": tid, "request": t["request"], "created": t["created"],
            "status": _status(tid),
            "subtotal": (v.get("quote") or {}).get("subtotal"),
            "stop_reasons": [r["label"] for r in v.get("stop_reasons") or []],
            "hold_until": v.get("hold_until"),
            "approved_by": pipeline.approved_by(v) if v.get("status") == "sent" else None,
        })
    return out


@app.get("/api/threads/{tid}")
def thread(tid: str):
    snap = graph.get_state({"configurable": {"thread_id": tid}})
    if not snap.values:
        raise HTTPException(404)
    pending = [i.value for t in snap.tasks for i in t.interrupts]
    return {"status": _status(tid), "values": snap.values, "interrupt": pending[0] if pending else None}


@app.post("/api/reset")
def reset():
    """처리 기록 초기화: 목록·체크포인트(대기 건 포함)·발송된 견적서 파일을 모두 지운다."""
    if _running:
        raise HTTPException(409, "처리 중인 건이 있어요. 끝난 뒤 다시 시도해 주세요")
    with _lock:
        saver = graph.checkpointer
        tids = {r[0] for r in _conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
        for tid in tids:
            saver.delete_thread(tid)
        _events.clear()
        _save_threads({})
        mail_inbox.save_mails([])          # 수신함도 비운다 (다시 '가져오기' 하면 들어온다)
        removed = 0
        if config.OUTBOX.exists():
            for f in config.OUTBOX.iterdir():
                if f.suffix in (".html", ".json"):
                    f.unlink()
                    removed += 1
    return {"ok": True, "threads": len(tids), "files": removed}


class Decision(BaseModel):
    action: str                        # 승인 대기: approve · edit · redo · reject / 유예: send_now · escalate · cancel
    lines: list[dict] = []             # edit: [{line_id, unit_price?, qty?, remove?}]
    instruction: str | None = None     # redo
    reason: str | None = None          # reject · escalate · cancel


ACTIONS = {"waiting": {"approve", "edit", "redo", "reject"}, "holding": {"send_now", "escalate", "cancel"}}


@app.post("/api/resume/{tid}")
def resume(tid: str, d: Decision):
    status = _status(tid)
    if d.action not in ACTIONS.get(status, set()):
        raise HTTPException(409, f"지금 상태({status})에서는 '{d.action}' 을 할 수 없습니다")
    if d.action == "reject" and not d.reason:
        raise HTTPException(400, "반려 사유를 적어 주세요")
    start = len(_events.get(tid, []))
    if not _start(tid, Command(resume=d.model_dump())):
        raise HTTPException(409, "이미 처리 중입니다")
    return {"ok": True, "since": start}
