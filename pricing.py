"""GraphRAG 로 찾은 항목 중 이번 산출물에 들어갈 줄을 고르고 금액을 계산한다.

LLM 을 쓰지 않는다. 같은 입력이면 항상 같은 견적이 나와야 하기 때문이다.
어떤 항목을 넣고 뺐는지, 무엇을 기본값으로 가정했는지는 줄마다 남긴다.
"""
import math
import re

import kg

FORMAT_ALIASES = {
    "A4": "A4", "B5": "B5", "신문5단": "신문5단", "신문4단": "신문4단", "배너": "배너",
    "양면": "양면", "2단 접지": "2단 접지", "3단 접지": "3단 접지",
    "태그": "태그", "현수막": "현수막", "스탠드": "스탠드", "입체": "입체",
    "국전": "국전", "2절": "2절", "4절": "4절",
}
CORE_WORDS = ("크리에이티브", "그래픽 개발", "Editing")


def _upper(detail):
    """'내지 (14p~24p)' → 24, '(48p 초과)' → inf, '(12p 이하)' → 12"""
    if m := re.search(r"(\d+)p\s*초과", detail):
        return math.inf
    if m := re.search(r"~\s*(\d+)p", detail):
        return int(m.group(1))
    if m := re.search(r"(\d+)p\s*이하", detail):
        return int(m.group(1))
    return None


def _pick_by_pages(cands, pages):
    ranged = sorted((c for c in cands if _upper(c["item"]["detail"] or "")), key=lambda c: _upper(c["item"]["detail"]))
    for c in ranged:
        if pages <= _upper(c["item"]["detail"]):
            return c
    return ranged[-1] if ranged else None


def _pick_by_format(cands, fmt):
    if fmt:
        for c in cands:
            if FORMAT_ALIASES.get(fmt, fmt) in (c["item"]["detail"] or ""):
                return c, None
    return cands[0], f"형태가 적혀 있지 않아 '{cands[0]['item']['detail']}' 기준으로 산출"


def _pick_duration(cands, sec):
    buckets = []
    for c in cands:
        if m := re.search(r"(\d+)'\s*(이하|이상)", c["item"]["detail"] or ""):
            buckets.append((int(m.group(1)), m.group(2), c))
    for limit, kind, c in sorted(b for b in buckets if b[1] == "이하"):
        if sec <= limit:
            return c
    return next((c for _, kind, c in buckets if kind == "이상"), None)


def _first_note(note):
    """비고란의 첫 항목만: '- 1P당 단가 - 원고작업 포함 …' → '1P당 단가'"""
    first = re.split(r"\s-\s|\n", f" {note or ''}".replace("*", ""))
    first = [s.strip(" -") for s in first if s.strip(" -")]
    return first[0] if first else ""


def _line(task, x, grade, qty, assumption=None, unit_note=None):
    it = x["item"]
    price = x["price"] or 0
    return {
        "line_id": f"{task['task_id']}.{it['id']}",
        "task_id": task["task_id"],
        "category": task["category"],
        "category_name": task["category_name"],
        "item_id": it["id"],
        "node": x["node"],
        "label": kg.item_label(it),
        "grade": grade if not it["flat"] else "공통",
        "unit_price": price,
        "qty": qty,
        "amount": price * qty,
        "unit_note": unit_note or _first_note(it["note"]),
        "evidence": [e["text"] for e in x["evidence"]],
        "evidence_nodes": [e["id"] for e in x["evidence"]],
        "negotiable": it["negotiable"],
        "assumption": assumption,
    }


