// JONIVERSE 견적 데스크 — 파이프라인 이벤트를 받아 사무실 무대와 단톡방을 움직인다.
const $ = (s, el = document) => el.querySelector(s);
const won = n => (n ?? 0).toLocaleString("ko-KR") + "원";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms / speed()));
const speed = () => Number($("#speed input:checked")?.value || 2);

let META = { roles: {}, designers: {} };
const workerInfo = {};   // task_id → 디자이너 {title, name, emoji}

// ── 사무실 배치 (백분율 좌표) ───────────────────────────
// tint: iOS 앱 아이콘처럼 역할마다 색을 준다 (시스템 색)
const STATIONS = {
  intake:       { x: 11, y: 36, ic: "📨", lb: "리셉션", pt: "메일 접수 · ① 체이닝", tint: "#0a84ff" },
  router:       { x: 11, y: 77, ic: "🗃️", lb: "분류 트레이", pt: "② 라우팅", tint: "#5ac8fa" },
  orchestrator: { x: 31, y: 57, ic: "🎨", lb: "AD 룸", pt: "④ 작업 분배", tint: "#af52de" },
  aggregate:    { x: 74, y: 32, ic: "🧮", lb: "정산 데스크", pt: "코드로 계산", tint: "#34c759" },
  evaluator:    { x: 74, y: 60, ic: "🔍", lb: "QC 테이블", pt: "⑤ 평가자", tint: "#ff9500" },
  gate:         { x: 91, y: 46, ic: "🖋️", lb: "실장님 결재", pt: "HITL", tint: "#ff375f" },
  outbox:       { x: 91, y: 83, ic: "📮", lb: "우편함", pt: "되돌릴 수 없음", tint: "#ff3b30" },
  archive:      { x: 74, y: 88, ic: "🗄️", lb: "캐비닛", pt: "보관 · 이관", tint: "#8e8e93" },
};
const STUDIO = { left: 43.5, top: 21, h: 76 };
const WORKER_X = 49, WORKER_Y = [36, 54, 72, 90];
const FLOW = [["intake", "router"], ["router", "orchestrator"], ["orchestrator", "W"], ["W", "aggregate"],
  ["aggregate", "evaluator"], ["evaluator", "gate"], ["gate", "outbox"], ["router", "archive"], ["gate", "archive"]];
const workerSlot = {};

const pos = key => key === "W" ? { x: 53, y: 57 } : STATIONS[key];

function drawStage() {
  const desks = WORKER_Y.map(y => `<div class="desk" style="top:${(y - STUDIO.top) / STUDIO.h * 100 - 7}%"><i></i>🖥️</div>`).join("");
  $("#stations").innerHTML =
    `<div class="studio" style="left:${STUDIO.left}%;top:${STUDIO.top}%;height:${STUDIO.h}%"><span>디자인 스튜디오 · ③ 등급 투표</span>${desks}</div>` +
    Object.entries(STATIONS).map(([k, s]) =>
      `<div class="station" id="st-${k}" style="left:${s.x}%;top:${s.y}%;--tint:${s.tint}"><div class="ic">${s.ic}</div><div class="lb">${s.lb}</div><div class="pt">${s.pt}</div></div>`).join("");
  $("#flow").innerHTML = FLOW.map(([a, b]) => {
    const p = pos(a), q = pos(b);
    return `<path id="fl-${a}-${b}" d="M${p.x},${p.y} C${(p.x + q.x) / 2},${p.y} ${(p.x + q.x) / 2},${q.y} ${q.x},${q.y}"/>`;
  }).join("");
}

// 사람 정보: 역할(manager·orchestrator·evaluator) 또는 워커(T1…)
function profile(id) {
  const r = META.roles[id];
  if (r) return { ...r, full: `${r.title} ${r.name}`.trim(), color: { manager: "#ffd9c9", orchestrator: "#d8e7ff", evaluator: "#e7defc", approver: "#fff2a8" }[id] };
  const d = workerInfo[id];
  if (d) return { ...d, full: `${d.title.replace(" 디자이너", "")} ${d.name}`, color: "#d7efe0" };
  return { name: id, title: "", emoji: "🙂", full: id, color: "#eee" };
}

function actor(id, x, y) {
  let el = $(`#a-${CSS.escape(id)}`);
  if (!el) {
    const p = profile(id);
    el = document.createElement("div");
    el.className = "actor pop";
    el.id = `a-${id}`;
    el.innerHTML = `<div class="bubble"></div><div class="body">${p.emoji}</div><div class="nm">${esc(p.full)}</div><div class="chips"></div>`;
    $("#actors").append(el);
  }
  place(el, x, y);
  return el;
}
function place(el, x, y) {
  el.style.left = x + "%"; el.style.top = y + "%";
  el.classList.toggle("edge-r", x > 80); el.classList.toggle("edge-l", x < 18);
}

const bubbleTimers = new Map();
function say(id, text, ms = 2200) {
  const el = $(`#a-${CSS.escape(id)}`);
  if (!el) return;
  const b = $(".bubble", el);
  b.textContent = text; b.classList.add("show");
  clearTimeout(bubbleTimers.get(id));
  bubbleTimers.set(id, setTimeout(() => b.classList.remove("show"), ms / speed() + 600));
}

// ── 진행 순서 (벽의 스테퍼) ──────────────────────────
const STEPS = [
  { key: "intake", lb: "접수" }, { key: "router", lb: "분류" }, { key: "orchestrator", lb: "작업 분배" },
  { key: "workers", lb: "단가 산출" }, { key: "aggregate", lb: "정산" }, { key: "evaluator", lb: "검수" },
  { key: "gate", lb: "결재 판단" }, { key: "outbox", lb: "발송" },
];
const PROJ_TEXT = { idle: "대기 중", run: "진행 중", stop: "실장님 결재 대기", hold: "발송 유예 중", done: "완료" };

