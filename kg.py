"""단가표 지식 그래프 (GraphRAG).

노드
  cat:<id>        카테고리 (패키지, 카드뉴스 …)
  item:<id>       단가 항목 (기획비/시안제작비/크리에이티브 …)
  grade:<등급>    신규S · 신규A · 파생B · 파생C · 수정A · 수정B
  crit:<row>:<g>  등급 판정 기준 문장 (등급별 기준 시트)
  rule:<id>       여러 항목에 걸치는 규칙 (진행비 10%, 스톡 70% …)
엣지
  cat -HAS_ITEM-> item -PRICED_AT{price}-> grade
  item -EXPLAINED_BY-> crit -OF_GRADE-> grade
  cat/item -APPLIES-> rule

검색은 카테고리에서 출발해 항목 → 등급 → 판정 기준 → 규칙으로 걸어가며,
걸은 경로(path)를 그대로 돌려준다. 이 경로가 승인 화면의 '근거'가 된다.
"""
import json
from functools import lru_cache

import networkx as nx

import config

# 단가 항목 이름 → 등급별 기준 시트의 어느 행을 근거로 삼는지
# 구체적인 키워드가 먼저 걸리도록 순서를 둔다 (예: '크리에이티브 구조개발' → 구조개발)
CRITERIA_KEYS = [
    ("구조개발", "구조개발"),
    ("Animation", "Animation Graphic"),
    ("그래픽 개발", "크리에이티브"),
    ("컨셉 및 기획", "컨셉 및 기획료"),
    ("디렉션", "컨셉 및 기획료"),
    ("시안", "시안제작비"),
    ("크리에이티브", "크리에이티브"),
    ("컴퓨터그래픽", "컴퓨터그래픽 합성"),
    ("교정/교열", "원고 교정/교열"),
    ("카피", "카피라이팅"),
]


@lru_cache
def load_rate_card():
    return json.loads(config.RATE_CARD.read_text())


def item_label(it):
    parts = [it["cost_type"], it["item"], it["detail"]]
    return " / ".join(p for p in parts if p)


@lru_cache
def build():
    rc = load_rate_card()
    G = nx.DiGraph()
    for g in rc["grades"]:
        G.add_node(f"grade:{g}", kind="grade", label=g)

    # 판정 기준 문장
    crit_by_key = {}
    for row in rc["grade_criteria"]:
        if row["section"] not in ("디자인",):
            continue
        key = (row["detail"] or row["item"] or "").strip()
        for g, text in zip(rc["grades"], row["grades"]):
            if not text:
                continue
            nid = f"crit:{row['row']}:{g}"
            G.add_node(nid, kind="crit", label=text[:60], text=text, grade=g, key=key)
            G.add_edge(nid, f"grade:{g}", rel="OF_GRADE")
            crit_by_key.setdefault(key, []).append(nid)

    for r in rc["rules"]:
        G.add_node(f"rule:{r['id']}", kind="rule", label=r["text"], **{k: v for k, v in r.items() if k != "text"})

    for cat in rc["categories"]:
        cid = f"cat:{cat['id']}"
        G.add_node(cid, kind="cat", label=cat["name"], cat_id=cat["id"])
        for r in rc["rules"]:
            if "*" in r["applies_to"] or cat["id"] in r["applies_to"]:
                G.add_edge(cid, f"rule:{r['id']}", rel="APPLIES")
        for it in cat["items"]:
            iid = f"item:{it['id']}"
            G.add_node(iid, kind="item", label=item_label(it), data=it)
            G.add_edge(cid, iid, rel="HAS_ITEM")
            for g, price in it["prices"].items():
                G.add_edge(iid, f"grade:{g}", rel="PRICED_AT", price=price)
            name = f"{it['item'] or ''} {it['detail'] or ''}"
            for kw, crit_key in CRITERIA_KEYS:
                if kw in name:
                    for key, nids in crit_by_key.items():
                        if key.startswith(crit_key):
                            for n in nids:
                                G.add_edge(iid, n, rel="EXPLAINED_BY")
                    break
            if "스톡" in name:
                for rid in ("stock_70", "stock_50_banner"):
                    if cat["id"] in G.nodes[f"rule:{rid}"]["applies_to"]:
                        G.add_edge(iid, f"rule:{rid}", rel="APPLIES")
            if it["negotiable"]:
                G.add_edge(iid, "rule:negotiable", rel="APPLIES")
    return G


def categories():
    return [{"id": c["id"], "name": c["name"]} for c in load_rate_card()["categories"]]


def grade_criteria_text(category_id):
    """등급 투표 프롬프트에 넣을 판정 기준: 카테고리 항목에서 EXPLAINED_BY 로 닿는 문장."""
    G = build()
    lines, seen = [], set()
    for iid in G.successors(f"cat:{category_id}"):
        if G.nodes[iid]["kind"] != "item":
            continue
        for n in G.successors(iid):
            nd = G.nodes[n]
            if nd["kind"] == "crit" and n not in seen:
                seen.add(n)
                lines.append(f"[{nd['grade']}] {nd['key']}: {nd['text']}")
    return "\n".join(sorted(lines))


def retrieve(category_id, grade):
    """카테고리 → 항목 → (등급 단가) → 판정 기준 → 규칙.

    반환: items(해당 등급 단가가 있는 항목과 그 근거), rules, path(방문 순서)
    """
    G = build()
    cid = f"cat:{category_id}"
    path = [cid]
    items, rules = [], []
    for n in G.successors(cid):
        nd = G.nodes[n]
        if nd["kind"] == "rule":
            rules.append({"id": n, "text": nd["label"]})
            continue
        it = nd["data"]
        price = it["prices"].get(grade)
        evidence = []
        for m in G.successors(n):
            md = G.nodes[m]
            if md["kind"] == "crit" and md["grade"] == grade:
                evidence.append({"id": m, "text": md["text"]})
            elif md["kind"] == "rule":
                evidence.append({"id": m, "text": md["label"]})
        items.append({"node": n, "item": it, "price": price, "evidence": evidence})
    for x in items:
        if x["price"] or x["item"]["negotiable"]:
            path.append(x["node"])
            path.append(f"grade:{grade}")
            path.extend(e["id"] for e in x["evidence"])
    path.extend(r["id"] for r in rules)
    # 순서를 지키며 중복 제거
    seen, ordered = set(), []
    for p in path:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return {"items": items, "rules": rules, "path": ordered}


def export_for_view():
    """프런트 GraphRAG 패널용 노드·엣지 (항목은 방문 시에만 그린다)."""
    G = build()
    nodes = [{"id": n, "kind": d["kind"], "label": d["label"]} for n, d in G.nodes(data=True)]
    edges = [{"s": u, "t": v, "rel": d["rel"]} for u, v, d in G.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    G = build()
    kinds = {}
    for _, d in G.nodes(data=True):
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    print("노드", kinds, "엣지", G.number_of_edges())
    r = retrieve("package", "파생C")
    print("경로 길이", len(r["path"]))
    for x in r["items"]:
        if x["price"]:
            print(" ", item_label(x["item"]), x["price"], [e["text"][:30] for e in x["evidence"]])
