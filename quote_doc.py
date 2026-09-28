"""JONIVERSE 견적서 (HTML, A4 인쇄용).

엑셀 '표준견적서' 시트 구조를 따른다:
  QUOTATION 머리 → 프로젝트·클라이언트·일자·담당자·결제조건
  → 항목 / 내역 / 구분(등급) / 수량 / 단가 / 비고, 카테고리별 Total → Sub Total(VAT 별도)
  → 회사명·주소·서명/날인
여기에 고객이 읽기 쉽도록 합계 박스(한글 금액), 포함/불포함 범위, 등급 설명, 조건을 더했다.
사내 정보(멈춘 이유, 누가 결재했는지)는 고객 문서에 넣지 않는다.
"""
from datetime import date, timedelta
from html import escape

import config

GRADE_TEXT = {
    "신규S": "신규 S", "신규A": "신규 A", "파생B": "파생 B", "파생C": "파생 C",
    "수정A": "수정 A", "수정B": "수정 B", "공통": "공통",
}
GRADE_LEGEND = [
    ("신규 (S·A)", "새 컨셉·새 디자인 개발. S는 조사·분석과 시안 3종 이상, A는 클라이언트 브리프 기준"),
    ("파생 (B·C)", "기존 디자인을 50% 이상 개선·변형 (레이아웃·요소·사이즈 변경, 시리즈 확장)"),
    ("수정 (A·B)", "기존 디자인의 문구 변경(A) 또는 컬러 변경(B)"),
]
CONDITIONS = [
    "모든 금액은 VAT 별도입니다.",
    "인쇄·출력·후가공·배송 등 제작 관련 비용은 실비로 정산합니다.",
    "외주 항목(촬영·샘플 제작 등)은 외주비의 10%를 진행비로 청구합니다.",
    "이미지·영상 라이선스는 1년 사용 기준이며, 다른 기간은 별도 협의합니다.",
    "작업 범위가 바뀌면 견적이 달라질 수 있습니다.",
]