// key 단계까지 진행. state: active | stop | hold | done | end, lastLabel: 마지막 단계 이름 바꾸기 (반려·이관)
function setStep(key, state = "active", lastLabel = null) {
  const idx = key === null ? -1 : STEPS.findIndex(s => s.key === key);
  $("#steps").innerHTML = STEPS.map((s, i) => {
    const cls = i < idx || (state === "done" && i === idx) ? "done" : i === idx ? state : "";
    const lb = i === STEPS.length - 1 && lastLabel ? lastLabel : s.lb;
    return `<li class="${cls}"${i === idx ? ' aria-current="step"' : ""}><span class="n">${cls === "done" ? "✓" : i + 1}</span><span class="t">${lb}</span></li>`;
  }).join("");
  const proj = state === "done" ? "done" : state === "end" ? "done" : idx < 0 ? "idle" : state === "active" ? "run" : state;
  $(".proj").dataset.state = proj;
  $("#projState").textContent = state === "end" ? `종료 · ${lastLabel}` : PROJ_TEXT[proj];
}

function activate(key, cls = "active") {
  document.querySelectorAll(".station").forEach(s => s.classList.remove("active"));
  $(`#st-${key}`)?.classList.add(cls);
}
const lightFlow = (a, b) => $(`#fl-${a}-${b}`)?.classList.add("lit");

async function flyNote(from, to, label) {
  const n = document.createElement("div");
  n.className = "note"; n.textContent = label;
  n.style.left = from.x + "%"; n.style.top = from.y + "%";
  $("#fx").append(n);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  n.style.transitionDuration = `${0.7 / speed()}s`;
  n.style.left = to.x + "%"; n.style.top = to.y + "%";
  await sleep(750);
  n.style.opacity = 0;
  setTimeout(() => n.remove(), 400);
}

function stamp(at, text, ok) {
  const s = document.createElement("div");
  s.className = `stamp ${ok ? "ok" : "bad"}`; s.textContent = text;
  s.style.left = at.x + "%"; s.style.top = at.y + "%";
  $("#fx").append(s);
  setTimeout(() => s.remove(), 2600 / speed() + 800);
}

