"""
Career Email Agent — multi-tenant API + dashboard

Each visitor sets up their own mailbox credentials (and optionally their own
Gemini key, resume, and summary) once, in the browser. That's remembered via
a cookie, no username/password login screen. Nothing about which inbox to
watch is baked into the server's .env anymore — the .env only holds an
optional fallback Gemini key for people who don't paste their own.

  - a `twin/` folder next to this file is NOT used anymore; each session gets
    its own `sessions/<id>/twin/resume.pdf` + `summary.txt`, uploaded via the
    dashboard's setup screen.
  - a `.env` file next to this file, now only needs (both optional):
        GOOGLE_API_KEY         <- fallback Gemini key if a user doesn't paste their own
        DAYS_TO_SCAN, POLL_INTERVAL_SECONDS

Run:
    pip install -r requirements.txt
    uvicorn app:app --port 8000
    # then open http://localhost:8000 in a browser

Deployment note: this process runs a background polling thread per active
session that must keep running between requests. That does NOT work on
serverless hosts (Vercel, Netlify Functions, AWS Lambda) — they spin the
process down between requests and kill background loops. Use a host that
keeps one process alive: Render, Railway, Fly.io, or a plain VPS. See the
included Dockerfile.
"""

import email as email_lib
import hashlib
import imaplib
import json
import os
import re
import secrets
import shutil
import smtplib
import threading
import time
import base64
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, make_msgid
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, UploadFile, Form, File
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import OpenAI
from sqlalchemy import create_engine, Column, String, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker
from cryptography.fernet import Fernet, InvalidToken
from pypdf import PdfReader
from docx import Document
from starlette.concurrency import run_in_threadpool

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Minimal production auth + database
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./career_agent.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
Base = declarative_base()
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

class User(Base):
    __tablename__ = "users"
    id = Column(String(64), primary_key=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    password_hash = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False)

class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash = Column(String(128), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)

Base.metadata.create_all(engine)

AUTH_COOKIE = "cea_auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 30
PASSWORD_ITERATIONS = 310000
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    try:
        FERNET = Fernet(ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise RuntimeError("ENCRYPTION_KEY must be a valid Fernet key. Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"") from exc
else:
    # Local development fallback. Production deployments MUST set ENCRYPTION_KEY.
    FERNET = Fernet(Fernet.generate_key())
    print("WARNING: ENCRYPTION_KEY is not set; encrypted credentials will not survive process restarts.")

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def encrypt_secret(value: str | None) -> str | None:
    return FERNET.encrypt(value.encode()).decode() if value else None

def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return FERNET.decrypt(value.encode()).decode()
    except InvalidToken:
        # Compatibility with old local sessions created before encryption.
        return value

def auth_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def current_user(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    db = SessionLocal()
    try:
        sess = db.get(AuthSession, auth_token_hash(token))
        if not sess:
            return None
        return db.get(User, sess.user_id)
    finally:
        db.close()

def require_user(request: Request, response: Response) -> tuple[User, str] | JSONResponse:
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Authentication required."}, status_code=401)

    # The authenticated database user ID is the only valid storage namespace.
    # Never trust a client-controlled session cookie to select another user's files.
    sid = user.id
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "0") == "1",
    )
    return user, sid


# ---------------------------------------------------------------------------
# Server-wide config (no per-user secrets live here anymore)
# ---------------------------------------------------------------------------
OWNER_GEMINI_KEY = os.getenv("GOOGLE_API_KEY")  # fallback only
DAYS_TO_SCAN = int(os.getenv("DAYS_TO_SCAN", "2"))
DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MAX_LOG_ENTRIES = 200
COOKIE_NAME = "cea_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

TRIGGER_KEYWORDS = [
    "job", "recruiter", "interview", "hiring", "position",
    "opportunity", "role", "vacancy", "career",
]

# NOTE: the OpenAI-compatible endpoint needs the bare model id
# ("gemini-3.1-flash-lite"), not the native resource path
# ("models/gemini-3.1-flash-lite") - that path format 404s against it.
MODEL_NAME = "gemini-3.1-flash-lite"


def make_gemini_client(gemini_key: Optional[str]) -> OpenAI:
    key = gemini_key or OWNER_GEMINI_KEY or "not-configured"
    return OpenAI(
        api_key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )


send_reply_tool_json = {
    "name": "send_reply_tool",
    "description": "Use this when you can write a confident, accurate, personalized reply to this email using only the given context.",
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The full reply email body, written in first person, professional in tone.",
            },
            "attach_resume": {
                "type": "boolean",
                "description": "True if it would help to attach the resume to this reply (e.g. the sender is asking about background/candidacy).",
            },
        },
        "required": ["reply_text", "attach_resume"],
        "additionalProperties": False,
    },
}

