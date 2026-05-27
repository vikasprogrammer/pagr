#!/usr/bin/env python3
"""pagr -- central monitor for Claude Code agents across machines.

Per-machine hook scripts POST lifecycle events here. We keep the latest state
per (machine, session) in SQLite, show a live dashboard (Machine / Title /
Folder / Summary / Status), and push a Telegram message when an agent needs
input or has been left waiting.

Entrypoint works three ways so it fits whatever the host runs:
  uvicorn app:app --host 0.0.0.0 --port $PORT
  gunicorn -k uvicorn.workers.UvicornWorker app:app
  python app.py
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               Response, StreamingResponse)


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    InstaPods' `env set` writes vars to <app>/.env but the systemd unit doesn't
    source it, so we load it ourselves. Real environment vars (e.g. PORT, HOST
    injected by the platform) always win via setdefault.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(".env")  # cwd is the app dir under systemd (WorkingDirectory)
_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# --------------------------------------------------------------------------- #
# Config (all via env)                                                         #
# --------------------------------------------------------------------------- #
TOKEN = os.environ.get("PAGR_TOKEN", "")
DB_PATH = os.environ.get("PAGR_DB", "./pagr.db")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PUBLIC_URL = os.environ.get("PAGR_PUBLIC_URL", "")  # deep-link in Telegram msg
NOTIFY_ON_STOP = os.environ.get("NOTIFY_ON_STOP", "") not in ("", "0", "false", "False")
NOTIFY_DEDUPE_SECONDS = int(os.environ.get("PAGR_DEDUPE_SECONDS", "30"))
# "Clear done" also removes working sessions untouched for this long (likely dead).
CLEAR_WORKING_AFTER = int(os.environ.get("PAGR_CLEAR_WORKING_AFTER", "3600"))

# event -> (status, push-by-default?)
EVENT_STATUS: dict[str, tuple[str, bool]] = {
    "start":       ("working",     False),
    "prompt":      ("working",     False),
    "stop":        ("idle",        False),  # fires every turn -> no push by default
    "needs_input": ("needs_input", True),
    "idle":        ("waiting",     True),
    "end":         ("ended",       False),
}

_LOCK = threading.Lock()
_conn: sqlite3.Connection
_subscribers: set[asyncio.Queue] = set()


# --------------------------------------------------------------------------- #
# Storage                                                                      #
# --------------------------------------------------------------------------- #
def _init_db() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            machine               TEXT NOT NULL,
            session_id            TEXT NOT NULL,
            cwd                   TEXT,
            branch                TEXT,
            status                TEXT,
            title                 TEXT,
            summary               TEXT,
            name                  TEXT,
            started_at            REAL,
            updated_at            REAL,
            last_notified_status  TEXT,
            last_notified_at      REAL,
            PRIMARY KEY (machine, session_id)
        )
        """
    )
    # migrate DBs created before a column existed
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    for col in ("title", "name"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
    # registry of enrolled machines — persists across session clears
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS machines (
            name        TEXT PRIMARY KEY,
            enrolled_at REAL,
            first_seen  REAL,
            last_seen   REAL
        )
        """
    )
    # backfill from any machines already present in the sessions table
    conn.execute(
        "INSERT OR IGNORE INTO machines (name, first_seen, last_seen) "
        "SELECT machine, MIN(started_at), MAX(updated_at) FROM sessions GROUP BY machine"
    )
    # runtime key/value config (e.g. Telegram bot token set via the Settings page)
    conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def _upsert(machine: str, session_id: str, event: str, cwd: str, branch: str,
            title: str, summary: str, message: str) -> dict[str, Any]:
    status, _ = EVENT_STATUS.get(event, ("working", False))
    now = time.time()
    text = summary or message or ""
    with _LOCK:
        row = _conn.execute(
            "SELECT * FROM sessions WHERE machine=? AND session_id=?",
            (machine, session_id),
        ).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO sessions (machine, session_id, cwd, branch, status, "
                "title, summary, name, started_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (machine, session_id, cwd, branch, status,
                 title, text, _mk_name(text), now, now),
            )
        else:
            # title tracks Claude's latest aiTitle; name is frozen from 1st prompt
            new_title = title or row["title"]
            new_name = row["name"] or _mk_name(text)
            _conn.execute(
                "UPDATE sessions SET cwd=?, branch=?, status=?, title=?, summary=?, "
                "name=?, updated_at=? WHERE machine=? AND session_id=?",
                (cwd or row["cwd"], branch or row["branch"], status, new_title,
                 text or row["summary"], new_name, now, machine, session_id),
            )
        _conn.commit()
        out = _conn.execute(
            "SELECT * FROM sessions WHERE machine=? AND session_id=?",
            (machine, session_id),
        ).fetchone()
    return dict(out)


def _mark_notified(machine: str, session_id: str, status: str) -> None:
    with _LOCK:
        _conn.execute(
            "UPDATE sessions SET last_notified_status=?, last_notified_at=? "
            "WHERE machine=? AND session_id=?",
            (status, time.time(), machine, session_id),
        )
        _conn.commit()


def _touch_machine(name: str, *, enrolled: bool = False) -> None:
    """Record a machine in the registry — on enrollment and on every event."""
    now = time.time()
    with _LOCK:
        row = _conn.execute("SELECT * FROM machines WHERE name=?", (name,)).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO machines (name, enrolled_at, first_seen, last_seen) VALUES (?,?,?,?)",
                (name, now if enrolled else None,
                 None if enrolled else now, None if enrolled else now),
            )
        else:
            if enrolled and not row["enrolled_at"]:
                _conn.execute("UPDATE machines SET enrolled_at=? WHERE name=?", (now, name))
            if not enrolled:
                _conn.execute(
                    "UPDATE machines SET first_seen=COALESCE(first_seen, ?), last_seen=? WHERE name=?",
                    (now, now, name),
                )
        _conn.commit()


def _should_notify(session: dict[str, Any], event: str) -> bool:
    status, base = EVENT_STATUS.get(event, ("working", False))
    if not (base or (event == "stop" and _notify_on_stop())):
        return False
    last_status = session.get("last_notified_status")
    last_at = session.get("last_notified_at") or 0
    if last_status == status and (time.time() - last_at) < NOTIFY_DEDUPE_SECONDS:
        return False
    return True


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def short_path(p: str) -> str:
    if not p:
        return ""
    parts = p.rstrip("/").split("/")
    return ".../" + "/".join(parts[-2:]) if len(parts) > 3 else p


def _mk_name(text: str) -> str:
    """A short, stable label for a session, frozen from its first prompt."""
    return " ".join((text or "").split())[:48]


def _cfg_get(key: str, default: str = "") -> str:
    with _LOCK:
        row = _conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def _cfg_set(key: str, value: str) -> None:
    with _LOCK:
        _conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _conn.commit()


def _tg_token() -> str:
    return _cfg_get("telegram_bot_token") or TELEGRAM_BOT_TOKEN


def _tg_chat() -> str:
    return _cfg_get("telegram_chat_id") or TELEGRAM_CHAT_ID


def _notify_on_stop() -> bool:
    v = _cfg_get("notify_on_stop")
    return NOTIFY_ON_STOP if v == "" else v not in ("0", "false", "False")


async def _broadcast(payload: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            _subscribers.discard(q)


async def _telegram_api(token: str, chat: str, text: str,
                        reply_markup: Optional[dict] = None) -> tuple[bool, str]:
    """Send a message; return (ok, error_description)."""
    payload: dict[str, Any] = {
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        data = r.json()
    return bool(data.get("ok")), str(data.get("description", ""))


async def _send_telegram(text: str, reply_markup: Optional[dict] = None) -> None:
    """Fire-and-forget notification using the runtime-configured bot."""
    token, chat = _tg_token(), _tg_chat()
    if not (token and chat):
        return
    try:
        await _telegram_api(token, chat, text, reply_markup)
    except Exception as exc:  # never let Telegram break ingestion
        print(f"[pagr] telegram error: {exc}", flush=True)


def _check_auth(authorization: Optional[str] = Header(None),
                key: Optional[str] = Query(None)) -> None:
    if not TOKEN:
        return  # auth disabled (local/dev)
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif key:
        supplied = key
    if supplied != TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conn
    _conn = _init_db()
    print(f"[pagr] up. db={DB_PATH} telegram={'on' if _tg_token() else 'off'} "
          f"auth={'on' if TOKEN else 'off'}", flush=True)
    yield
    _conn.close()


app = FastAPI(title="pagr", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "pagr"}


@app.post("/api/event")
async def api_event(request: Request, _auth: None = Depends(_check_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    machine = (body.get("machine") or "unknown").strip()
    session_id = (body.get("session_id") or "unknown").strip()
    event = (body.get("event") or "prompt").strip()
    cwd = (body.get("cwd") or "").strip()
    branch = (body.get("branch") or "").strip()
    title = (body.get("title") or "").strip()
    summary = (body.get("summary") or "").strip()
    message = (body.get("message") or "").strip()

    session = _upsert(machine, session_id, event, cwd, branch, title, summary, message)
    _touch_machine(machine)
    await _broadcast({"type": "session", "session": session})

    if _should_notify(session, event):
        status = session["status"]
        emoji = {"needs_input": "🟡", "waiting": "🟡", "idle": "✅"}.get(status, "🔔")
        label = {"needs_input": "needs input", "waiting": "waiting on you",
                 "idle": "done"}.get(status, status)
        headline = session.get("title") or session.get("name") or label
        meta = " · ".join(x for x in (label, machine, short_path(session.get("cwd") or "")) if x)
        text = f"{emoji} <b>{html.escape(headline)}</b>\n<i>{html.escape(meta)}</i>"
        markup = None
        if PUBLIC_URL:
            link = f"{PUBLIC_URL}/?key={TOKEN}" if TOKEN else PUBLIC_URL
            markup = {"inline_keyboard": [[{"text": "📊 Open dashboard", "url": link}]]}
        await _send_telegram(text, markup)
        _mark_notified(machine, session_id, status)

    return JSONResponse({"ok": True})


@app.get("/api/sessions")
async def api_sessions(_auth: None = Depends(_check_auth)):
    with _LOCK:
        rows = _conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
    return {"sessions": [dict(r) for r in rows], "now": time.time()}


@app.post("/api/enroll")
async def api_enroll(request: Request, _auth: None = Depends(_check_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    machine = (body.get("machine") or "").strip()
    if machine:
        _touch_machine(machine, enrolled=True)
        await _broadcast({"type": "refresh"})
    return {"ok": True}


@app.get("/api/machines")
async def api_machines(_auth: None = Depends(_check_auth)):
    with _LOCK:
        machines = _conn.execute(
            "SELECT * FROM machines ORDER BY COALESCE(last_seen, enrolled_at, 0) DESC"
        ).fetchall()
        counts: dict[str, dict] = {}
        for r in _conn.execute(
            "SELECT machine, status, COUNT(*) AS n FROM sessions GROUP BY machine, status"
        ).fetchall():
            d = counts.setdefault(r["machine"], {"total": 0, "active": 0})
            d["total"] += r["n"]
            if r["status"] in ("working", "needs_input", "waiting"):
                d["active"] += r["n"]
    out = []
    for m in machines:
        d = dict(m)
        c = counts.get(m["name"], {})
        d["sessions"] = c.get("total", 0)
        d["active"] = c.get("active", 0)
        out.append(d)
    return {"machines": out, "count": len(out), "now": time.time()}


@app.get("/api/settings")
async def get_settings(_auth: None = Depends(_check_auth)):
    tok = _tg_token()
    hint = (tok.split(":")[0] + ":…" + tok[-4:]) if tok else ""
    return {
        "telegram_configured": bool(tok and _tg_chat()),
        "telegram_bot_token_hint": hint,
        "telegram_chat_id": _tg_chat(),
        "notify_on_stop": _notify_on_stop(),
        "skin": _cfg_get("skin", "clean"),
    }


@app.post("/api/settings")
async def post_settings(request: Request, _auth: None = Depends(_check_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tok = (body.get("telegram_bot_token") or "").strip()
    if tok:  # only overwrite when a fresh token is supplied (field left blank = keep)
        _cfg_set("telegram_bot_token", tok)
    if "telegram_chat_id" in body:
        _cfg_set("telegram_chat_id", (body.get("telegram_chat_id") or "").strip())
    if "notify_on_stop" in body:
        _cfg_set("notify_on_stop", "1" if body.get("notify_on_stop") else "0")
    if body.get("skin"):
        _cfg_set("skin", str(body["skin"]))
    return {"ok": True}


@app.post("/api/settings/test")
async def test_settings(_auth: None = Depends(_check_auth)):
    tok, chat = _tg_token(), _tg_chat()
    if not (tok and chat):
        return JSONResponse({"ok": False, "error": "Set the bot token and chat ID first."},
                            status_code=400)
    try:
        ok, err = await _telegram_api(
            tok, chat, "✅ <b>pagr</b> — test message. Telegram is connected.")
        return {"ok": ok, "error": err or None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/settings/detect")
async def detect_chat(request: Request, _auth: None = Depends(_check_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tok = (body.get("telegram_bot_token") or "").strip() or _tg_token()
    if not tok:
        return JSONResponse({"ok": False, "error": "Enter the bot token first.", "chats": []},
                            status_code=400)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.telegram.org/bot{tok}/getUpdates")
            data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "getUpdates failed"), "chats": []}
        seen: dict = {}
        for u in data.get("result", []):
            msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
            ch = msg.get("chat") or {}
            if ch.get("id") is not None:
                seen[ch["id"]] = (ch.get("username") or ch.get("title")
                                  or ch.get("first_name") or str(ch["id"]))
        return {"ok": True, "chats": [{"id": k, "name": v} for k, v in seen.items()]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "chats": []}


@app.get("/favicon.svg")
async def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="8" fill="#1a7f37"/>'
           '<circle cx="16" cy="16" r="5.5" fill="#fff"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@app.post("/api/clear")
async def api_clear(_auth: None = Depends(_check_auth)):
    with _LOCK:
        _conn.execute(
            "DELETE FROM sessions WHERE status IN ('ended','idle') "
            "OR (status='working' AND updated_at < ?)",
            (time.time() - CLEAR_WORKING_AFTER,),
        )
        _conn.commit()
    await _broadcast({"type": "refresh"})
    return {"ok": True}


@app.get("/events")
async def events(_auth: None = Depends(_check_auth)):
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)

    async def gen():
        try:
            yield "event: ping\ndata: {}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(DASHBOARD_HTML.replace("__SKIN_DEFAULT__", _cfg_get("skin", "clean")))


# --------------------------------------------------------------------------- #
# Self-serve enrollment:  curl -fsSL <url>/enroll.sh | PAGR_TOKEN=… bash     #
# These are public (no secrets): the hook + merge script carry no token; the   #
# token is supplied by the operator at install time.                           #
# --------------------------------------------------------------------------- #
_HOOKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks")

ENROLL_SH = r"""#!/usr/bin/env bash
# pagr self-serve enrollment — installs the hook on the machine that runs this.
#   curl -fsSL __URL__/enroll.sh | PAGR_TOKEN=<token> bash -s -- --machine NAME
set -euo pipefail
PAGR_URL="${PAGR_URL:-__URL__}"
TOKEN="${PAGR_TOKEN:-}"
MACHINE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --machine) MACHINE="$2"; shift 2;;
    --token)   TOKEN="$2"; shift 2;;
    --url)     PAGR_URL="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