def korean_amount(n):
    """29540000 → '이천구백오십사만'"""
    digits = "영일이삼사오육칠팔구"
    small = ["", "십", "백", "천"]
    big = ["", "만", "억", "조"]
    if n == 0:
        return "영"
    out, i = [], 0
    while n > 0:
        chunk, n = n % 10000, n // 10000
        if chunk:
            s = ""
            for j in range(3, -1, -1):
                d = (chunk // 10 ** j) % 10
                if d:
                    s += ("" if d == 1 and j > 0 else digits[d]) + small[j]
            out.append(s + big[i])
        i += 1
    return "".join(reversed(out))


def build_doc(state, thread_id=None, today=None):
    """상태 → 견적서 데이터. 발송 파일과 미리보기가 같은 함수를 쓴다."""
    today = today or date.today()
    req, ex, q, draft = state["request"], state.get("extraction", {}), state["quote"], state.get("draft", {})
    tasks = {t["task_id"]: t for t in state.get("tasks", [])}
    groups = []
    for tid in dict.fromkeys(l["task_id"] for l in q["lines"]):
        lines = [l for l in q["lines"] if l["task_id"] == tid]
        t = tasks.get(tid, {})
        groups.append({
            "title": (t.get("category_name") or lines[0]["category_name"]),
            "desc": (t.get("deliverable") or {}).get("description", ""),
            "lines": lines,
            "total": sum(l["amount"] for l in lines),
        })
    return {
        "quote_no": f"JONI-{today:%Y%m%d}-{req['id']}",
        "issued": str(today),
        "valid_until": str(today + timedelta(days=config.QUOTE_VALID_DAYS)),
        "client": req.get("client"),
        "contact": req.get("contact"),
        "project": ex.get("summary") or req.get("text", "")[:60],
        "deadline": ex.get("deadline"),
        "manager": config.who("manager"),
        "company": config.COMPANY,
        "groups": groups,
        "subtotal": q["subtotal"], "vat": q["vat"], "total": q["total"],
        "draft": draft,
        "thread_id": thread_id,
    }


def render(doc, draft_mark=False):
    e = escape
    won = lambda n: f"{n:,}"
    rows = []
    for g in doc["groups"]:
        rows.append(f'<tr class="grp"><td colspan="6"><b>{e(g["title"])}</b><span>{e(g["desc"])}</span></td></tr>')
        for l in g["lines"]:
            parts = l["label"].split(" / ")
            item, detail = parts[0], " / ".join(parts[1:])
            note = " · ".join(x for x in [l.get("unit_note"), l.get("assumption")] if x)
            amount = "별도 협의" if l["negotiable"] else won(l["amount"])
            unit = "별도 협의" if l["negotiable"] else won(l["unit_price"])
            rows.append(
                f'<tr><td>{e(item)}</td><td>{e(detail)}</td><td class="c">{e(GRADE_TEXT.get(l["grade"], l["grade"]))}</td>'
                f'<td class="n">{unit}</td><td class="c">{l["qty"]}</td><td class="n">{amount}</td></tr>'
                + (f'<tr class="note"><td></td><td colspan="5">{e(note)}</td></tr>' if note else "")
            )
        rows.append(f'<tr class="sub"><td colspan="5">소계</td><td class="n">{won(g["total"])}</td></tr>')

    d = doc.get("draft") or {}
    inc = "".join(f"<li>{e(x)}</li>" for x in d.get("included", []))
    exc = "".join(f"<li>{e(x)}</li>" for x in d.get("excluded", []))
    legend = "".join(f"<li><b>{e(k)}</b> {e(v)}</li>" for k, v in GRADE_LEGEND)
    cond = "".join(f"<li>{e(x)}</li>" for x in CONDITIONS)
    c = doc["company"]

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>견적서 {e(doc['quote_no'])}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/web/static/pretendard.min.css">
<style>
:root {{ --ink:#1d1b18; --muted:#7a736a; --line:#e3ddd2; --accent:#e8603c; --soft:#faf6ef; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#ebe7e0; color:var(--ink); font:13px/1.55 Pretendard, -apple-system, sans-serif; }}
.page {{ position:relative; width:210mm; min-height:297mm; margin:24px auto; background:#fff; padding:18mm 16mm; box-shadow:0 10px 30px -12px rgba(0,0,0,.3); }}
@media (max-width: 820px) {{ .page {{ width:auto; min-height:0; margin:0; padding:24px 16px; }} }}
@media print {{ body {{ background:#fff; }} .page {{ margin:0; box-shadow:none; }} .noprint {{ display:none; }} }}
.head {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:3px solid var(--ink); padding-bottom:12px; }}
.logo {{ font:700 26px "Space Grotesk", sans-serif; letter-spacing:.1em; }}
.logo i {{ color:var(--accent); font-style:normal; font-size:12px; vertical-align:12px; }}
.logo small {{ display:block; font:500 10px "Space Grotesk", sans-serif; letter-spacing:.3em; color:var(--muted); }}
.title {{ text-align:right; }}
.title h1 {{ margin:0; font:700 28px "Space Grotesk", sans-serif; letter-spacing:.18em; }}
.title div {{ color:var(--muted); font-size:12px; }}
.meta {{ display:grid; grid-template-columns:1fr 1fr; gap:0 24px; margin:16px 0; }}
.meta dl {{ margin:0; display:grid; grid-template-columns:78px 1fr; row-gap:4px; }}
.meta dt {{ color:var(--muted); font-size:12px; }}
.meta dd {{ margin:0; font-weight:600; }}
.sum {{ display:flex; justify-content:space-between; align-items:center; background:var(--soft); border:1px solid var(--line); border-left:5px solid var(--accent); padding:12px 16px; margin-bottom:18px; gap:12px; flex-wrap:wrap; }}
.sum .words {{ font-size:15px; font-weight:700; }}
.sum .words small {{ display:block; font-size:11px; font-weight:500; color:var(--muted); }}
.sum .fig {{ text-align:right; font-variant-numeric:tabular-nums; }}
.sum .fig b {{ font-size:22px; }}
.sum .fig span {{ display:block; font-size:11px; color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th {{ background:var(--ink); color:#fff; font-weight:600; padding:7px 8px; text-align:left; }}
td {{ padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
td.n, th.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.c, th.c {{ text-align:center; white-space:nowrap; }}
tr.grp td {{ background:var(--soft); border-bottom:1px solid var(--line); padding-top:9px; }}
tr.grp span {{ margin-left:8px; color:var(--muted); font-size:11px; }}
tr.note td {{ border-bottom:1px dashed var(--line); color:var(--muted); font-size:11px; padding-top:0; }}
tr.sub td {{ text-align:right; font-weight:700; border-bottom:2px solid var(--line); }}
tfoot td {{ font-weight:700; border:0; padding:5px 8px; text-align:right; }}
tfoot tr.total td {{ font-size:15px; border-top:2px solid var(--ink); }}
.two {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:18px; }}
@media (max-width: 820px) {{ .two, .meta {{ grid-template-columns:1fr; }} }}
h2 {{ font-size:12px; letter-spacing:.08em; margin:0 0 6px; padding-bottom:4px; border-bottom:1px solid var(--ink); }}
ul {{ margin:0; padding-left:18px; }}
li {{ margin:2px 0; }}
.small li {{ font-size:11.5px; color:#48433c; }}
.foot {{ display:flex; justify-content:space-between; align-items:flex-end; margin-top:26px; padding-top:14px; border-top:1px solid var(--line); }}
.foot .co b {{ font:700 15px "Space Grotesk", sans-serif; letter-spacing:.1em; }}
.foot .co div {{ color:var(--muted); font-size:11.5px; }}
.seal {{ width:74px; height:74px; border:2px solid var(--accent); color:var(--accent); border-radius:50%; display:grid; place-items:center; font:700 11px "Space Grotesk", sans-serif; letter-spacing:.08em; transform:rotate(-8deg); opacity:.85; }}
.draftmark {{ position:absolute; top:40%; left:50%; transform:translate(-50%,-50%) rotate(-24deg); font:700 110px "Space Grotesk", sans-serif; color:rgba(232,96,60,.09); letter-spacing:.1em; pointer-events:none; }}
.bar {{ max-width:210mm; margin:16px auto 0; display:flex; gap:8px; justify-content:flex-end; }}
.bar button {{ font:inherit; border:0; background:var(--ink); color:#fff; padding:7px 14px; border-radius:8px; cursor:pointer; }}
</style></head>
<body>
<div class="bar noprint"><button onclick="print()">인쇄 / PDF 저장</button></div>
<div class="page">
  {'<div class="draftmark">DRAFT</div>' if draft_mark else ''}
  <div class="head">
    <div class="logo">{e(c['name'])}<i>●</i><small>DESIGN STUDIO</small></div>
    <div class="title"><h1>QUOTATION</h1><div>견적서 · No. {e(doc['quote_no'])}</div></div>
  </div>
  <div class="meta">
    <dl><dt>수신</dt><dd>{e(doc['client'])}{(' ' + e(doc['contact']) + '님') if doc.get('contact') else ''} 귀하</dd>
        <dt>프로젝트</dt><dd>{e(doc['project'])}</dd>
        <dt>희망 일정</dt><dd>{e(doc.get('deadline') or '협의')}</dd></dl>
    <dl><dt>발행일</dt><dd>{e(doc['issued'])}</dd>
        <dt>유효기간</dt><dd>{e(doc['valid_until'])}까지</dd>
        <dt>담당자</dt><dd>{e(doc['manager'])}</dd>
        <dt>결제조건</dt><dd>{e(c['payment'])}</dd></dl>
  </div>
  <div class="sum">
    <div class="words">일금 {korean_amount(doc['total'])}원정<small>(VAT 포함 · ₩{won(doc['total'])})</small></div>
    <div class="fig"><b>₩{won(doc['subtotal'])}</b><span>공급가액 · VAT 별도</span></div>
  </div>
  <table>
    <thead><tr><th style="width:17%">항목</th><th>내역</th><th class="c" style="width:9%">구분</th><th class="n" style="width:13%">단가</th><th class="c" style="width:7%">수량</th><th class="n" style="width:14%">금액</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
    <tfoot>
      <tr><td colspan="5">공급가액 (Sub Total)</td><td>{won(doc['subtotal'])}</td></tr>
      <tr><td colspan="5">부가세 (VAT 10%)</td><td>{won(doc['vat'])}</td></tr>
      <tr class="total"><td colspan="5">합계</td><td>₩{won(doc['total'])}</td></tr>
    </tfoot>
  </table>
  <div class="two">
    <div><h2>포함 범위</h2><ul>{inc}</ul></div>
    <div><h2>불포함 · 별도 협의</h2><ul>{exc}</ul></div>
  </div>
  <div class="two">
    <div><h2>작업 구분(등급) 안내</h2><ul class="small">{legend}</ul></div>
    <div><h2>견적 조건</h2><ul class="small">{cond}</ul></div>
  </div>
  <div class="foot">
    <div class="co"><b>{e(c['name'])}</b><div>{e(c.get('address') or '')}</div><div>{e(d.get('greeting', ''))}</div></div>
    <div class="seal">{e(c['name'][:4])}<br>SEAL</div>
  </div>
</div>
</body></html>"""