notify_me_tool_json = {
    "name": "notify_me_tool",
    "description": "Use this instead of replying when the email asks something that cannot be answered confidently using only the given context. This notifies the account owner so they can reply personally. Do not send an email reply in this case.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "A short explanation of what information is missing or why you can't answer confidently.",
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

tools = [
    {"type": "function", "function": send_reply_tool_json},
    {"type": "function", "function": notify_me_tool_json},
]


# ---------------------------------------------------------------------------
# Per-session persistent config (credentials + profile), stored on disk
# ---------------------------------------------------------------------------
def session_dir(sid: str) -> Path:
    return SESSIONS_DIR / sid


def profile_archive_dir(sid: str) -> Path:
    # Keep archived inbox profiles outside the active workspace so the
    # workspace can be cleared safely without deleting the saved profiles.
    return SESSIONS_DIR / f"{sid}_profiles"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def profile_key(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()[:32]


def archived_profile_dir(sid: str, email: str) -> Path:
    return profile_archive_dir(sid) / profile_key(email)


def config_path(sid: str) -> Path:
    return session_dir(sid) / "config.json"


def twin_dir(sid: str) -> Path:
    return session_dir(sid) / "twin"


def resume_path(sid: str) -> Path:
    return twin_dir(sid) / "resume.pdf"


def summary_path(sid: str) -> Path:
    return twin_dir(sid) / "summary.txt"


def source_document_path(sid: str) -> Path:
    return twin_dir(sid) / "source_document"


def source_document_text_path(sid: str) -> Path:
    return twin_dir(sid) / "source_document.txt"


def state_file(sid: str) -> Path:
    return session_dir(sid) / "handled_message_ids.json"


def log_file(sid: str) -> Path:
    return session_dir(sid) / "agent_activity_log.json"


def is_configured(sid: str) -> bool:
    return config_path(sid).exists()


def load_config(sid: str) -> Optional[dict]:
    p = config_path(sid)
    if not p.exists():
        return None
    cfg = json.loads(p.read_text())
    for key in ("app_password", "gemini_key", "pushover_user", "pushover_token"):
        if cfg.get(key):
            cfg[key] = decrypt_secret(cfg[key])
    return cfg


def save_config(sid: str, cfg: dict) -> None:
    session_dir(sid).mkdir(parents=True, exist_ok=True)
    twin_dir(sid).mkdir(parents=True, exist_ok=True)
    safe_cfg = dict(cfg)
    for key in ("app_password", "gemini_key", "pushover_user", "pushover_token"):
        if safe_cfg.get(key):
            safe_cfg[key] = encrypt_secret(safe_cfg[key])
    config_path(sid).write_text(json.dumps(safe_cfg, indent=2))


# ---------------------------------------------------------------------------
# Per-session runtime state (in-memory - lost on restart, rebuilt lazily)
# ---------------------------------------------------------------------------
@dataclass
class SessionRuntime:
    dry_run: bool = True
    polling: bool = False
    poll_interval: int = DEFAULT_POLL_INTERVAL
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    logs: list = field(default_factory=list)
    handled_ids: Optional[set] = None
    resume_text: Optional[str] = None
    summary_text: Optional[str] = None
    source_document_text: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    process_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


_registry_lock = threading.Lock()
RUNTIME: dict[str, SessionRuntime] = {}


def get_runtime(sid: str) -> SessionRuntime:
    with _registry_lock:
        rt = RUNTIME.get(sid)
        if rt is None:
            rt = SessionRuntime()
            if log_file(sid).exists():
                try:
                    rt.logs = json.loads(log_file(sid).read_text())
                except json.JSONDecodeError:
                    rt.logs = []
            RUNTIME[sid] = rt
        return rt


def get_or_create_sid(request: Request, response: Response) -> str:
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        sid = secrets.token_urlsafe(24)
    response.set_cookie(
        COOKIE_NAME, sid, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1"
    )
    return sid


# ---------------------------------------------------------------------------
# Personal context (resume + summary), loaded/cached per session
# ---------------------------------------------------------------------------
def load_resume_text(sid: str) -> str:
    p = resume_path(sid)
    if not p.exists():
        return ""
    reader = PdfReader(str(p))
    text = ""
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text
    return text


def load_summary_text(sid: str) -> str:
    p = summary_path(sid)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def load_source_document_text(sid: str) -> str:
    p = source_document_text_path(sid)
    if p.exists():
        return p.read_text(encoding="utf-8")

    raw_files = list(twin_dir(sid).glob("source_document.*"))
    if not raw_files:
        return ""
    raw = raw_files[0]

    if raw.suffix.lower() == ".pdf":
        reader = PdfReader(str(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if raw.suffix.lower() == ".docx":
        doc = Document(str(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return raw.read_text(encoding="utf-8", errors="replace")


def invalidate_twin_cache(sid: str) -> None:
    rt = get_runtime(sid)
    with rt.lock:
        rt.resume_text = None
        rt.summary_text = None
        rt.source_document_text = None


def build_system_prompt(name: str, resume_text: str, summary: str, source_document_text: str) -> str:
    who = name.strip() if name and name.strip() else "the account holder"
    return f"""
# Your role

You are {who}'s personal email-reply assistant. {who} receives emails
related to job opportunities, recruiters, and their career. You will be shown
ONE such email. Your job is to decide between exactly two actions, using
ONLY the information given below - nothing else.

1. Call `send_reply_tool` if, and only if, you can write an accurate, specific,
   and genuinely helpful reply using ONLY the information given below. Write the
   reply in first person, as if you are {who} yourself, in a professional tone.

2. Call `notify_me_tool` if the email asks for anything - a fact, a date, a
   preference, availability, salary expectations, anything - that is not
   explicitly present in the information below. Do NOT guess, infer, or make up
   anything that isn't there. It is much better to notify {who} than to send
   an inaccurate or generic reply.

Always call exactly one of these two tools. Never reply in plain text without
calling a tool.

# Summary of {who}

{summary}

# {who}'s Resume

{resume_text}

# Additional document provided by {who}

{source_document_text}
"""


def get_agent_context(sid: str, cfg: dict):
    """Returns (resume_text, summary_text, system_prompt, client), cached
    per-session until a twin upload invalidates the cache."""
    rt = get_runtime(sid)
    with rt.lock:
        if rt.resume_text is None:
            rt.resume_text = load_resume_text(sid)
        if rt.summary_text is None:
            rt.summary_text = load_summary_text(sid)
        if rt.source_document_text is None:
            rt.source_document_text = load_source_document_text(sid)
        resume_text, summary_text, source_document_text = rt.resume_text, rt.summary_text, rt.source_document_text
    system_prompt = build_system_prompt(cfg.get("your_name", ""), resume_text, summary_text, source_document_text)
    client = make_gemini_client(cfg.get("gemini_key"))
    return resume_text, summary_text, system_prompt, client


# ---------------------------------------------------------------------------
# Logging (per session)
# ---------------------------------------------------------------------------
def log_event(sid: str, **entry):
    rt = get_runtime(sid)
    entry["timestamp"] = datetime.now().isoformat(timespec="seconds")
    rt.logs.append(entry)
    del rt.logs[: -MAX_LOG_ENTRIES]
    log_file(sid).write_text(json.dumps(rt.logs[-MAX_LOG_ENTRIES:], indent=2))


# ---------------------------------------------------------------------------
# Tool handlers (all parameterized by session + config now)
# ---------------------------------------------------------------------------
def contains_any_keyword(text: str, keywords: list) -> bool:
    text_lower = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text_lower) for kw in keywords)


def get_email_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        return payload.decode(charset, errors="replace") if payload else ""


def load_handled_ids(sid: str) -> set:
    p = state_file(sid)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def save_handled_ids(sid: str, ids: set) -> None:
    state_file(sid).write_text(json.dumps(sorted(ids)))


def push(sid: str, cfg: dict, rt: SessionRuntime, message: str):
    user, token = cfg.get("pushover_user"), cfg.get("pushover_token")
    print(f"Push [{sid}]: {message}")
    if rt.dry_run:
        print("[DRY RUN] Not actually sending push.")
        return
    if not user or not token:
        print("No Pushover credentials configured for this session - skipped push.")
        return
    requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"user": user, "token": token, "message": message},
    )


def handle_send_reply(sid: str, cfg: dict, rt: SessionRuntime, original_msg, reply_text: str, attach_resume: bool) -> str:
    _, sender_addr = parseaddr(original_msg.get("Reply-To") or original_msg.get("From"))
    subject = original_msg.get("Subject", "") or ""

    if not sender_addr:
        log_event(sid, type="error", subject=subject, sender="", detail="No valid reply-to address found - skipped")
        return "Skipped - no valid reply-to address found"

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    reply = EmailMessage()
    reply["From"] = cfg["email"]
    reply["To"] = sender_addr
    reply["Subject"] = subject
    reply["In-Reply-To"] = original_msg.get("Message-ID", "")
    references = original_msg.get("References", "")
    reply["References"] = f"{references} {original_msg.get('Message-ID', '')}".strip()
    reply["Message-ID"] = make_msgid()
    reply.set_content(reply_text)

    rpath = resume_path(sid)
    if attach_resume:
        if rpath.exists():
            reply.add_attachment(rpath.read_bytes(), maintype="application", subtype="pdf", filename=rpath.name)
        else:
            attach_resume = False

    if rt.dry_run:
        log_event(sid, type="replied", subject=subject, sender=sender_addr, detail=reply_text,
                   attach_resume=attach_resume, dry_run=True)
        return "Reply drafted (dry run - not actually sent)"

    with smtplib.SMTP(cfg["smtp_server"], 587) as server:
        server.starttls()
        server.login(cfg["email"], cfg["app_password"])
        server.send_message(reply)

    log_event(sid, type="replied", subject=subject, sender=sender_addr, detail=reply_text,
               attach_resume=attach_resume, dry_run=False)
    return "Reply sent successfully"


def handle_notify(sid: str, cfg: dict, rt: SessionRuntime, original_msg, reason: str) -> str:
    sender = original_msg.get("From", "unknown sender")
    subject = original_msg.get("Subject", "") or "(no subject)"
    push(sid, cfg, rt, f"Career email needs your reply.\nFrom: {sender}\nSubject: {subject}\nWhy I couldn't answer: {reason}")
    log_event(sid, type="notified", subject=subject, sender=sender, detail=reason, dry_run=rt.dry_run)
    return "Notification sent"


# ---------------------------------------------------------------------------
# Agent loop (manual tool-calling loop, tool_choice="required")
# ---------------------------------------------------------------------------
def run_agent_for_email(sid: str, cfg: dict, rt: SessionRuntime, original_msg) -> str:
    _, _, system_prompt, client = get_agent_context(sid, cfg)

    sender = original_msg.get("From", "")
    subject = original_msg.get("Subject", "") or ""
    body = get_email_body(original_msg)
    user_content = f"From: {sender}\nSubject: {subject}\n\n{body}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = client.chat.completions.create(
        model=MODEL_NAME, messages=messages, tools=tools, tool_choice="required"
    )

    while response.choices[0].finish_reason == "tool_calls":
        message = response.choices[0].message
        messages.append(message)

        for tool_call in message.tool_calls:
            args = json.loads(tool_call.function.arguments)

            if tool_call.function.name == "send_reply_tool":
                result = handle_send_reply(sid, cfg, rt, original_msg, args["reply_text"], args["attach_resume"])
            elif tool_call.function.name == "notify_me_tool":
                result = handle_notify(sid, cfg, rt, original_msg, args["reason"])
            else:
                result = f"Unknown tool: {tool_call.function.name}"

            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})

        response = client.chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=tools, tool_choice="auto"
        )

    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Reading the inbox (IMAP)