def select_lines(task, grade, retrieved):
    d = task["deliverable"]
    opts = set(d.get("options") or [])
    pages = d.get("pages") or 0
    qty = max(1, d.get("quantity") or 1)
    items = retrieved["items"]
    lines = []

    def group(pred):
        return [x for x in items if pred(x["item"])]

    def priced(xs):
        return [x for x in xs if x["price"] or x["item"]["negotiable"]]

    # 기획비 줄: 컨셉 기획(신규만 단가 있음) · 디렉션 · 시안
    for x in priced(group(lambda it: it["cost_type"] == "기획비")):
        name = x["item"]["item"] or ""
        if "콘티" in name and "콘티" not in opts:
            continue
        lines.append(_line(task, x, grade, 1))

    # 디자인비 핵심 줄
    design = group(lambda it: it["cost_type"] in ("디자인비", "프로덕션") and any(w in f"{it['item']} {it['detail']}" for w in CORE_WORDS))
    has_cover = any((x["item"]["detail"] or "").startswith("표지") for x in design)
    if task["category"] == "video":
        c = _pick_duration(design, d.get("duration_sec") or 60)
        if c:
            lines.append(_line(task, c, grade, qty, None if d.get("duration_sec") else "영상 길이가 없어 60초 이하로 가정"))
    elif has_cover:
        cover = next(x for x in design if (x["item"]["detail"] or "").startswith("표지"))
        if cover["price"]:
            lines.append(_line(task, cover, grade, 1))
        inner = pages - 1 if pages else 0
        if inner > 0:
            c = _pick_by_pages([x for x in design if "내지" in (x["item"]["detail"] or "")], inner)
            if c and c["price"]:
                lines.append(_line(task, c, grade, inner, unit_note="내지 페이지당"))
    elif any(x["item"]["detail"] and _upper(x["item"]["detail"]) is None for x in design) and task["category"] != "package":
        c, assumption = _pick_by_format(design, d.get("format"))
        if c["price"]:
            lines.append(_line(task, c, grade, max(qty, pages or 1), assumption))
    else:
        for x in design:
            detail = x["item"]["detail"] or ""
            if "구조개발" in detail and "구조개발" not in opts:
                continue
            if x["price"]:
                lines.append(_line(task, x, grade, max(qty, pages or 1) if task["category"] in ("web_pdp", "web_banner", "ad_b") else qty))

    # 옵션 줄
    for x in items:
        it, name = x["item"], f"{x['item']['item']} {x['item']['detail'] or ''}"
        if not (x["price"] or it["negotiable"]):
            continue
        if "Animation" in name and "애니메이션" in opts:
            lines.append(_line(task, x, grade, max(qty, pages or 1)))
        elif "컴퓨터그래픽" in name and "CG" in opts:
            lines.append(_line(task, x, grade, max(1, pages or 1), "합성 시간은 페이지당 1시간으로 가정", "시간당"))
        elif "스톡" in name and "스톡" in opts:
            ratio = next((0.7 if "70%" in e else 0.5 for e in x["evidence"] if "%" in e), None)
            n = math.ceil(pages * ratio) if ratio and pages else 1
            lines.append(_line(task, x, grade, n, f"규칙 적용: 페이지의 {int(ratio * 100)}%" if ratio and pages else None, "이미지 개당"))
        elif it["cost_type"] == "카피" and "카피" in opts:
            lines.append(_line(task, x, grade, 1))
        elif "Stock Audio" in name and "BGM" in opts:
            lines.append(_line(task, x, grade, qty))
        elif "자막" in name and "자막" in opts:
            lines.append(_line(task, x, grade, qty))
        elif "Voice" in (it["item"] or "") and "A Class" in (it["detail"] or "") and "성우" in opts:
            lines.append(_line(task, x, grade, qty))

    # 편집비(원고 교정/교열): 편집물은 기본, 그 외에는 요청했을 때만
    # 수정 등급은 크리에이티브 단가가 없는 카테고리가 많아 원고 교정비로 청구한다
    edit = [x for x in items if x["item"]["cost_type"] == "편집비" and x["price"]]
    minor = grade.startswith("수정")
    if edit and (task["category"] in ("edit_a", "edit_b", "cardnews") or "교정" in opts or task["category"] == "ad_b" or minor):
        variants = [x for x in edit if x["item"]["detail"]]
        if not variants:
            lines.append(_line(task, edit[0], grade, max(qty, pages or 1)))
        elif has_cover:
            inner = pages - 1 if pages else 0
            c = _pick_by_pages(variants, inner) if inner > 0 else None
            if c:
                lines.append(_line(task, c, grade, inner, unit_note="1P당"))
        else:
            c, assumption = _pick_by_format(variants, d.get("format"))
            lines.append(_line(task, c, grade, max(qty, 1), assumption))
    return lines


def aggregate(lines):
    subtotal = sum(l["amount"] for l in lines)
    vat = round(subtotal * 0.10)
    return {"lines": lines, "subtotal": subtotal, "vat": vat, "total": subtotal + vat}


def has_core(lines, task_id):
    """작업마다 실제 작업비 줄이 하나는 있어야 한다. 수정 등급은 편집비(원고 교정)도 인정."""
    def core(l):
        if "디자인비" in l["label"] or "프로덕션" in l["label"]:
            return True
        return l["grade"].startswith("수정") and "편집비" in l["label"] or l["grade"] == "공통" and "편집비" in l["label"]
    return any(l["task_id"] == task_id and l["amount"] > 0 and core(l) for l in lines)