let lastStation = "intake";
function resetStage(title, { keepChat = false } = {}) {
  $("#actors").innerHTML = ""; $("#fx").innerHTML = "";
  document.querySelectorAll(".station").forEach(s => s.className = "station");
  document.querySelectorAll(".flow path").forEach(p => p.classList.remove("lit"));
  Object.keys(workerSlot).forEach(k => delete workerSlot[k]);
  $("#stageTitle").textContent = title;
  $("#chatSub").textContent = title;
  setStep(null);
  if (!keepChat) { $("#chat").innerHTML = ""; lastSender = null; chatSys(new Date().toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric", weekday: "long" }), "date"); }
  const s = STATIONS.intake;
  actor("manager", s.x + 6, s.y - 4);
  actor("orchestrator", STATIONS.orchestrator.x, STATIONS.orchestrator.y - 8);
  actor("evaluator", STATIONS.evaluator.x - 8, STATIONS.evaluator.y + 2);
  lastStation = "intake";
}

// ── 단톡방 ───────────────────────────────────────────
let lastSender = null;
const clock = () => new Date().toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" });

function chatMsg(from, html, { mine = false } = {}) {
  const box = $("#chat");
  const cont = lastSender === (mine ? "__me" : from);
  lastSender = mine ? "__me" : from;
  const p = mine ? profile("approver") : profile(from);
  const m = document.createElement("div");
  m.className = `msg${mine ? " mine" : ""}${cont ? " cont" : ""}`;
  m.innerHTML = `<div class="av" style="--av:${p.color}">${p.emoji}</div>
    <div class="col"><div class="nm">${esc(p.name)}<em>${esc(p.title)}</em></div>
    <div class="line"><div class="b${html.startsWith("<div class=\"ch\"") ? " card" : ""}">${html}</div><span class="t">${clock()}</span></div></div>`;
  box.append(m);
  box.scrollTop = box.scrollHeight;
}
function chatSys(text, cls = "") {
  const s = document.createElement("div");
  s.className = `sys ${cls}`; s.textContent = text;
  $("#chat").append(s);
  $("#chat").scrollTop = 1e9;
  lastSender = null;
}
const chatMine = text => chatMsg("approver", esc(text), { mine: true });

// ── 이벤트 재생 ─────────────────────────────────────────
const handlers = {
  async move(e) {
    const s = STATIONS[e.to]; if (!s) return;
    const el = $("#a-manager");
    lightFlow(lastStation, e.to);
    if (lastStation === "orchestrator" && e.to === "aggregate") { lightFlow("orchestrator", "W"); lightFlow("W", "aggregate"); }
    lastStation = e.to;
    if (e.to !== "archive" && e.to !== "outbox") setStep(e.to);
    el.classList.add("walking");
    el.style.transitionDuration = `${0.9 / speed()}s`;
    place(el, s.x + (s.x > 80 ? -7 : 6), s.y - 4);
    activate(e.to);
    await sleep(950);
    el.classList.remove("walking");
  },
  async say(e) {
    say(e.actor, e.text);
    chatMsg(e.actor, esc(e.text));
    await sleep(1300);
  },
  async dispatch(e) {
    setStep("workers");
    workerInfo[e.to] = e.designer;
    const i = Object.keys(workerSlot).length;
    workerSlot[e.to] = i;
    const y = WORKER_Y[i % WORKER_Y.length];
    chatMsg("orchestrator", `<span class="mention">@${esc(e.designer.name)}</span> ${esc(e.label)} 부탁해요!<br><small>“${esc(e.task)}”</small>`);
    await flyNote(pos("orchestrator"), { x: WORKER_X, y: y - 4 }, `📄 ${e.label}`);
    actor(e.to, WORKER_X, y);
    await sleep(300);
  },
  async vote(e) {
    const el = $(`#a-${CSS.escape(e.actor)}`);
    const chipHtml = cls => e.votes.map((v, i) =>
      `<span class="${cls} ${!e.agreed && v !== e.grade ? "bad" : ""}" style="animation-delay:${i * 0.15 / speed()}s">${v}</span>`).join("");
    const chips = chipHtml("");
    if (el) $(".chips", el).innerHTML = chipHtml("chip");
    say(e.actor, e.agreed ? `3번 다 ${e.grade}!` : `표가 갈렸어요… ${e.votes.join("/")}`);
    chatMsg(e.actor, `등급 판정 3번 해봤어요 → <b>${e.grade}</b>${e.agreed ? "" : " (의견 갈림 ⚠︎)"}<div class="chipline">${chips}</div>`);
    await sleep(1200);
  },
  async kg(e) {
    say(e.actor, `📚 자료실에서 단가표 찾는 중…`);
    const found = await drawKgPath(e.actor, e.path);
    const items = found.items.length;
    chatMsg(e.actor, `<div class="ch">📚 자료실 · 후보 ${e.candidates}개 중 ${items}개 항목</div><ul>${found.evidence.slice(0, 3).map(t => `<li>${esc(t.slice(0, 48))}</li>`).join("")}</ul>`);
  },
  async report(e) {
    const i = workerSlot[e.from] ?? 0;
    say(e.from, `완료! ${e.label}`);
    chatMsg(e.from, `끝났어요 🙌 ${esc(e.label)}`);
    await flyNote({ x: WORKER_X, y: WORKER_Y[i % 4] - 4 }, pos("orchestrator"), `✅ ${e.label}`);
  },
  async eval(e) {
    const s = STATIONS.evaluator;
    stamp({ x: s.x, y: s.y + 9 }, e.passed ? "PASS" : `재작성 ${e.attempt}`, e.passed);
    if (!e.passed && e.issues.length > 1) chatMsg("evaluator", `<div class="ch">🔍 QC 의견 ${e.issues.length}건</div><ul>${e.issues.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
    await sleep(900);
  },
  async stop(e) {
    $("#holdbox")?.remove();
    setStep("gate", "stop");
    $("#st-gate").classList.add("stop");
    e.reasons.forEach(r => chatSys(`⏸ 결재 필요 · ${r.label} (${r.detail})`, "stop"));
    await sleep(800);
  },
  async hold(e) {
    const s = STATIONS.outbox, el = $("#a-manager");
    setStep("outbox", "hold", "발송 유예");
    lightFlow("gate", "outbox");
    el.classList.add("walking");
    place(el, s.x - 7, s.y - 4);
    activate("outbox");
    await sleep(950);
    el.classList.remove("walking");
    showHoldBox(stageTid, e.hold_until);
    say("manager", `${e.seconds}초 뒤 발송돼요 ⏳`, 3000);
    chatSys(`📮 우편함 투입 · ${e.seconds}초 뒤 자동 발송 (실장님이 멈출 수 있어요)`, "warn");
  },
  async interrupt(e) {
    say("manager", "실장님, 결재 부탁드려요! 🙏", 4000);
    chatMsg("manager", `<span class="mention">@실장님</span> 결재 부탁드려요! 🙏 결재함에 올려 뒀어요.`);
    await sleep(600);
  },
  async done(e) {
    $("#holdbox")?.remove();
    const ok = e.status === "sent";
    if (ok) setStep("outbox", "done");
    else setStep("outbox", "end", e.status === "handoff" ? "이관" : "반려");
    const at = STATIONS[ok ? "outbox" : "archive"];
    if (ok) {
      lightFlow("gate", "outbox");
      await flyNote(STATIONS.gate, at, "✉️ 견적서");
      $("#st-gate").classList.remove("stop"); $("#st-gate").classList.add("okay");
    }
    activate(ok ? "outbox" : "archive", ok ? "okay" : "active");
    stamp({ x: at.x - 6, y: at.y - 12 }, ok ? "발송" : e.status === "handoff" ? "이관" : "반려", ok);
    say("manager", e.text);
    chatSys(ok ? `✉️ ${e.text}` : `🗄️ ${e.text}`, ok ? "ok" : "");
    if (e.quote) chatMsg("manager", `발송한 견적서 여기 있어요 👉 <a href="#" data-quote="${esc(e.quote)}">📎 견적서 열기</a>`);
    await sleep(1200);
  },
  async error(e) { chatSys(`오류 · ${e.text}`, "stop"); say("manager", "앗, 오류가 났어요 😵"); },
  async end() {},
};

let playing = Promise.resolve();
let stageTid = null;            // 지금 무대에 올라 있는 건

function watch(tid, since = 0) {
  // 이벤트는 빠르게 오고, 재생은 순서대로 천천히 한다.
  // 무대가 다른 건으로 바뀌면 재생은 건너뛰고 끝만 기다린다.
  return new Promise(resolve => {
    const queue = [];
    let closed = false, busy = false, last = since;
    const src = new EventSource(`/api/events/${encodeURIComponent(tid)}?since=${since}`);
    const pump = async () => {
      if (busy) return; busy = true;
      while (queue.length) {
        const ev = queue.shift();
        last = ev.i;
        if (stageTid === tid) await (handlers[ev.type] || (async () => {}))(ev);
        if (ev.type === "end") { busy = false; refreshLists(); resolve({ status: ev.status, last }); return; }
      }
      busy = false;
    };
    src.onmessage = m => { const ev = JSON.parse(m.data); queue.push(ev); if (ev.type === "end") { src.close(); closed = true; } pump(); };
    src.onerror = () => { if (!closed) { src.close(); resolve({ status: "error", last }); } };
  });
}

// 유예 건: 타이머 만료나 실장님 조작으로 재개되면 이어지는 이벤트를 무대에 재생한다
async function followHold(tid, since) {
  const { status } = await watch(tid, since);
  if (status === "waiting" && stageTid === tid) openApprovalFor(tid);
}

async function runRequest(body, title, client, { openApproval = true } = {}) {
  await playing;
  let finish;
  playing = new Promise(r => finish = r);
  resetStage(title);
  chatSys(`📨 ${client}에서 새 견적 메일`);
  const { thread_id } = await api("/api/run", body);
  stageTid = thread_id;
  refreshLists();
  const { status, last } = await watch(thread_id);
  finish();
  if (status === "waiting") {
    if (openApproval) openApprovalFor(thread_id);
    else toast(`${client} — 실장님 결재함에 올라갔어요`);
  }
  if (status === "holding" && openApproval) followHold(thread_id, last);
  return status;
}

// ── 발송 유예 ─────────────────────────────────────────
let clockOffset = 0;            // 서버 시각 - 브라우저 시각 (ms)
const remain = until => Math.max(0, until * 1000 - (Date.now() + clockOffset));

function showHoldBox(tid, until) {
  $("#holdbox")?.remove();
  const s = STATIONS.outbox;
  const box = document.createElement("div");
  box.id = "holdbox"; box.className = "holdbox";
  box.style.left = (s.x - 10) + "%"; box.style.top = (s.y - 24) + "%";
  box.dataset.until = until; box.dataset.total = remain(until);
  box.innerHTML = `<div class="cd"><span>–</span><small> 초 뒤 자동 발송</small></div><div class="bar"><i></i></div>
    <div class="btns"><button class="go" data-hold="send_now">지금 발송</button><button data-hold="escalate">보류</button><button data-hold="cancel">취소</button></div>`;
  box.onclick = e => { const b = e.target.closest("[data-hold]"); if (b) holdAction(tid, b.dataset.hold); };
  $("#fx").append(box);
}

async function holdAction(tid, action) {
  let reason = null;
  if (action === "escalate") {
    reason = await ask({ title: "발송 보류", message: "결재함으로 옮깁니다.\n결재 기준이 놓친 이유를 적어 두면 기준 개선에 씁니다.", input: "금액 재확인 필요", ok: "보류" });
    if (reason === null) return;
  }
  if (action === "cancel") {
    reason = await ask({ title: "발송 취소", message: "이 견적은 발송되지 않고 반려로 기록됩니다.", input: "고객 요청 철회", ok: "발송 취소", danger: true });
    if (reason === null) return;
  }
  $("#holdbox")?.remove();
  try { await api(`/api/resume/${encodeURIComponent(tid)}`, { action, reason }); } catch { return; }
  if (stageTid === tid) chatMine({ send_now: "지금 바로 보내 주세요", escalate: `잠깐, 보류할게요. ${reason}`, cancel: `이 건은 발송 취소해 주세요. ${reason}` }[action]);
  toast({ send_now: "바로 발송합니다", escalate: "결재함으로 옮겼어요", cancel: "발송을 취소했어요" }[action]);
  refreshLists();
  if (action === "escalate" && stageTid !== tid) setTimeout(() => openApprovalFor(tid), 800);
}

function tick() {
  document.querySelectorAll("[data-until]").forEach(el => {
    const ms = remain(Number(el.dataset.until));
    const sec = Math.ceil(ms / 1000);
    if (el.id === "holdbox") {
      $(".cd span", el).textContent = sec;
      $(".bar i", el).style.width = (ms / Number(el.dataset.total || 1) * 100) + "%";
    } else {
      el.textContent = sec > 0 ? `⏳ ${sec}초` : "발송 중…";
    }
  });
}
setInterval(tick, 250);
setInterval(() => { if (Number($("#holdCount").textContent) > 0) refreshLists(); }, 1500);

// ── 자료실 (GraphRAG) ───────────────────────────────────
let KG = null;
const KG_COL = { cat: 16, item: 118, grade: 222, crit: 300, rule: 300 };
const KG_COLOR = { cat: "var(--k-cat)", item: "var(--k-item)", grade: "var(--k-grade)", crit: "var(--k-crit)", rule: "var(--k-rule)" };
const short = (s, n = 10) => (s.length > n ? s.slice(0, n) + "…" : s);

function kgLabel(n) {
  if (n.kind === "cat") return short(n.label.split("(")[0].trim(), 9);
  if (n.kind === "item") return short(n.label.split(" / ").slice(-1)[0], 9);
  if (n.kind === "crit") return short(n.label.replace(/^[⁃\-\s]+/, ""), 12);
  if (n.kind === "rule") return short(n.label, 12);
  return n.label;
}

function kgBase() {
  const layout = {};
  KG.nodes.filter(n => n.kind === "cat").forEach((n, i) => layout[n.id] = { x: KG_COL.cat, y: 26 + i * 40 });
  KG.nodes.filter(n => n.kind === "grade").forEach((n, i) => layout[n.id] = { x: KG_COL.grade, y: 60 + i * 60 });
  return layout;
}

function renderKg(layout, visible, lit = new Set(), fresh = new Set()) {
  const byId = Object.fromEntries(KG.nodes.map(n => [n.id, n]));
  // 탐색 경로 위의 엣지만 그린다 (전체 엣지는 1,000개가 넘어 읽을 수 없다)
  const edges = KG.edges.filter(e => layout[e.s] && layout[e.t] && lit.has(e.s) && lit.has(e.t));
  $("#kg").innerHTML =
    edges.map(e => `<line x1="${layout[e.s].x}" y1="${layout[e.s].y}" x2="${layout[e.t].x}" y2="${layout[e.t].y}"/>`).join("") +
    Object.entries(layout).map(([id, p]) => {
      const n = byId[id];
      const cls = ["n", lit.has(id) ? "hit" : visible.has(id) ? "" : "dim", fresh.has(id) ? "new" : ""].join(" ");
      return `<g class="${cls}"><title>${esc(n.label)}</title><circle cx="${p.x}" cy="${p.y}" r="${n.kind === "cat" || n.kind === "grade" ? 6.5 : 5}" fill="${KG_COLOR[n.kind]}"/><text x="${p.x + 9}" y="${p.y + 3.5}">${esc(kgLabel(n))}</text></g>`;
    }).join("");
}

async function drawKgPath(who, path) {
  if (!KG) return { items: [], evidence: [] };
  const byId = Object.fromEntries(KG.nodes.map(n => [n.id, n]));
  const layout = kgBase();
  const items = path.filter(id => byId[id]?.kind === "item");
  const right = path.filter(id => ["crit", "rule"].includes(byId[id]?.kind));
  items.forEach((id, i) => layout[id] = { x: KG_COL.item, y: 20 + i * Math.min(40, 430 / Math.max(items.length, 1)) });
  right.forEach((id, i) => layout[id] = { x: KG_COL.crit, y: 20 + i * Math.min(40, 430 / Math.max(right.length, 1)) });
  const catNode = byId[path[0]];
  const gradeNode = path.map(id => byId[id]).find(n => n?.kind === "grade");
  $("#kgSub").textContent = `${profile(who).full} · ${catNode?.label.split("(")[0].trim()} → ${gradeNode?.label ?? ""} · 경로 ${path.length}개 노드`;
  const visible = new Set(KG.nodes.filter(n => n.kind === "cat" || n.kind === "grade").map(n => n.id));
  const lit = new Set();
  const step = Math.max(40, 1600 / path.length);
  for (const id of path) {
    visible.add(id); lit.add(id);
    renderKg(layout, visible, lit, new Set([id]));
    await sleep(step);
  }
  const evidence = right.filter(id => byId[id].kind === "crit").map(id => byId[id].label);
  $("#kgEvidence").innerHTML = right.map(id => `<li class="${byId[id].kind}">${esc(byId[id].label)}</li>`).join("");
  await sleep(500);
  return { items, evidence };
}

// ── 목록 ─────────────────────────────────────────────
const STATUS_TXT = { sent: "발송", waiting: "결재 대기", rejected: "반려", handoff: "이관", running: "처리 중", holding: "유예" };

async function refreshLists() {
  const { now, threads } = await (await fetch("/api/threads")).json();
  clockOffset = now * 1000 - Date.now();
  const hold = threads.filter(t => t.status === "holding");
  const pend = threads.filter(t => t.status === "waiting");
  const done = threads.filter(t => !["waiting", "holding"].includes(t.status));
  $("#holdCount").textContent = hold.length; $("#holdCount").dataset.n = hold.length;
  $("#holding").innerHTML = hold.length ? hold.map(t =>
    `<li data-tid="${esc(t.thread_id)}"><b>${esc(t.request.client)}</b><span class="tag holding" data-until="${t.hold_until}">⏳</span>
      <small>${won(t.subtotal)} · 결재 기준 미해당, 곧 자동 발송</small>
      <div class="row-btns"><button data-hold="send_now">지금 발송</button><button data-hold="escalate">보류</button><button data-hold="cancel">취소</button></div></li>`).join("")
    : `<li class="empty">우편함이 비어 있어요</li>`;
  tick();
  $("#pendingCount").textContent = pend.length; $("#pendingCount").dataset.n = pend.length;
  $("#pending").innerHTML = pend.length ? pend.map(t =>
    `<li data-tid="${esc(t.thread_id)}"><b>${esc(t.request.client)}</b><span class="tag waiting">${won(t.subtotal)}</span><small>${esc(t.stop_reasons.join(" · "))}</small></li>`).join("")
    : `<li class="empty">결재할 건이 없어요</li>`;
  $("#done").innerHTML = done.map(t =>
    `<li data-tid="${esc(t.thread_id)}"><b>${esc(t.request.client)}</b><span class="tag ${t.status}">${STATUS_TXT[t.status] || t.status}${t.approved_by === "human" ? " · 실장님" : t.approved_by === "auto" ? " · 자동" : ""}</span><small>${t.subtotal != null ? won(t.subtotal) + " · " : ""}${esc(t.request.text)}</small></li>`).join("");
  const n = threads.length, stopped = threads.filter(t => t.stop_reasons.length).length;
  $("#stat").textContent = n ? `${n}건 중 ${stopped}건 결재 · 개입률 ${Math.round(stopped / n * 100)}%` : "";
  refreshMail();
}

// ── 메일 수신함 ─────────────────────────────────────────
let MAIL = { mails: [] };
const MAIL_STATUS = { new: "새 메일", running: "처리 중", holding: "유예", waiting: "결재 대기", sent: "발송", rejected: "반려", handoff: "이관" };
const fmtWhen = iso => {
  const d = new Date(iso), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
};

async function refreshMail() {
  MAIL = await (await fetch("/api/mail")).json();
  const fresh = MAIL.mails.filter(m => m.status === "new").length;
  $("#mailCount").textContent = fresh; $("#mailCount").dataset.n = fresh;
  const src = MAIL.source === "imap" ? (MAIL.username || "IMAP 미설정") : "테스트 폴더 (data/inbox)";
  $("#mailStatus").innerHTML = `<span class="dot ${MAIL.auto_fetch ? "on" : ""}"></span>${esc(src)}` +
    ` · ${MAIL.auto_fetch ? `${MAIL.poll_minutes}분마다 자동 확인` : "수동 확인"}` +
    (MAIL.auto_process ? " · 자동 견적" : "") +
    (MAIL.last_fetch ? ` · 마지막 ${fmtWhen(MAIL.last_fetch)}` : "");
  $("#mails").innerHTML = MAIL.mails.length ? MAIL.mails.map(m => `
    <li data-mail="${esc(m.mail_id)}" class="${m.status === "new" ? "new" : ""}">
      <b>${esc(m.from_name)}</b><span class="when">${fmtWhen(m.received)}</span>
      <span class="subj">${esc(m.subject)}</span>
      <small>${esc(m.preview)}</small>
      ${m.status === "new" ? `<button class="btn sm go" data-go>견적 처리 →</button>` : `<span class="tag ${m.status} go">${MAIL_STATUS[m.status] || m.status}</span>`}
    </li>`).join("")
    : `<li class="empty">⟳ 가져오기를 눌러 메일을 불러오세요</li>`;
}

async function fetchMail() {
  $("#mailFetch").disabled = true;
  try {
    const r = await api("/api/mail/fetch", {});
    toast(r.new ? `새 견적 메일 ${r.new}통을 가져왔어요` : "새 메일이 없어요");
  } catch { /* 토스트로 알림 */ }
  $("#mailFetch").disabled = false;
  refreshMail(); refreshLists();
}

function onMailClick(e) {
  const li = e.target.closest("li[data-mail]"); if (!li) return;
  const m = MAIL.mails.find(x => x.mail_id === li.dataset.mail); if (!m) return;
  if (e.target.closest("[data-go]")) {
    runRequest({ mail_id: m.mail_id }, `${m.from_name} — ${m.subject}`, m.from_name);
    return;
  }
  if (m.status === "sent") openQuote(m.thread_id, { client: m.from_name });
  else if (m.status === "waiting") openApprovalFor(m.thread_id);
}

async function openMailSettings() {
  const s = await (await fetch("/api/mail/settings")).json();
  const opt = Object.entries(s.presets).map(([k, p]) => `<option value="${k}" ${k === s.preset ? "selected" : ""}>${p.label}</option>`).join("");
  $("#mBody").innerHTML = `
    <div class="kicker">JONIVERSE · 메일 수신 설정</div>
    <h3 id="mTitle">견적 메일 가져오기</h3>
    <div class="sub">견적 요청이 들어오는 메일함을 연결합니다. 가져온 메일은 수신함에 쌓이고, '견적 처리'를 누르거나 자동 처리로 파이프라인에 들어갑니다.</div>
    <form class="mailset" id="msForm">
      <div class="src">
        <label><input type="radio" name="source" value="folder" ${s.source === "folder" ? "checked" : ""}><b>테스트 폴더</b>data/inbox 의 .eml 파일을 메일처럼 가져와요. 계정 없이 시연할 때.</label>
        <label><input type="radio" name="source" value="imap" ${s.source === "imap" ? "checked" : ""}><b>메일 계정 (IMAP)</b>Gmail·네이버·다음·회사 메일에서 직접 가져와요.</label>
      </div>
      <div data-imap>
        <div class="grid">
          <label class="field">메일 서비스<select name="preset">${opt}</select></label>
          <label class="field">메일 주소 (아이디)<input name="username" value="${esc(s.username)}" placeholder="quote@joniverse.com"></label>
          <label class="field">IMAP 서버<input name="host" value="${esc(s.host)}"></label>
          <label class="field">포트<input name="port" type="number" value="${s.port}"></label>
          <label class="field">메일함 폴더<input name="folder" value="${esc(s.folder)}"></label>
          <div class="field">비밀번호<span class="pw ${s.password_set ? "ok" : "no"}">${s.password_set ? "✓ .env 의 MAIL_PASSWORD 사용 중" : "✗ 아직 설정 안 됨"}</span></div>
        </div>
        <div class="help" id="msHelp"></div>
        <div class="help">비밀번호는 보안을 위해 이 화면에서 받지 않아요. 프로젝트 폴더의 <code>.env</code> 파일에 <code>MAIL_PASSWORD=앱 비밀번호</code> 를 넣고 서버를 다시 시작하세요.</div>
      </div>
      <div class="grid">
        <label class="field">가져올 메일 (제목·본문 키워드)<input name="subject_filter" value="${esc(s.subject_filter)}" placeholder="비우면 전부"></label>
        <label class="field">최근 며칠<input name="since_days" type="number" min="1" value="${s.since_days}"></label>
      </div>
      <div class="checks">
        <label><input type="checkbox" name="auto_fetch" ${s.auto_fetch ? "checked" : ""}> 자동으로 가져오기 · <input name="poll_minutes" type="number" min="1" value="${s.poll_minutes}"> 분마다</label>
        <label><input type="checkbox" name="auto_process" ${s.auto_process ? "checked" : ""}> 새 메일은 바로 견적 처리 <small style="color:var(--muted)">(결재 기준에 걸리면 결재함으로)</small></label>
      </div>
      <div class="result" id="msResult"></div>
      <div class="actions">
        <button class="btn ghost" type="button" id="msTest">연결 테스트</button>
        <button class="btn" type="submit">저장</button>
      </div>
    </form>`;
  const f = $("#msForm");
  const syncSource = () => $("[data-imap]", f).hidden = f.source.value !== "imap";
  const syncHelp = () => $("#msHelp").textContent = s.presets[f.preset.value].help;
  f.querySelectorAll("[name=source]").forEach(r => r.onchange = syncSource);
  f.preset.onchange = () => {
    const p = s.presets[f.preset.value];
    if (p.host) { f.host.value = p.host; f.port.value = p.port; }
    syncHelp();
  };
  syncSource(); syncHelp();
  const collect = () => ({
    source: f.source.value, preset: f.preset.value, username: f.username.value.trim(), host: f.host.value.trim(),
    port: Number(f.port.value), folder: f.folder.value.trim() || "INBOX", subject_filter: f.subject_filter.value.trim(),
    since_days: Number(f.since_days.value), auto_fetch: f.auto_fetch.checked, poll_minutes: Number(f.poll_minutes.value),
    auto_process: f.auto_process.checked,
  });
  $("#msTest").onclick = async () => {
    const out = $("#msResult"); out.className = "result"; out.textContent = "연결하는 중…";
    await api("/api/mail/settings", collect());
    const r = await api("/api/mail/test", {});
    out.className = `result ${r.ok ? "ok" : "bad"}`; out.textContent = r.message;
  };
  f.onsubmit = async e => {
    e.preventDefault();
    await api("/api/mail/settings", collect());
    $("#modal").hidden = true;
    toast("메일 설정을 저장했어요");
    refreshMail();
  };
  $("#modal").hidden = false;
}

// ── 결재 화면 ─────────────────────────────────────────
let current = null;

async function openApprovalFor(tid) {
  const t = await (await fetch(`/api/threads/${encodeURIComponent(tid)}`)).json();
  if (!t.interrupt || t.interrupt.kind !== "approval") { toast("이미 처리된 건이에요"); return; }
  current = { tid, p: t.interrupt, editing: false };
  renderApproval();
  $("#modal").hidden = false;
}

function renderApproval() {
  const { p, editing } = current;
  const q = p.quote;
  const tasks = Object.fromEntries((p.grade_votes || []).map(v => [v.task_id, v]));
  $("#mBody").innerHTML = `
    <div class="kicker">JONIVERSE · 결재 요청</div>
    <h3 id="mTitle">${esc(p.request.client)} 견적서 결재</h3>
    <div class="sub">${esc(p.request.id)} · ${esc(p.extraction.summary)}</div>
    <div class="reasons">${p.stop_reasons.map(r => `<div class="reason" title="${esc(r.risk)}"><b>⏸ ${esc(r.label)}</b><span>${esc(r.detail)}</span></div>`).join("")}</div>
    <div class="grid2">
      <div class="box"><h4>고객 메일 원문</h4><div class="quote-orig">“${esc(p.request.text)}”</div></div>
      <div class="box"><h4>견적 금액</h4>
        <div class="money">${won(q.subtotal)} <small>공급가 · VAT 포함 ${won(q.total)}</small></div>
        <h4 style="margin-top:10px">디자이너 등급 판정 (3회)</h4>
        <div class="votes">${(p.grade_votes || []).map(v => `<span class="${v.agreed ? "" : "bad"}">${v.task_id} ${v.grade} <small>(${v.votes.join("/")})</small></span>`).join("")}</div>
      </div>
    </div>
    <div class="table-wrap"><table class="lines">
      <thead><tr><th>작업</th><th>항목 · 근거</th><th>등급</th><th class="num">단가</th><th class="num">수량</th><th class="num">금액</th>${editing ? "<th>빼기</th>" : ""}</tr></thead>
      <tbody>${q.lines.map(l => `
        <tr class="${l.negotiable ? "neg" : ""}" data-lid="${esc(l.line_id)}">
          <td>${esc(l.task_id)}</td>
          <td>${esc(l.label)}${l.negotiable ? " <b>(별도 협의)</b>" : ""}
            ${l.evidence.length ? `<div class="ev">근거: ${esc(l.evidence[0].slice(0, 70))}</div>` : ""}
            ${l.assumption ? `<div class="as">가정: ${esc(l.assumption)}</div>` : ""}</td>
          <td>${esc(l.grade)}${tasks[l.task_id] && !tasks[l.task_id].agreed ? " ⚠︎" : ""}</td>
          <td class="num">${editing ? `<input class="u" value="${l.unit_price}">` : won(l.unit_price)}</td>
          <td class="num">${editing ? `<input class="q" value="${l.qty}">` : l.qty}</td>
          <td class="num">${won(l.amount)}</td>
          ${editing ? `<td><input type="checkbox" class="rm"></td>` : ""}
        </tr>`).join("")}</tbody>
    </table></div>
    <details class="draft"><summary>견적서 문안 미리보기</summary>
      <p>${esc(p.draft.greeting)}</p><p>${esc(p.draft.scope_summary)}</p>
      <p><b>포함</b> ${esc(p.draft.included.join(", "))}</p><p><b>불포함</b> ${esc(p.draft.excluded.join(", "))}</p>
      <p>${esc(p.draft.schedule_note)}</p></details>
    <div class="effect">결재하면 → ${esc(p.if_approved)}<small>발송된 견적서는 되돌릴 수 없습니다.</small>
      <button class="btn ghost sm preview" type="button" data-act="preview">📄 나갈 견적서 미리보기</button></div>
    <div class="actions">
      ${editing
        ? `<button class="btn ok" data-act="edit-send">수정한 금액으로 결재</button><button class="btn gray" data-act="edit-cancel">취소</button>`
        : `<button class="btn ok" data-act="approve">결재 (승인)</button>
           <button class="btn warn" data-act="edit">수정 후 결재</button>
           <input id="mText" placeholder="반려 사유 또는 다시 판정 지시 (예: 파생B로 다시 계산)">
           <button class="btn ghost" data-act="redo" ${p.redo_left > 0 ? "" : "disabled"}>다시 판정 (${p.redo_left}회 남음)</button>
           <button class="btn gray" data-act="reject">반려</button>`}
    </div>`;
}

async function decide(act) {
  if (act === "preview") { openQuote(current.tid, { draft: true, client: current.p.request.client }); return; }
  if (act === "edit") { current.editing = true; renderApproval(); return; }
  if (act === "edit-cancel") { current.editing = false; renderApproval(); return; }
  const text = $("#mText")?.value.trim();
  let body, said;
  if (act === "approve") { body = { action: "approve" }; said = "확인했어요. 결재합니다 👍"; }
  if (act === "reject") { if (!text) { toast("반려 사유를 적어 주세요"); return; } body = { action: "reject", reason: text }; said = `반려할게요. ${text}`; }
  if (act === "redo") { if (!text) { toast("다시 판정할 지시를 적어 주세요"); return; } body = { action: "redo", instruction: text }; said = `다시 봐 주세요 — ${text}`; }
  if (act === "edit-send") {
    const lines = [...document.querySelectorAll("#mBody tbody tr")].map(tr => ({
      line_id: tr.dataset.lid, unit_price: Number($(".u", tr).value.replace(/[^\d]/g, "")),
      qty: Number($(".q", tr).value), remove: $(".rm", tr).checked,
    }));
    body = { action: "edit", lines }; said = "금액 조금 고쳐서 결재할게요 ✍️";
  }
  $("#modal").hidden = true;
  const tid = current.tid, p = current.p;
  const { since } = await api(`/api/resume/${encodeURIComponent(tid)}`, body);
  await playing;
  let finish; playing = new Promise(r => finish = r);
  const same = stageTid === tid;
  resetStage(`${p.request.client} — 실장님 결재`, { keepChat: same });
  if (!same) chatSys(`🖋️ ${p.request.client} 건 결재`);
  chatMine(said);
  const g = STATIONS.gate;
  place($("#a-manager"), g.x - 7, g.y - 4); lastStation = "gate"; activate("gate"); setStep("gate");
  stageTid = tid;
  const { status } = await watch(tid, since);
  finish();
  if (status === "waiting") openApprovalFor(tid);
}

// ── 공통 ─────────────────────────────────────────────
async function api(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || "요청 실패"); throw new Error(e.detail); }
  return r.json();
}
// 견적서 뷰어 (새 탭 대신 모달)
function openQuote(tid, { draft = false, client = "" } = {}) {
  $("#qKicker").textContent = draft ? "발송 전 미리보기 · DRAFT" : "발송된 견적서";
  $("#qTitle").textContent = client ? `${client} 견적서` : "견적서";
  $("#qFrame").src = `/quote/${encodeURIComponent(tid)}?embed=1`;
  $("#quoteModal").hidden = false;
}
function closeQuote() { $("#quoteModal").hidden = true; $("#qFrame").src = "about:blank"; }

// 화면 안 확인 창. input 을 주면 입력값(문자열), 아니면 true 를 돌려주고 취소하면 null.
function ask({ title, message = "", input = null, ok = "확인", danger = false }) {
  return new Promise(resolve => {
    const dlg = $("#dialog"), inp = $("#dInput");
    $("#dTitle").textContent = title;
    $("#dMsg").textContent = message;
    $("#dOk").textContent = ok;
    $("#dOk").className = `btn${danger ? " danger" : ""}`;
    $("#dForm").classList.toggle("danger", danger);
    inp.hidden = input === null; inp.value = input ?? "";
    dlg.hidden = false;
    (input === null ? $("#dOk") : inp).focus();
    const close = v => { dlg.hidden = true; $("#dForm").onsubmit = $("#dCancel").onclick = dlg.onclick = dlg.onkeydown = null; resolve(v); };
    $("#dForm").onsubmit = e => { e.preventDefault(); close(input === null ? true : inp.value.trim()); };
    $("#dCancel").onclick = () => close(null);
    dlg.onclick = e => { if (e.target === dlg) close(null); };
    dlg.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); close(null); } };
  });
}

let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.hidden = true, 3000);
}

async function init() {
  const [meta, samples, kg] = await Promise.all(["/api/meta", "/api/samples", "/api/graph"].map(u => fetch(u).then(r => r.json())));
  META = meta; KG = kg;
  drawStage();
  resetStage("문의를 입력하거나 예시 메일을 골라 보세요");
  chatSys("AE 조이, AD 레오, QC 미오, 실장님이 들어왔습니다.");
  renderKg(kgBase(), new Set());
  $("#llmBadge").textContent = meta.llm === "mock" ? "LLM 없이 규칙 모드" : `LLM · ${meta.llm}`;
  $("#holdHint").textContent = `${meta.hold_seconds}초 유예 후 발송`;
  $("#samples").innerHTML = samples.map(s =>
    `<li data-id="${esc(s.id)}"><b>${esc(s.client)}</b><span class="tag">${esc(s.id)}</span><small>${esc(s.text)}</small></li>`).join("");
  $("#samplesBox .stat").textContent = `${samples.length}건`;
  const titleOf = s => `${s.client} — ${s.text.slice(0, 34)}…`;
  $("#samples").onclick = e => {
    const li = e.target.closest("li[data-id]"); if (!li) return;
    const s = samples.find(x => x.id === li.dataset.id);
    runRequest({ request_id: s.id }, titleOf(s), s.client);
  };
  $("#runAll").onclick = async e => {
    e.preventDefault();                      // summary 안의 버튼이라 접힘 토글을 막는다
    $("#runAll").disabled = true;
    for (const s of samples) await runRequest({ request_id: s.id }, titleOf(s), s.client, { openApproval: false });
    $("#runAll").disabled = false;
    toast("모두 처리했어요. 결재함과 우편함을 확인해 주세요");
  };

  // 메일 수신함
  $("#mailFetch").onclick = fetchMail;
  $("#mailSet").onclick = openMailSettings;
  $("#mails").onclick = onMailClick;
  setInterval(() => { if (MAIL.auto_fetch) refreshLists(); }, 10000);

  // 수기 입력
  let fillIdx = 0;
  $("#cFill").onclick = () => {
    const s = samples[fillIdx++ % samples.length];
    $("#cClient").value = s.client; $("#cContact").value = ""; $("#cText").value = s.text;
    $("#cText").focus();
  };
  $("#cForm").onsubmit = async e => {
    e.preventDefault();
    const client = $("#cClient").value.trim(), contact = $("#cContact").value.trim(), text = $("#cText").value.trim();
    if (!client || !text) { toast("고객사와 문의 내용을 적어 주세요"); return; }
    $("#cRun").disabled = true;
    const run = runRequest({ client, contact, text }, `${client} — ${text.slice(0, 34)}…`, client);
    $("#cText").value = "";                  // 접수되면 내용은 비우고 고객사는 남긴다
    $("#cRun").disabled = false;
    await run;
  };
  $("#cText").addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#cForm").requestSubmit();
  });

  // 초기화
  $("#resetAll").onclick = async () => {
    const n = $("#done").querySelectorAll("li[data-tid]").length + Number($("#pendingCount").textContent) + Number($("#holdCount").textContent);
    const yes = await ask({
      title: "처리 기록 초기화",
      message: `처리한 견적 ${n}건(결재·유예 대기 포함)과 발송된 견적서 파일이 모두 지워지고\n처음 상태로 돌아갑니다. 되돌릴 수 없어요.`,
      ok: "초기화", danger: true,
    });
    if (!yes) return;
    try {
      const r = await api("/api/reset", {});
      stageTid = null;
      resetStage("문의를 입력하거나 예시 메일을 골라 보세요");
      chatSys(`처리 기록을 초기화했어요 (대화 ${r.threads}건 · 견적서 파일 ${r.files}개 삭제)`);
      renderKg(kgBase(), new Set()); $("#kgEvidence").innerHTML = ""; $("#kgSub").textContent = "디자이너가 단가를 찾으면 탐색 경로가 여기에 그려져요";
      refreshLists();
    } catch { /* api() 가 토스트로 알린다 */ }
  };
  $("#pending").onclick = e => { const li = e.target.closest("li[data-tid]"); if (li) openApprovalFor(li.dataset.tid); };
  $("#holding").onclick = e => { const b = e.target.closest("[data-hold]"); const li = e.target.closest("li[data-tid]"); if (b && li) holdAction(li.dataset.tid, b.dataset.hold); };
  $("#done").onclick = e => {
    const li = e.target.closest("li[data-tid]"); if (!li) return;
    if (li.querySelector(".tag.sent")) openQuote(li.dataset.tid, { client: li.querySelector("b").textContent });
    else toast(`${li.querySelector("b").textContent}: ${li.querySelector(".tag").textContent}`);
  };
  $("#mClose").onclick = () => $("#modal").hidden = true;
  $("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").hidden = true; };
  $("#mBody").onclick = e => { const b = e.target.closest("[data-act]"); if (b) decide(b.dataset.act); };
  // 견적서 뷰어
  $("#chat").addEventListener("click", e => {
    const a = e.target.closest("[data-quote]"); if (!a) return;
    e.preventDefault();
    const t = [...document.querySelectorAll("#done li[data-tid]")].find(li => li.dataset.tid === a.dataset.quote);
    openQuote(a.dataset.quote, { client: t?.querySelector("b").textContent || "" });
  });
  $("#qClose").onclick = closeQuote;
  $("#quoteModal").onclick = e => { if (e.target.id === "quoteModal") closeQuote(); };
  $("#qPrint").onclick = () => $("#qFrame").contentWindow?.print();
  // Esc: 가장 위의 창부터 닫는다 (견적서 뷰어 → 결재·설정 창)
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (!$("#quoteModal").hidden) closeQuote(); else $("#modal").hidden = true;
  });
  refreshLists();
}
init();