# ---------------------------------------------------------------------------
def fetch_recent_emails(cfg: dict, days: int = DAYS_TO_SCAN):
    imap = imaplib.IMAP4_SSL(cfg["imap_server"])
    imap.login(cfg["email"], cfg["app_password"])
    imap.select("INBOX")

    since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    status, data = imap.search(None, f'(SINCE "{since_date}")')
    if status != "OK":
        imap.logout()
        return []

    messages = []
    for num in data[0].split():
        status, msg_data = imap.fetch(num, "(FLAGS BODY.PEEK[])")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        flags_meta = msg_data[0][0] or b""
        is_seen = b"\\Seen" in flags_meta
        raw_email = msg_data[0][1]
        parsed = email_lib.message_from_bytes(raw_email, policy=policy.default)
        messages.append((num, parsed, is_seen))

    imap.logout()
    return messages


def stable_message_key(msg, imap_num) -> str:
    msg_id = msg.get("Message-ID", "")
    if msg_id:
        return msg_id
    basis = f"{msg.get('From', '')}|{msg.get('Subject', '')}|{msg.get('Date', '')}"
    return "fallback-" + hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Putting it all together (per session)
# ---------------------------------------------------------------------------
def process_inbox(sid: str):
    cfg = load_config(sid)
    if not cfg:
        return {"checked": 0, "new": 0, "error": "not_configured"}
    rt = get_runtime(sid)

    if not rt.process_lock.acquire(blocking=False):
        return {"checked": 0, "new": 0, "skipped_concurrent_run": True}

    try:
        handled_ids = load_handled_ids(sid)
        recent = fetch_recent_emails(cfg)
        new_count = 0

        for num, msg, is_seen in recent:
            msg_id = stable_message_key(msg, num)
            subject = msg.get("Subject", "") or ""
            sender_addr = parseaddr(msg.get("From"))[1]

            try:
                if msg_id in handled_ids:
                    continue

                if sender_addr.lower() == (cfg["email"] or "").lower():
                    handled_ids.add(msg_id)
                    continue
                if re.search(r"no-?reply|mailer-daemon|postmaster", sender_addr, re.IGNORECASE):
                    handled_ids.add(msg_id)
                    continue

                body = get_email_body(msg)
                full_text = f"{subject}\n{body}"
                new_count += 1

                if not contains_any_keyword(full_text, TRIGGER_KEYWORDS):
                    log_event(sid, type="skipped", subject=subject, sender=sender_addr, detail="Not career-related")
                    handled_ids.add(msg_id)
                    continue

                if is_seen:
                    log_event(sid, type="skipped", subject=subject, sender=sender_addr,
                               detail="Already read - leaving it for you to handle personally")
                    handled_ids.add(msg_id)
                    continue

                run_agent_for_email(sid, cfg, rt, msg)
                handled_ids.add(msg_id)

            except Exception as exc:
                log_event(sid, type="error", subject=subject, sender=sender_addr, detail=str(exc))

            finally:
                save_handled_ids(sid, handled_ids)

        return {"checked": len(recent), "new": new_count}

    finally:
        rt.process_lock.release()


