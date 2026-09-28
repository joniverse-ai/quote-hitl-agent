"""메일 수신함: 견적 요청 메일을 가져와 목록으로 보관한다.

가져오는 곳 두 가지
  folder : data/inbox/*.eml 파일 (메일 계정 없이 시연·테스트용)
  imap   : 실제 메일 계정 (Gmail·네이버·다음·Outlook 등). 비밀번호는 .env 의 MAIL_PASSWORD 에서만 읽는다.

설정은 data/mail_settings.json, 가져온 메일 목록은 data/mails.json 에 둔다.
메일 한 통은 Message-ID 로 구분해서 두 번 가져오지 않는다.
"""
import email
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape

import config

SETTINGS = config.DATA / "mail_settings.json"
MAILS = config.DATA / "mails.json"
INBOX_DIR = config.DATA / "inbox"

PRESETS = {
    "gmail": {"label": "Gmail", "host": "imap.gmail.com", "port": 993,
              "help": "Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호를 만들어 MAIL_PASSWORD 에 넣으세요. Gmail 설정에서 IMAP 사용을 켜야 합니다."},
    "naver": {"label": "네이버", "host": "imap.naver.com", "port": 993,
              "help": "네이버 메일 → 환경설정 → POP3/IMAP 설정에서 IMAP 사용을 켜세요. 2단계 인증을 쓰면 애플리케이션 비밀번호가 필요합니다."},
    "daum": {"label": "다음/카카오", "host": "imap.daum.net", "port": 993,
             "help": "다음 메일 → 환경설정 → IMAP/POP3 에서 IMAP 사용을 켜세요."},
    "outlook": {"label": "Outlook", "host": "outlook.office365.com", "port": 993,
                "help": "조직 계정은 관리자가 IMAP 을 막아 둔 경우가 있습니다."},
    "custom": {"label": "직접 입력", "host": "", "port": 993, "help": "회사 메일 서버의 IMAP 주소와 포트를 입력하세요."},
}

DEFAULTS = {
    "source": "folder",          # folder | imap
    "preset": "gmail",
    "host": "imap.gmail.com",
    "port": 993,
    "username": "",
    "folder": "INBOX",
    "subject_filter": "견적",     # 제목이나 본문에 이 단어가 있는 메일만 (비우면 전부)
    "since_days": 7,
    "max_fetch": 30,
    "auto_fetch": False,          # N분마다 자동으로 가져오기
    "poll_minutes": 5,
    "auto_process": False,        # 새 메일을 바로 견적 처리
}


def load_settings():
    s = dict(DEFAULTS)
    if SETTINGS.exists():
        s.update(json.loads(SETTINGS.read_text()))
    return s


def public_settings():
    """화면에 돌려줄 설정. 비밀번호는 값 대신 설정 여부만."""
    return {**load_settings(), "password_set": bool(os.getenv("MAIL_PASSWORD")), "presets": PRESETS}


def save_settings(data):
    s = load_settings()
    for k in DEFAULTS:
        if k in data:
            s[k] = data[k]
    s["port"] = int(s["port"])
    s["since_days"] = max(1, int(s["since_days"]))
    s["poll_minutes"] = max(1, int(s["poll_minutes"]))
    s["max_fetch"] = max(1, min(200, int(s["max_fetch"])))
    config.DATA.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def load_mails():
    return json.loads(MAILS.read_text()) if MAILS.exists() else []


def save_mails(mails):
    MAILS.write_text(json.dumps(mails, ensure_ascii=False, indent=1))


