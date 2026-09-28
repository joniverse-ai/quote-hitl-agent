"""시연 시나리오 실행 + 화면 캡처 → docs/screenshots/*.png, docs/run_result.md

운영 데이터와 섞이지 않도록 별도 데이터 폴더로 띄운 서버에 붙는다.
  QUOTE_DATA_DIR=/tmp/demo_data QUOTE_HOLD_SECONDS=8 uvicorn server:app --port 8766
  python scripts/demo_capture.py http://localhost:8766

멈춘 건에 대한 실장님 결정은 아래 PLAN 에 시연 운영자가 미리 적어 둔 것이다.
LLM 판단은 실행마다 조금씩 달라질 수 있어서, PLAN 에 없는 상태가 나오면 기본 처리(결재 대기 → 승인, 유예 → 자동 발송)를 한다.
"""
import json
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8766"
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
api = httpx.Client(base_url=BASE, timeout=60)

# 요청 id → 멈췄을 때 실장님이 할 일 (순서대로)
PLAN = {
    "Q02": {"holding": [{"action": "escalate", "reason": "스톡 이미지 수량 재확인 필요"}], "waiting": [{"action": "approve"}]},
    "Q03": {"waiting": [{"action": "approve"}]},
    "Q04": {"waiting": [{"action": "reject", "reason": "카피 단가 협의 후 재견적"}]},
    "Q06": {"waiting": [{"action": "redo", "instruction": "기존 브랜드 가이드를 활용하므로 파생B로 다시 계산"}, {"action": "approve"}]},
    "Q07": {"holding": [{"action": "send_now"}]},
}
ACTION_TEXT = {"approve": "승인", "edit": "수정 후 승인", "redo": "다시 판정", "reject": "반려",
               "send_now": "유예 중 즉시 발송", "escalate": "유예 중 보류 → 결재함", "cancel": "유예 중 취소", "auto_send": "유예 만료 자동 발송"}
log = {}   # thread_id → 사람이 한 일


def status(tid):
    return api.get(f"/api/threads/{tid}").json()["status"]