def poll_loop(sid: str):
    rt = get_runtime(sid)
    while not rt.stop_event.is_set():
        try:
            process_inbox(sid)
            rt.last_run_at = datetime.now().isoformat(timespec="seconds")
            rt.last_error = None
        except Exception as exc:
            rt.last_error = str(exc)
            log_event(sid, type="error", subject="", sender="", detail=str(exc))
        rt.stop_event.wait(rt.poll_interval)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="Career Email Agent")


class SetupBody(BaseModel):
    email: str
    app_password: str
    smtp_server: str = "smtp.gmail.com"
    imap_server: str = "imap.gmail.com"
    gemini_key: Optional[str] = None
    your_name: Optional[str] = None
    pushover_user: Optional[str] = None
    pushover_token: Optional[str] = None


class DryRunBody(BaseModel):
    enabled: bool


class SummaryBody(BaseModel):
    text: str


@app.post("/api/auth/signup")
def signup(body: dict, response: Response):
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return JSONResponse({"ok": False, "error": "Enter a valid email address."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"ok": False, "error": "Password must be at least 8 characters."}, status_code=400)
    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=email).first():
            return JSONResponse({"ok": False, "error": "An account with that email already exists."}, status_code=409)
        user = User(id=secrets.token_hex(16), email=email, password_hash=hash_password(password), created_at=datetime.utcnow())
        db.add(user); db.commit()
        token = secrets.token_urlsafe(32)
        db.add(AuthSession(token_hash=auth_token_hash(token), user_id=user.id, created_at=datetime.utcnow())); db.commit()
        sid = user.id
        response.set_cookie(AUTH_COOKIE, token, max_age=AUTH_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
        response.set_cookie(COOKIE_NAME, sid, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
        return {"ok": True}
    finally:
        db.close()

@app.post("/api/auth/login")
def login(body: dict, response: Response):
    email = str(body.get("email", "")).strip().lower(); password = str(body.get("password", ""))
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=email).first()
        if not user or not verify_password(password, user.password_hash):
            return JSONResponse({"ok": False, "error": "Invalid email or password."}, status_code=401)
        token = secrets.token_urlsafe(32); sid = user.id
        db.add(AuthSession(token_hash=auth_token_hash(token), user_id=user.id, created_at=datetime.utcnow())); db.commit()
        response.set_cookie(AUTH_COOKIE, token, max_age=AUTH_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
        response.set_cookie(COOKIE_NAME, sid, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
        return {"ok": True}
    finally:
        db.close()

@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        db = SessionLocal()
        try:
            sess = db.get(AuthSession, auth_token_hash(token))
            if sess:
                db.delete(sess); db.commit()
        finally:
            db.close()
    response.delete_cookie(AUTH_COOKIE); response.delete_cookie(COOKIE_NAME)
    return {"ok": True}

@app.get("/api/auth/status")
def auth_status(request: Request):
    user = current_user(request)
    return {"authenticated": bool(user), "email": user.email if user else None}

@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse):
        return auth
    _, sid = auth

    email = normalize_email(body.email)
    if not email:
        return JSONResponse({"ok": False, "error": "Email address is required."}, status_code=400)

    # Test credentials against IMAP immediately, so a typo shows an error
    # right away instead of silently failing on the first poll.
    try:
        imap = imaplib.IMAP4_SSL(body.imap_server)
        imap.login(email, body.app_password)
        imap.logout()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not log in with those credentials: {exc}"}, status_code=400)

    archive = archived_profile_dir(sid, email)
    active_cfg = load_config(sid)
    active_email = normalize_email(active_cfg.get("email", "")) if active_cfg else ""

    # When switching inboxes, preserve the existing profile instead of deleting it.
    # This lets the same email come back later without re-uploading its files.
    if active_cfg and active_email and active_email != email and is_configured(sid):
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            shutil.rmtree(archive, ignore_errors=True)
        archive.mkdir(parents=True, exist_ok=True)
        for item in session_dir(sid).iterdir():
            if item.name == "profiles":
                continue
            shutil.move(str(item), str(archive / item.name))
        invalidate_twin_cache(sid)

    # Restore a previously saved profile for this exact inbox email.
    # Existing active data is replaced only after the credentials were verified.
    if archive.exists():
        for item in archive.iterdir():
            dest = session_dir(sid) / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            shutil.move(str(item), str(dest))
        try:
            archive.rmdir()
        except OSError:
            pass

    existing_cfg = load_config(sid)
    existing_same_email = existing_cfg and normalize_email(existing_cfg.get("email", "")) == email

    cfg = {
        "email": email,
        "app_password": body.app_password,
        "smtp_server": body.smtp_server,
        "imap_server": body.imap_server,
        # Blank means: use the shared .env fallback. If this email already had
        # an explicit key and the user leaves it blank, keep the existing key.
        "gemini_key": body.gemini_key or (existing_cfg.get("gemini_key") if existing_same_email and existing_cfg else None),
        "your_name": body.your_name or (existing_cfg.get("your_name", "") if existing_same_email and existing_cfg else ""),
        "pushover_user": body.pushover_user or (existing_cfg.get("pushover_user") if existing_same_email and existing_cfg else None),
        "pushover_token": body.pushover_token or (existing_cfg.get("pushover_token") if existing_same_email and existing_cfg else None),
    }
    save_config(sid, cfg)
    invalidate_twin_cache(sid)

    profile_complete = (
        resume_path(sid).exists()
        and summary_path(sid).exists()
        and bool(summary_path(sid).read_text(encoding="utf-8").strip())
        and source_document_text_path(sid).exists()
    )
    return {"ok": True, "profile_complete": profile_complete}


@app.post("/api/setup/resume")
async def upload_resume(request: Request, response: Response, file: UploadFile = File(...)):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Set up your account before uploading a resume."}, status_code=400)
    twin_dir(sid).mkdir(parents=True, exist_ok=True)
    resume_path(sid).write_bytes(await file.read())
    invalidate_twin_cache(sid)
    return {"ok": True}


@app.post("/api/setup/summary")
def upload_summary(body: SummaryBody, request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Set up your account before adding a summary."}, status_code=400)
    twin_dir(sid).mkdir(parents=True, exist_ok=True)
    summary_path(sid).write_text(body.text, encoding="utf-8")
    invalidate_twin_cache(sid)
    return {"ok": True}


@app.post("/api/setup/source-document")
async def upload_source_document(request: Request, response: Response, file: UploadFile = File(...)):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Set up your account before uploading a document."}, status_code=400)

    allowed = {".pdf", ".docx", ".txt", ".md"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        return JSONResponse({"ok": False, "error": "Upload a PDF, DOCX, TXT, or MD file."}, status_code=400)

    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "File is too large. Maximum size is 8 MB."}, status_code=400)
    if not data:
        return JSONResponse({"ok": False, "error": "The uploaded document is empty."}, status_code=400)

    twin_dir(sid).mkdir(parents=True, exist_ok=True)
    for old in twin_dir(sid).glob("source_document.*"):
        old.unlink(missing_ok=True)

    raw = source_document_path(sid).with_suffix(suffix)
    raw.write_bytes(data)

    try:
        extracted = load_source_document_text(sid)
    except Exception as exc:
        raw.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": f"Could not extract text: {exc}"}, status_code=400)

    source_document_text_path(sid).write_text(extracted, encoding="utf-8")
    invalidate_twin_cache(sid)
    return {"ok": True, "filename": file.filename, "characters_extracted": len(extracted)}


@app.post("/api/change-account")
def change_account(request: Request, response: Response):
    """Switch inboxes without destroying their saved profiles.

    The current inbox profile is archived by inbox email. Selecting that same
    email again restores its resume, summary, supporting document, and config.
    """
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse):
        return auth
    _, sid = auth

    cfg = load_config(sid)
    if cfg and cfg.get("email"):
        archive = archived_profile_dir(sid, cfg["email"])
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            shutil.rmtree(archive, ignore_errors=True)
        archive.mkdir(parents=True, exist_ok=True)
        rt = RUNTIME.get(sid)
        if rt:
            rt.stop_event.set()
            rt.polling = False
        for item in session_dir(sid).iterdir():
            if item.name == "profiles":
                continue
            shutil.move(str(item), str(archive / item.name))

    # Keep the profile archive; clear only the active workspace.
    shutil.rmtree(session_dir(sid), ignore_errors=True)
    session_dir(sid).mkdir(parents=True, exist_ok=True)
    profile_archive_dir(sid).mkdir(parents=True, exist_ok=True)
    with _registry_lock:
        RUNTIME.pop(sid, None)

    response.set_cookie(COOKIE_NAME, sid, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
    return {"ok": True}


@app.get("/api/status")
def get_status(request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return {"configured": False, "authenticated": False}
    _, sid = auth
    if not is_configured(sid):
        return {"configured": False, "authenticated": True}

    cfg = load_config(sid)
    rt = get_runtime(sid)
    return {
        "configured": True,
        "dry_run": rt.dry_run,
        "polling": rt.polling,
        "poll_interval": rt.poll_interval,
        "last_run_at": rt.last_run_at,
        "last_error": rt.last_error,
        "email_address": cfg["email"],
        "days_to_scan": DAYS_TO_SCAN,
        "resume_found": resume_path(sid).exists(),
        "summary_found": summary_path(sid).exists(),
        "source_document_found": source_document_text_path(sid).exists(),
        "profile_complete": (
            resume_path(sid).exists()
            and summary_path(sid).exists()
            and bool(summary_path(sid).read_text(encoding="utf-8").strip())
            and source_document_text_path(sid).exists()
        ),
        "gemini_key_found": bool(cfg.get("gemini_key") or OWNER_GEMINI_KEY),
        "using_own_gemini_key": bool(cfg.get("gemini_key")),
        "pushover_configured": bool(cfg.get("pushover_user") and cfg.get("pushover_token")),
        "handled_count": len(load_handled_ids(sid)),
    }


@app.post("/api/dry-run")
def set_dry_run(body: DryRunBody, request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    rt = get_runtime(sid)
    rt.dry_run = body.enabled
    return {"dry_run": rt.dry_run}


@app.post("/api/run-once")
async def run_once(request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    rt = get_runtime(sid)
    try:
        result = await run_in_threadpool(process_inbox, sid)
        rt.last_run_at = datetime.now().isoformat(timespec="seconds")
        rt.last_error = None
        return {"ok": True, **result}
    except Exception as exc:
        rt.last_error = str(exc)
        return {"ok": False, "error": str(exc)}


@app.post("/api/polling/start")
def start_polling(request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    if not is_configured(sid):
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=400)
    rt = get_runtime(sid)
    with _registry_lock:
        if rt.polling:
            return {"polling": True}
        rt.stop_event.clear()
        rt.thread = threading.Thread(target=poll_loop, args=(sid,), daemon=True)
        rt.thread.start()
        rt.polling = True
    return {"polling": True}


@app.post("/api/polling/stop")
def stop_polling(request: Request, response: Response):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    rt = get_runtime(sid)
    rt.stop_event.set()
    rt.polling = False
    return {"polling": False}


@app.get("/api/logs")
def get_logs(request: Request, response: Response, limit: int = 50):
    auth = require_user(request, response)
    if isinstance(auth, JSONResponse): return auth
    _, sid = auth
    rt = get_runtime(sid)
    return list(reversed(rt.logs[-limit:]))


@app.get("/health")
def health():
    return {"ok": True}


# Serve the dashboard. Registered last so it doesn't shadow the /api routes above.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