# ── 메일 해석 ─────────────────────────────────────────
def _h(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body(msg):
    """본문 텍스트. text/plain 을 먼저, 없으면 html 에서 태그를 걷어낸다. 인용된 이전 메일은 자른다."""
    plain, html = None, None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain is None and html:
        plain = unescape(re.sub(r"<(br|/p|/div)[^>]*>", "\n", html, flags=re.I))
        plain = re.sub(r"<[^>]+>", "", plain)
    text = (plain or "").replace("\r\n", "\n")
    text = re.split(r"\n-{2,}\s*Original Message|\n\s*On .+ wrote:|\n\d{4}[.-]\s?\d{1,2}[.-].+작성:|\n>", text)[0]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(raw: bytes, source):
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_h(msg.get("From")))
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone().isoformat(timespec="minutes")
    except Exception:
        received = datetime.now().isoformat(timespec="minutes")
    mid = (msg.get("Message-ID") or "").strip() or hashlib.sha1(raw).hexdigest()
    return {
        "mail_id": "M" + hashlib.sha1(mid.encode()).hexdigest()[:8],
        "message_id": mid,
        "from_name": name or addr.split("@")[0],
        "from_addr": addr,
        "subject": _h(msg.get("Subject")) or "(제목 없음)",
        "received": received,
        "body": _body(msg)[:4000],
        "source": source,
        "thread_id": None,
    }


def _wanted(m, s):
    kw = (s.get("subject_filter") or "").strip()
    return not kw or kw in m["subject"] or kw in m["body"]


# ── 가져오기 ──────────────────────────────────────────
def _from_folder(s):
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    return [parse(p.read_bytes(), "folder") for p in sorted(INBOX_DIR.glob("*.eml"))]


def _from_imap(s):
    password = os.getenv("MAIL_PASSWORD")
    if not (s["host"] and s["username"] and password):
        raise ValueError("IMAP 주소·아이디를 설정하고 .env 에 MAIL_PASSWORD 를 넣어 주세요")
    since = (datetime.now() - timedelta(days=s["since_days"])).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(s["host"], s["port"], timeout=20) as box:
        box.login(s["username"], password)
        typ, _ = box.select(f'"{s["folder"]}"', readonly=True)   # 읽음 표시를 바꾸지 않는다
        if typ != "OK":
            raise ValueError(f"메일함 '{s['folder']}' 을 열 수 없어요")
        _, data = box.search(None, "SINCE", since)
        ids = data[0].split()[-s["max_fetch"]:]
        out = []
        for i in reversed(ids):
            _, parts = box.fetch(i, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append(parse(raw, "imap"))
        return out


def test_connection():
    s = load_settings()
    if s["source"] == "folder":
        n = len(list(INBOX_DIR.glob("*.eml"))) if INBOX_DIR.exists() else 0
        return {"ok": True, "message": f"테스트 폴더 연결됨 · .eml {n}개 ({INBOX_DIR.relative_to(config.ROOT)})"}
    try:
        msgs = _from_imap(s)
        return {"ok": True, "message": f"{s['username']} 연결 성공 · 최근 {s['since_days']}일 메일 {len(msgs)}통"}
    except Exception as e:
        return {"ok": False, "message": f"연결 실패: {e}"}


def fetch():
    """새 메일만 목록에 더한다. 반환: 새로 들어온 메일들."""
    s = load_settings()
    got = _from_folder(s) if s["source"] == "folder" else _from_imap(s)
    mails = load_mails()
    known = {m["message_id"] for m in mails}
    new = [m for m in got if m["message_id"] not in known and _wanted(m, s)]
    mails = sorted(mails + new, key=lambda m: m["received"], reverse=True)
    save_mails(mails)
    s["last_fetch"] = datetime.now().isoformat(timespec="seconds")
    SETTINGS.write_text(json.dumps({k: v for k, v in s.items()}, ensure_ascii=False, indent=1))
    return new


def to_request(m):
    """메일 → 파이프라인 요청. 제목도 요청 내용에 넣는다 (제목에만 품목이 있는 메일이 많다)."""
    # '하루베이커리 박소연' → 고객사 '하루베이커리', 담당자 '박소연'
    parts = m["from_name"].split()
    client, contact = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (m["from_name"], None)
    return {"id": m["mail_id"], "client": client, "contact": contact,
            "from_addr": m["from_addr"], "text": f"{m['subject']}\n{m['body']}".strip()}


def link(mail_id, thread_id):
    mails = load_mails()
    for m in mails:
        if m["mail_id"] == mail_id:
            m["thread_id"] = thread_id
    save_mails(mails)