def wait_settled(tid, timeout=180):
    """처리 중이 끝날 때까지 (결재 대기·유예·완료)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status(tid)
        if s != "running":
            return s
        time.sleep(1)
    raise TimeoutError(tid)


def drive(tid, req_id):
    """PLAN 대로 결정을 넣으며 끝날 때까지 진행."""
    steps = {k: list(v) for k, v in PLAN.get(req_id, {}).items()}
    while True:
        s = wait_settled(tid)
        if s == "waiting":
            d = (steps.get("waiting") or [{"action": "approve"}]).pop(0) if steps.get("waiting") else {"action": "approve"}
        elif s == "holding":
            if not steps.get("holding"):
                log.setdefault(tid, []).append({"action": "auto_send"})
                while status(tid) == "holding":      # 타이머가 보낼 때까지 기다린다
                    time.sleep(1)
                continue
            d = steps["holding"].pop(0)
        else:
            return s
        log.setdefault(tid, []).append(d)
        r = api.post(f"/api/resume/{tid}", json=d)
        r.raise_for_status()
        time.sleep(1)


def shot(page, name):
    page.screenshot(path=str(SHOTS / name))
    print("  📸", name)


def main():
    api.post("/api/reset", json={})
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="light")
        page.goto(BASE)
        page.wait_for_selector("#samples li", state="attached")   # 예시 메일은 접혀 있다
        page.click('#speed input[value="4"]')

        # 01 메일 가져오기 직후 전체 화면
        page.click("#mailFetch")
        page.wait_for_selector("#mails li[data-mail]")
        page.wait_for_timeout(800)
        shot(page, "01_overview.png")

        # 02 여러 작업이 섞인 요청: 오케스트레이터 → 디자이너 병렬 → 자료실(GraphRAG)
        page.evaluate("document.querySelector('#samplesBox').open = true")
        page.click('#samples li[data-id="Q09"]')
        page.wait_for_selector("#actors .chips .chip", timeout=120_000)
        page.wait_for_selector("#kg line", timeout=60_000)
        page.wait_for_timeout(1500)
        shot(page, "02_workers_graphrag.png")

        # 03 결재 화면 → 04 나갈 견적서 미리보기 → 수정 후 결재
        page.wait_for_selector("#modal:not([hidden])", timeout=180_000)
        page.wait_for_timeout(600)
        shot(page, "03_approval.png")
        page.click('[data-act="preview"]')
        page.frame_locator("#qFrame").locator(".page").wait_for()
        page.wait_for_timeout(800)
        shot(page, "04_quote_preview.png")
        page.click("#qClose")
        page.click('[data-act="edit"]')
        first = page.locator("#mBody tbody tr").first
        price = int(first.locator("input.u").input_value())
        first.locator("input.u").fill(str(int(price * 0.9)))
        q09 = page.evaluate("current.tid")
        log.setdefault(q09, []).append({"action": "edit", "reason": f"첫 항목 단가 {price:,} → {int(price * 0.9):,}원 (10% 조정)"})
        page.click('[data-act="edit-send"]')
        page.wait_for_selector(".sys.ok", timeout=120_000)
        page.wait_for_timeout(1200)

        # 05 기준 미해당 → 발송 유예 카운트다운
        page.click('#samples li[data-id="Q01"]')
        page.wait_for_selector("#holdbox", timeout=180_000)
        page.wait_for_timeout(1500)
        shot(page, "05_hold_countdown.png")
        q01 = page.evaluate("stageTid")
        log.setdefault(q01, []).append({"action": "auto_send"})

        # 06 메일 수신 설정
        page.click("#mailSet")
        page.wait_for_selector("#msForm")
        page.wait_for_timeout(500)
        shot(page, "06_mail_settings.png")
        page.click("#mClose")

        # 나머지 예시 + 메일 3통은 API 로 진행 (실장님 결정은 PLAN)
        samples = api.get("/api/samples").json()
        for s in samples:
            if s["id"] in ("Q01", "Q09"):
                continue
            tid = api.post("/api/run", json={"request_id": s["id"]}).json()["thread_id"]
            print(f"  ▶ {s['id']} {s['client']} → {drive(tid, s['id'])}")
        for m in api.get("/api/mail").json()["mails"]:
            tid = api.post("/api/run", json={"mail_id": m["mail_id"]}).json()["thread_id"]
            print(f"  ▶ {m['mail_id']} {m['from_name']} → {drive(tid, m['mail_id'])}")
        while any(t["status"] in ("running", "holding") for t in api.get("/api/threads").json()["threads"]):
            time.sleep(1)

        # 07 처리가 끝난 뒤 전체 화면 · 08 다크 모드
        page.reload()
        page.wait_for_selector("#done li[data-tid]")
        page.wait_for_timeout(800)
        shot(page, "07_after_run.png")
        dark = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="dark")
        dark.goto(BASE)
        dark.wait_for_selector("#done li[data-tid]")
        dark.wait_for_timeout(800)
        dark.screenshot(path=str(SHOTS / "08_dark.png"))
        print("  📸 08_dark.png")
        browser.close()

    write_report()


def write_report():
    threads = api.get("/api/threads").json()["threads"]
    threads.sort(key=lambda t: t["created"])
    rows, n_stop, n_hold, n_hand = [], 0, 0, 0
    for t in threads:
        tid, req = t["thread_id"], t["request"]
        v = api.get(f"/api/threads/{tid}").json()["values"]
        acts = log.get(tid, [])
        stopped = [r["label"] for r in v.get("stop_reasons") or []]
        first_stop = any(a["action"] in ("approve", "edit", "redo", "reject") for a in acts) or (stopped and not any(a["action"] == "escalate" for a in acts))
        if t["status"] == "handoff":
            n_hand += 1
            gate = "일반 문의 → 이관"
        elif first_stop and not any(a["action"] == "escalate" for a in acts):
            n_stop += 1
            gate = "⏸ 결재 대기 — " + " · ".join(stopped)
        else:
            n_hold += 1
            gate = "📮 발송 유예" + (" → 실장님 보류" if any(a["action"] == "escalate" for a in acts) else "")
        human = " → ".join(ACTION_TEXT[a["action"]] + (f" ({a.get('reason') or a.get('instruction')})" if a.get("reason") or a.get("instruction") else "") for a in acts) or "-"
        result = {"sent": "✉️ 발송", "rejected": "반려", "handoff": "이관"}.get(t["status"], t["status"])
        by = {"human": "실장님", "auto": "자동"}.get(t.get("approved_by") or "", "")
        grades = ",".join(g["grade"] for g in v.get("grade_votes") or [])
        rows.append(f"| {req['id']} | {req['client']} | {(t['subtotal'] or 0):,} | {grades or '-'} | {gate} | {human} | {result}{' · ' + by if by else ''} |")
    total = len(threads)
    md = "\n".join([
        "# 시연 실행 결과", "",
        f"- 처리 {total}건: 예시 메일 9건 + 메일 수신함 3통 (LLM: gpt-4o-mini, 발송 유예 8초)",
        f"- 결재 대기로 멈춤 **{n_stop}건** · 발송 유예 후 처리 **{n_hold}건** · 일반 문의 이관 **{n_hand}건**",
        "- 멈춘 건의 실장님 결정은 시연 운영자가 미리 정한 것이다 (`scripts/demo_capture.py` 의 PLAN).",
        "",
        "| 건 | 고객 | 공급가 | 등급 | 멈춤 판단 | 사람이 한 일 | 결과 |",
        "|---|---|---|---|---|---|---|",
        *rows, "",
    ])
    (ROOT / "docs" / "run_result.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