[ -n "$TOKEN" ] || { echo "error: set PAGR_TOKEN env or pass --token" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required" >&2; exit 1; }
mkdir -p "$HOME/.claude"
echo "→ installing pagr hook from $PAGR_URL"
curl -fsSL "$PAGR_URL/pagr-hook" -o "$HOME/.claude/pagr-hook"
chmod +x "$HOME/.claude/pagr-hook"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
curl -fsSL "$PAGR_URL/merge_settings.py" -o "$TMP"
PAGR_URL="$PAGR_URL" PAGR_TOKEN="$TOKEN" PAGR_MACHINE="$MACHINE" python3 "$TMP"
echo "✓ $(hostname) enrolled → $PAGR_URL  (new Claude Code sessions will report)"
"""


def _serve_hooks_file(name: str) -> PlainTextResponse:
    try:
        with open(os.path.join(_HOOKS_DIR, name), encoding="utf-8") as f:
            return PlainTextResponse(f.read())
    except OSError:
        raise HTTPException(status_code=404, detail="not found")


@app.get("/pagr-hook")
async def serve_hook():
    return _serve_hooks_file("pagr-hook")


@app.get("/merge_settings.py")
async def serve_merge():
    return _serve_hooks_file("merge_settings.py")


@app.get("/enroll.sh")
async def serve_enroll(request: Request):
    base = (PUBLIC_URL or str(request.base_url)).rstrip("/")
    return PlainTextResponse(ENROLL_SH.replace("__URL__", base),
                             media_type="text/x-shellscript")


# --------------------------------------------------------------------------- #
# Dashboard (single page, no build step) — modern light theme                  #
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0c131b">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<title>pagr · agents</title>
<style>
  /* ---------- palette: CLEAN is the default; MISSION overrides ---------- */
  :root{
    --bg:#eef1f5; --panel:#ffffff; --panel2:#f6f8fa; --line:#dfe3e8; --line2:#cfd6de;
    --text:#1f2328; --muted:#59636e; --faint:#8a95a1;
    --c-working:#0969da; --c-need:#bf8700; --c-wait:#8250df; --c-idle:#6e7781; --c-done:#1a7f37;
    --font-ui:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --font-head:var(--font-ui); --font-mono:ui-monospace,Menlo,Consolas,monospace;
    --head-spacing:.2px; --head-transform:none;
    --radar-stroke:rgba(9,105,218,.14); --radar-sweep:rgba(9,105,218,.22); --radar-bg:#fff;
    --shadow:0 1px 3px rgba(27,31,36,.08); --shadow-hi:0 6px 22px rgba(27,31,36,.13); --bggrid:none;
  }
  body[data-skin="mission"]{
    --bg:#070b10; --panel:#0c131b; --panel2:#0e1822; --line:#1a2734; --line2:#243444;
    --text:#c4d2e0; --muted:#62768a; --faint:#3d4d5e;
    --c-working:#37bdf8; --c-need:#ffb627; --c-wait:#a78bfa; --c-idle:#62768a; --c-done:#2fd07a;
    --font-ui:"IBM Plex Mono",ui-monospace,monospace; --font-head:"Chakra Petch",sans-serif; --font-mono:"IBM Plex Mono",monospace;
    --head-spacing:1.5px; --head-transform:uppercase;
    --radar-stroke:rgba(47,208,122,.16); --radar-sweep:rgba(47,208,122,.5); --radar-bg:radial-gradient(circle,rgba(47,208,122,.08),transparent 70%);
    --shadow:0 1px 2px rgba(0,0,0,.4); --shadow-hi:0 6px 22px rgba(0,0,0,.5);
    --bggrid:linear-gradient(#11202e 1px,transparent 1px),linear-gradient(90deg,#11202e 1px,transparent 1px);
  }

  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background-color:var(--bg);background-image:var(--bggrid);background-size:42px 42px;
       color:var(--text);font:14px/1.5 var(--font-ui);-webkit-font-smoothing:antialiased}
  b{font-weight:600}

  /* ---------- command bar ---------- */
  .cmd{display:flex;align-items:center;gap:20px;padding:11px 20px;background:var(--panel);
       border-bottom:1px solid var(--line2);position:sticky;top:0;z-index:20;flex-wrap:wrap}
  .brand{display:flex;align-items:center;gap:9px;font-family:var(--font-head);font-weight:700;font-size:16px;
         letter-spacing:var(--head-spacing);text-transform:var(--head-transform);color:var(--text)}
  .brand .dot{width:10px;height:10px;border-radius:50%;background:var(--c-done);box-shadow:0 0 0 3px rgba(26,127,55,.16)}
  body[data-skin="mission"] .brand .dot{background:var(--c-need);box-shadow:0 0 9px var(--c-need)}
  .brand .dot.off{background:#cf222e;box-shadow:0 0 0 3px rgba(207,34,46,.16)}
  .brand .sep{color:var(--faint)}
  .brand .skinname{color:var(--c-working);font-size:13px}
  .crumb{color:var(--muted);font-family:var(--font-head);font-size:11.5px;letter-spacing:1px;text-transform:uppercase}
  .crumb b{color:var(--c-done)}
  .metrics{display:flex;gap:8px;margin-left:auto}
  .metric{min-width:74px;text-align:right;padding:4px 12px;border-left:1px solid var(--line)}
  .metric .n{font-family:var(--font-head);font-size:19px;font-weight:700;line-height:1.05;color:var(--text)}
  .metric .l{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--faint)}
  .metric.alert .n{color:var(--c-need)}
  .cmd-right{display:flex;align-items:center;gap:8px}

  button,.skin-sel{background:var(--panel2);color:var(--text);border:1px solid var(--line2);border-radius:8px;
    padding:7px 12px;font:500 12.5px var(--font-ui);cursor:pointer;transition:.12s}
  body[data-skin="mission"] button,body[data-skin="mission"] .skin-sel{border-radius:4px;font-family:var(--font-head);
    text-transform:uppercase;letter-spacing:1.5px;font-size:11px}
  button:hover,.skin-sel:hover{border-color:var(--c-working)}
  .icon{font-size:15px;padding:6px 9px;line-height:1}

  /* ---------- layout: full width, rail + feed ---------- */
  .layout{display:grid;grid-template-columns:288px 1fr;gap:18px;padding:18px 22px 60px;align-items:start}
  .rail{display:flex;flex-direction:column;gap:12px;position:sticky;top:74px}
  .rail-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:2px}
  .sect{font-family:var(--font-head);font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--faint)}
  .sect b{color:var(--text)}
  .add{padding:4px 11px;font-size:11.5px;border-radius:7px;background:var(--c-working);color:#fff;border:1px solid var(--c-working)}
  body[data-skin="mission"] .add{border-radius:4px;color:#04121a}
  .add:hover{filter:brightness(1.08)}

  .station{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--panel);box-shadow:var(--shadow)}
  body[data-skin="mission"] .station{border-radius:6px}
  .station .h{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .station .name{font-family:var(--font-head);letter-spacing:1px;color:var(--text);font-size:13px;font-weight:600;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .station .sub{color:var(--muted);font-size:11px;margin-top:3px}
  .led{width:9px;height:9px;border-radius:50%;flex:none}
  .led.lw{background:var(--c-working);box-shadow:0 0 8px var(--c-working);animation:throbb 1.4s ease-in-out infinite}
  .led.la{background:var(--c-need);box-shadow:0 0 8px var(--c-need);animation:blink 1.1s steps(1) infinite}
  .led.lp{background:var(--c-wait);box-shadow:0 0 8px var(--c-wait)}
  .led.lg{background:var(--c-done);box-shadow:0 0 8px var(--c-done)}
  .led.li{background:var(--c-idle)}
  @keyframes blink{50%{opacity:.25}} @keyframes throbb{50%{opacity:.5}} @keyframes spin{to{transform:rotate(360deg)}}

  .radar{aspect-ratio:1;border:1px solid var(--line);border-radius:10px;position:relative;overflow:hidden;
    background:var(--radar-bg);margin-top:4px}
  .radar::before{content:"";position:absolute;inset:0;
    background:repeating-radial-gradient(circle at 50% 50%,transparent 0 24px,var(--radar-stroke) 24px 25px)}
  .radar .sweep{position:absolute;inset:0;background:conic-gradient(from 0deg,var(--radar-sweep),transparent 32%);animation:spin 3.4s linear infinite}
  .radar .blip{position:absolute;width:6px;height:6px;border-radius:50%;background:var(--c-need);box-shadow:0 0 8px var(--c-need)}

  /* ---------- feed ---------- */
  .feed-controls{display:flex;align-items:center;gap:12px;margin-bottom:4px}
  .feed-count{margin-left:auto;color:var(--faint);font-size:12px;font-family:var(--font-head);letter-spacing:1px}
  .grp{font-family:var(--font-head);text-transform:uppercase;letter-spacing:2.5px;color:var(--faint);font-size:11px;margin:18px 0 4px 2px}
  .grp.alert{color:var(--c-need)}
  .feedhead,.frow{display:grid;grid-template-columns:150px 158px 1fr 104px 56px;gap:16px;align-items:center}
  .feedhead{color:var(--faint);font-size:10.5px;padding:0 14px 8px;font-family:var(--font-head);letter-spacing:1.5px;text-transform:uppercase}
  .frow{padding:13px 14px;border:1px solid var(--line);border-left-width:3px;border-radius:10px;margin-bottom:9px;
    background:var(--panel);box-shadow:var(--shadow);transition:box-shadow .14s,transform .14s}
  body[data-skin="mission"] .frow{border-radius:6px;margin-bottom:8px}
  .frow:hover{box-shadow:var(--shadow-hi);transform:translateY(-1px)}
  .frow.s-working{border-left-color:var(--c-working)} .frow.s-needs_input{border-left-color:var(--c-need)}
  .frow.s-waiting{border-left-color:var(--c-wait)} .frow.s-idle{border-left-color:var(--c-idle)}
  .frow.s-ended{border-left-color:var(--line2);opacity:.72}
  .frow.stale{opacity:.55}
  .gcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(296px,1fr));gap:11px;margin-bottom:6px}
  .gcard{border:1px solid var(--line);border-left-width:3px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow);padding:13px 15px;transition:box-shadow .14s,transform .14s}
  body[data-skin="mission"] .gcard{border-radius:6px}
  .gcard:hover{box-shadow:var(--shadow-hi);transform:translateY(-1px)}
  .gcard.s-working{border-left-color:var(--c-working)} .gcard.s-needs_input{border-left-color:var(--c-need)}
  .gcard.s-waiting{border-left-color:var(--c-wait)} .gcard.s-idle{border-left-color:var(--c-idle)}
  .gcard.s-ended{border-left-color:var(--line2);opacity:.72} .gcard.stale{opacity:.55}
  .gc-top{display:flex;align-items:center;gap:8px}
  .gc-top .gc-age{margin-left:auto;color:var(--faint);font-size:11px}
  .gc-title{font-weight:600;color:var(--text);margin-top:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  body[data-skin="mission"] .gc-title{color:#e8f0f7}
  .gc-sum{color:var(--muted);font-size:12.5px;margin-top:4px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;min-height:2.5em}
  .gc-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}
  .gc-foot .st-name{font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis}
  .gc-branch{color:var(--c-working);font-family:var(--font-mono);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%;flex:none}
  .cell-status{display:flex;align-items:center;gap:9px}
  .slabel{font-family:var(--font-head);font-size:11px;font-weight:600;letter-spacing:1px}
  .st-working{color:var(--c-working)} .st-needs_input{color:var(--c-need)} .st-waiting{color:var(--c-wait)}
  .st-idle{color:var(--muted)} .st-ended{color:var(--faint)}
  .st-name{font-family:var(--font-head);font-weight:600;letter-spacing:.5px;color:var(--text);font-size:13px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block}
  .st-id{font-family:var(--font-mono);color:var(--faint);font-size:11px}
  .cell-task{min-width:0}
  .t-title{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  body[data-skin="mission"] .t-title{color:#e8f0f7}
  .t-sum{color:var(--muted);font-size:12.5px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .t-path{color:var(--faint);font-family:var(--font-mono);font-size:11.5px}
  .cell-branch{color:var(--c-working);font-size:12px;font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cell-age{color:var(--faint);text-align:right;font-size:12px}
  .empty,.locked{color:var(--muted);text-align:center;margin-top:64px}
  .locked input{background:var(--panel);border:1px solid var(--line2);color:var(--text);border-radius:8px;padding:9px 12px;margin:10px 6px 0;width:250px;font-size:14px}
  a{color:var(--c-working)}

  /* ---------- modals ---------- */
  .modal{display:none;position:fixed;inset:0;background:rgba(10,14,20,.5);align-items:flex-start;justify-content:center;z-index:40;padding:60px 16px}
  body[data-skin="mission"] .modal{background:rgba(3,6,9,.62)}
  .modal.show{display:flex}
  .sheet{background:var(--panel);border:1px solid var(--line2);border-radius:14px;width:min(560px,100%);
    box-shadow:0 18px 50px rgba(0,0,0,.3);max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
  body[data-skin="mission"] .sheet{border-radius:8px}
  .sheet-h{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--line);
    font-family:var(--font-head);font-weight:600;letter-spacing:.5px}
  .sheet-h .x{border:none;background:none;color:var(--muted);font-size:14px;padding:4px 6px}
  .sheet-body{padding:16px 18px;overflow:auto}
  .addlabel{font-size:12px;color:var(--muted);margin-bottom:8px}
  .cmdrow{display:flex;gap:8px;align-items:stretch}
  .cmdrow code{flex:1;min-width:0;background:#0d1117;color:#e6edf3;border-radius:8px;padding:10px 12px;
    font:12px/1.5 var(--font-mono);overflow-x:auto;white-space:pre}
  .cmdrow button{white-space:nowrap}
  .mininp{display:block;margin-top:12px;font-size:12px;color:var(--muted)}
  .mininp input{display:block;width:100%;margin-top:4px;background:var(--panel2);border:1px solid var(--line2);border-radius:7px;padding:8px 10px;font-size:13px;color:var(--text)}
  .field{display:block;margin-bottom:14px;font-size:12.5px;color:var(--muted)}
  .field input,.field select{display:block;width:100%;margin-top:5px;background:var(--panel2);border:1px solid var(--line2);border-radius:8px;padding:9px 11px;font-size:14px;color:var(--text)}
  .field .inline{display:flex;gap:8px} .field .inline input{margin-top:0}
  .choices{display:flex;flex-wrap:wrap;gap:6px;margin:-6px 0 14px}
  .choices button{font-size:12px;padding:5px 10px}
  .check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text);margin-bottom:16px}
  .actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .primary{background:var(--c-working);color:#fff;border-color:var(--c-working)}
  .subtle{color:var(--muted);font-size:12px} .hint{color:var(--muted);font-size:12.5px;margin:0 0 16px}

  @media(max-width:860px){ .layout{grid-template-columns:1fr} .rail{position:static} .metrics{order:3;width:100%;margin:8px 0 0;justify-content:flex-start} .metric{border-left:none;border-right:1px solid var(--line);padding-left:0} }
  @media(max-width:620px){ .feedhead{display:none} .frow{grid-template-columns:1fr auto;gap:5px 10px} .cell-branch{display:none} .cell-age{grid-column:2;grid-row:1} }
</style>
</head>
<body>
<header class="cmd">
  <span class="brand"><span class="dot" id="live"></span>pagr<span class="sep">▸</span><span class="skinname" id="skinName">CLEAN</span></span>
  <span class="crumb">FLEET <b id="fleetState">nominal</b> · TG <b id="tgState">—</b></span>
  <div class="metrics" id="metrics"></div>
  <div class="cmd-right">
    <button class="icon" onclick="openSettings()" title="Settings" aria-label="Settings">⚙</button>
  </div>
</header>

<div class="layout">
  <aside class="rail">
    <div class="rail-head"><span class="sect">Stations · <b id="stationCount">0</b></span><button class="add" onclick="openAdd()">+ Add</button></div>
    <div id="stations"></div>
    <div class="radar"><div class="sweep"></div><div class="blip" style="top:30%;left:62%"></div><div class="blip" style="top:58%;left:40%"></div></div>
  </aside>
  <main class="feed-wrap">
    <div class="feed-controls">
      <button id="modeBtn" onclick="toggleMode()">Group: status</button>
      <button onclick="clearDone()" title="Remove finished &amp; idle sessions, plus working ones stale for >1h">Clear done</button>
      <span class="feed-count" id="feedCount"></span>
      <button id="viewBtn" onclick="toggleView()">▤ List</button>
    </div>
    <div id="feed"><div class="empty">Connecting…</div></div>
  </main>
</div>

<div id="addModal" class="modal" onclick="if(event.target===this)closeAdd()">
  <div class="sheet">
    <div class="sheet-h"><span>Add a machine</span><button class="x" onclick="closeAdd()">✕</button></div>
    <div class="sheet-body">
      <p class="hint">Run this on the machine you want to track (token is baked in). It only affects new Claude Code sessions started afterward.</p>
      <div class="addlabel">Enroll command</div>
      <div class="cmdrow"><code id="installCmd"></code><button id="copyBtn" onclick="copyInstall()">Copy</button></div>
      <label class="mininp">Machine name<input id="mname" placeholder="(optional — defaults to the box's hostname)" oninput="renderInstall()"></label>
    </div>
  </div>
</div>

<div id="settingsModal" class="modal" onclick="if(event.target===this)closeSettings()">
  <div class="sheet">
    <div class="sheet-h"><span>Settings</span><button class="x" onclick="closeSettings()">✕</button></div>
    <div class="sheet-body">
      <label class="field">Theme
        <select id="skinSel" onchange="setSkin(this.value)">
          <option value="clean">◍ Clean (light)</option>
          <option value="mission">▣ Mission Control (dark)</option>
        </select>
      </label>
      <div class="addlabel" style="margin:-2px 0 12px;font-weight:600;color:var(--text);font-size:13px">Telegram</div>
      <p class="hint">Create a bot with <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> (send <code>/newbot</code>), paste its token below, send your new bot any message, then <b>Detect</b> your chat ID.</p>
      <label class="field">Bot token
        <input id="botToken" type="password" placeholder="(leave blank to keep current)" autocomplete="off">
        <span id="tokenHint" class="subtle"></span>
      </label>
      <label class="field">Chat ID
        <span class="inline"><input id="chatId" placeholder="e.g. 7024650475"><button onclick="detectChat()">Detect</button></span>
      </label>
      <div id="chatChoices" class="choices"></div>
      <label class="check"><input type="checkbox" id="notifyStop"> Also notify on every turn end (noisier)</label>
      <div class="actions">
        <button class="primary" onclick="saveSettings()">Save</button>
        <button onclick="testTelegram()">Send test</button>
        <span id="settingsMsg" class="subtle"></span>
      </div>
    </div>
  </div>
</div>

<script>
const META = {
  needs_input:{label:"NEEDS INPUT", order:0, group:"Needs you", led:"la"},
  waiting:    {label:"WAITING",     order:0, group:"Needs you", led:"lp"},
  working:    {label:"WORKING",     order:1, group:"Working",   led:"lw"},
  idle:       {label:"IDLE",        order:2, group:"Idle / done", led:"lg"},
  ended:      {label:"ENDED",       order:3, group:"Idle / done", led:"li"},
};
const SKIN_LABEL = {clean:"CLEAN", mission:"MISSION CONTROL"};
let KEY = new URLSearchParams(location.search).get("key") || localStorage.getItem("pagr_key") || "";
if (KEY) localStorage.setItem("pagr_key", KEY);
let MODE = localStorage.getItem("pagr_mode") || "status";
let VIEW = localStorage.getItem("pagr_view") || "list";   // "list" | "grid"
let SKIN = new URLSearchParams(location.search).get("skin") || localStorage.getItem("pagr_skin") || "__SKIN_DEFAULT__" || "clean";
if (SKIN === "default") SKIN = "clean";
let LAST = {sessions:[], now:0, machines:[]};
const $ = id => document.getElementById(id);

function ago(ts, now){
  const s = Math.max(0, Math.floor(now - ts));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m";
  if (s < 86400) return Math.floor(s/3600) + "h";
  return Math.floor(s/86400) + "d";
}
function esc(t){const d=document.createElement("div");d.textContent=t==null?"":t;return d.innerHTML;}
function needy(s){ return s.status==="needs_input" || s.status==="waiting"; }
function active(s){ return s.status==="working" || needy(s); }
function ordOf(s){ return (META[s.status]||{order:9}).order; }
function byStatusThenTime(a,b){ return ordOf(a)-ordOf(b) || (b.updated_at-a.updated_at); }
function shortId(s){ return (s.session_id||"").slice(-6) || "??????"; }
function shortPath(p){ if(!p) return ""; const parts=p.replace(/\/+$/,"").split("/").filter(Boolean); return parts.length>3 ? "…/"+parts.slice(-2).join("/") : p; }
function authHdr(extra){ const h=extra||{}; if(KEY) h.Authorization="Bearer "+KEY; return h; }

function applySkin(){
  document.body.dataset.skin = SKIN;
  const sel = $("skinSel"); if (sel) sel.value = SKIN;
  $("skinName").textContent = SKIN_LABEL[SKIN] || SKIN;
}
function setSkin(v){
  SKIN = v; localStorage.setItem("pagr_skin", v); applySkin(); paint();
  fetch("/api/settings", {method:"POST", headers: authHdr({"Content-Type":"application/json"}), body: JSON.stringify({skin:v})}).catch(()=>{});
}
function toggleMode(){ MODE = (MODE==="status")?"machine":"status"; localStorage.setItem("pagr_mode", MODE); paint(); }

function lock(){
  $("feed").innerHTML = '<div class="locked">Enter your pagr key to view agents.'
    + '<div><input id="k" type="password" placeholder="PAGR_TOKEN" autofocus>'
    + '<button onclick="saveKey()">Unlock</button></div></div>';
}
function saveKey(){ const v=$("k").value.trim(); if(!v) return; localStorage.setItem("pagr_key", v); KEY=v; location.href=location.pathname; }

function metricTile(n, l, alert){ return '<div class="metric'+(alert?' alert':'')+'"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'; }
function frow(s, now){
  const m = META[s.status] || {label:s.status, led:"li"};
  const stale = (s.status==="working" && (now - s.updated_at) > 600) ? " stale" : "";
  const title = s.title || s.name || ("session "+shortId(s));
  return '<div class="frow s-'+s.status+stale+'">'
    + '<div class="cell-status"><span class="led '+m.led+'"></span><span class="slabel st-'+s.status+'">'+m.label+'</span></div>'
    + '<div class="cell-station"><span class="st-name">'+esc(s.machine)+'</span><span class="st-id">#'+esc(shortId(s))+'</span></div>'
    + '<div class="cell-task"><div class="t-title">'+esc(title)+'</div><div class="t-sum">'+esc(s.summary||"—")+' <span class="t-path" title="'+esc(s.cwd||"")+'">'+esc(shortPath(s.cwd))+'</span></div></div>'
    + '<div class="cell-branch" title="'+esc(s.branch||"")+'">'+(s.branch?'⎇ '+esc(s.branch):'—')+'</div>'
    + '<div class="cell-age">'+ago(s.updated_at, now)+'</div></div>';
}
const FEEDHEAD = '<div class="feedhead"><span>Status</span><span>Station</span><span>Task</span><span>Branch</span><span>T+</span></div>';
function gcard(s, now){
  const m = META[s.status] || {label:s.status, led:"li"};
  const stale = (s.status==="working" && (now - s.updated_at) > 600) ? " stale" : "";
  const title = s.title || s.name || ("session "+shortId(s));
  return '<div class="gcard s-'+s.status+stale+'">'
    + '<div class="gc-top"><span class="led '+m.led+'"></span><span class="slabel st-'+s.status+'">'+m.label+'</span><span class="gc-age">'+ago(s.updated_at, now)+'</span></div>'
    + '<div class="gc-title" title="'+esc(title)+'">'+esc(title)+'</div>'
    + '<div class="gc-sum">'+esc(s.summary||"—")+'</div>'
    + '<div class="gc-foot"><span class="st-name">'+esc(s.machine)+' <span class="st-id">#'+esc(shortId(s))+'</span></span>'
    + '<span class="gc-branch" title="'+esc(s.branch||"")+'">'+(s.branch?'⎇ '+esc(s.branch):'')+'</span></div></div>';
}
function toggleView(){ VIEW = (VIEW==="list")?"grid":"list"; localStorage.setItem("pagr_view", VIEW); paint(); }

function renderStations(){
  const {sessions, now, machines} = LAST;
  $("stationCount").textContent = machines.length;
  if (!machines.length){ $("stations").innerHTML = '<div class="station"><div class="sub">No stations yet — click <b>+ Add</b>.</div></div>'; return; }
  $("stations").innerHTML = machines.map(mc => {
    const mine = sessions.filter(s => s.machine === mc.name);
    const act = mine.filter(active).length;
    const led = mine.some(needy) ? "la" : (act ? "lw" : "lg");
    return '<div class="station"><div class="h"><span class="name">'+esc(mc.name)+'</span><span class="led '+led+'"></span></div>'
      + '<div class="sub">'+mine.length+' sessions · '+act+' active · '+(mc.last_seen?ago(mc.last_seen,now)+' ago':'—')+'</div></div>';
  }).join("");
}

function renderFeed(){
  const {sessions, now} = LAST;
  if (!sessions.length){ $("feed").innerHTML = '<div class="empty">No agents yet. Click <b>+ Add</b> to enroll a machine.</div>'; return; }
  const grid = VIEW === "grid";
  const items = arr => grid
    ? '<div class="gcards">' + arr.map(s => gcard(s, now)).join("") + '</div>'
    : FEEDHEAD + arr.map(s => frow(s, now)).join("");
  let html = "";
  if (MODE === "machine"){
    const groups = {};
    sessions.forEach(s => { (groups[s.machine] = groups[s.machine] || []).push(s); });
    Object.keys(groups).sort((a,b)=>{ const na=groups[a].some(needy), nb=groups[b].some(needy); return na!==nb?(na?-1:1):a.localeCompare(b); })
      .forEach(mc => {
        const arr = groups[mc].sort(byStatusThenTime);
        const n = arr.filter(needy).length;
        html += '<div class="grp'+(n?' alert':'')+'">'+esc(mc)+' · '+arr.length+(n?' · '+n+' need you':'')+'</div>' + items(arr);
      });
  } else {
    const groups = []; const seen = {};
    sessions.slice().sort(byStatusThenTime).forEach(s => {
      const g = (META[s.status]||{group:"Other"}).group;
      if (!seen[g]){ seen[g] = {name:g, alert:ordOf(s)===0, items:[]}; groups.push(seen[g]); }
      seen[g].items.push(s);
    });
    groups.forEach(g => { html += '<div class="grp'+(g.alert?' alert':'')+'">'+esc(g.name)+'</div>' + items(g.items); });
  }
  $("feed").innerHTML = html;
}

function paint(){
  const {sessions} = LAST;
  const need = sessions.filter(needy).length;
  const act = sessions.filter(active).length;
  $("metrics").innerHTML = metricTile(need,"needs you",need>0) + metricTile(act,"active") + metricTile(sessions.length,"sessions") + metricTile(LAST.machines.length,"stations");
  $("fleetState").textContent = need ? "needs you" : "nominal";
  $("fleetState").style.color = need ? "var(--c-need)" : "";
  document.title = (need ? "("+need+") " : "") + "pagr";
  $("modeBtn").textContent = "Group: " + MODE;
  $("viewBtn").textContent = VIEW === "grid" ? "▦ Grid" : "▤ List";
  $("feedCount").textContent = sessions.length + " sessions";
  renderStations();
  renderFeed();
}

async function load(){
  try{
    const h = authHdr();
    const [rs, rm] = await Promise.all([fetch("/api/sessions",{headers:h}), fetch("/api/machines",{headers:h})]);
    if (rs.status === 401){ lock(); return; }
    const data = await rs.json();
    let machines = []; try{ machines = (await rm.json()).machines || []; }catch(e){}
    LAST = {sessions:data.sessions, now:data.now, machines};
    $("live").classList.remove("off");
    paint();
  }catch(e){ $("live").classList.add("off"); }
}
async function fetchMeta(){
  try{ const d = await (await fetch("/api/settings",{headers:authHdr()})).json();
    $("tgState").textContent = d.telegram_configured ? "linked" : "off";
    $("tgState").style.color = d.telegram_configured ? "var(--c-done)" : "var(--faint)";
  }catch(e){}
}
async function clearDone(){ await fetch("/api/clear",{method:"POST",headers:authHdr()}); load(); }
function connectSSE(){
  const es = new EventSource("/events" + (KEY ? "?key="+encodeURIComponent(KEY) : ""));
  let t=null;
  es.onopen = () => $("live").classList.remove("off");
  es.onmessage = () => { clearTimeout(t); t=setTimeout(load,150); };
  es.onerror = () => $("live").classList.add("off");
}

/* Add-machine modal */
function renderInstall(){
  const name = ($("mname").value||"").trim();
  $("installCmd").textContent = "curl -fsSL " + location.origin + "/enroll.sh | PAGR_TOKEN=" + (KEY||"<token>") + " bash" + (name?" -s -- --machine "+name:"");
}
function openAdd(){ renderInstall(); $("addModal").classList.add("show"); }
function closeAdd(){ $("addModal").classList.remove("show"); }
function copyInstall(){ navigator.clipboard.writeText($("installCmd").textContent).then(()=>{ const b=$("copyBtn"); b.textContent="Copied!"; setTimeout(()=>b.textContent="Copy",1500); }); }

/* Settings modal */
function openSettings(){
  $("settingsMsg").textContent=""; $("chatChoices").innerHTML=""; $("botToken").value="";
  fetch("/api/settings",{headers:authHdr()}).then(r=> r.status===401 ? (lock(),null) : r.json()).then(d=>{
    if(!d) return;
    $("tokenHint").textContent = d.telegram_bot_token_hint ? "current: "+d.telegram_bot_token_hint : "not set yet";
    $("chatId").value = d.telegram_chat_id || ""; $("notifyStop").checked = !!d.notify_on_stop;
  }).catch(()=>{});
  $("settingsModal").classList.add("show");
}
function closeSettings(){ $("settingsModal").classList.remove("show"); }
async function detectChat(){
  const msg=$("settingsMsg"); msg.textContent="detecting…";
  try{
    const r = await fetch("/api/settings/detect",{method:"POST",headers:authHdr({"Content-Type":"application/json"}),body:JSON.stringify({telegram_bot_token:$("botToken").value.trim()})});
    const d = await r.json(); const box=$("chatChoices");
    if(!d.ok){ msg.textContent=d.error||"detect failed"; box.innerHTML=""; return; }
    if(!d.chats.length){ msg.textContent="No chats yet — message your bot, then Detect again."; box.innerHTML=""; return; }
    msg.textContent="Pick your chat:";
    box.innerHTML = d.chats.map(c=>'<button onclick="document.getElementById(\'chatId\').value=\''+c.id+'\'">'+esc(c.name)+' ('+c.id+')</button>').join("");
  }catch(e){ msg.textContent="detect error"; }
}
async function saveSettings(){
  const msg=$("settingsMsg"); msg.textContent="saving…";
  const body={ telegram_chat_id:$("chatId").value.trim(), notify_on_stop:$("notifyStop").checked };
  const tok=$("botToken").value.trim(); if(tok) body.telegram_bot_token=tok;
  try{ await fetch("/api/settings",{method:"POST",headers:authHdr({"Content-Type":"application/json"}),body:JSON.stringify(body)});
    $("botToken").value=""; msg.textContent="saved ✓"; fetchMeta();
  }catch(e){ msg.textContent="save error"; }
}
async function testTelegram(){
  const msg=$("settingsMsg"); msg.textContent="sending…";
  try{ const d = await (await fetch("/api/settings/test",{method:"POST",headers:authHdr()})).json();
    msg.textContent = d.ok ? "test sent ✓ — check Telegram" : ("failed: "+(d.error||"unknown"));
  }catch(e){ msg.textContent="test error"; }
}
document.addEventListener("keydown", e => { if(e.key==="Escape"){ closeAdd(); closeSettings(); } });

applySkin();
if (!KEY) lock(); else { load(); fetchMeta(); connectSSE(); setInterval(load, 15000); }
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    # Cap graceful shutdown so the long-lived /events (SSE) streams don't block
    # restarts for ~90s on every deploy/reload. Clients auto-reconnect.
    uvicorn.run("app:app",
                host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")),
                timeout_graceful_shutdown=5)
