# pagr

[![Deploy to InstaPods](https://instapods.com/deploy-button.svg)](https://app.instapods.com/dashboard/pods/create?repo=https://github.com/vikasprogrammer/pagr&utm_source=readme_badge)

**Lightweight dashboard for every Claude Code agent you run** — on your machine
or any remote SSH box — showing real-time session status, with a **Telegram
ping** the moment an agent needs your input or has been left waiting.

![pagr dashboard — the Mission Control skin](assets/preview.png)

## Why pagr?

When you've got several Claude Code agents going at once — some local, some on
remote servers — it's easy to lose track. One's chugging along, another quietly
stopped to ask for permission ten minutes ago, and a third already finished.
pagr puts them all on one screen and taps you on Telegram the moment one
actually needs you, so you're not babysitting terminals.

## What you get

- **One live board** for every agent, across every machine.
- **Status at a glance** — see which agents are working, waiting on you, or done.
- **Telegram pings only when it matters** — when an agent needs input or has been
  left idle. No spam on every step.
- **Works anywhere Claude Code runs** — your laptop and any SSH-reachable server,
  each enrolled with a single command.
- **Two looks** — a clean light theme or a "Mission Control" dark ops view.

## How it works

A tiny [hook](hooks/pagr-hook) on each machine reports Claude Code's activity to
the pagr server, which updates the dashboard and sends the Telegram alerts.

```
each machine ──(Claude Code hooks)──▶ ~/.claude/pagr-hook ──HTTPS──▶ pagr server ──▶ dashboard + Telegram
```

There's nothing to install but that one small script — it's pure Python standard
library and only fires on Claude Code's own lifecycle events.

## Quick start

Three steps: deploy the server, enroll a machine, open the dashboard.

### 1. Deploy the server (InstaPods)

```bash
cd pagr
instapods deploy pagr --preset python --plan launch
instapods env set pagr \
  PAGR_TOKEN=<secret> \
  TELEGRAM_BOT_TOKEN=<botfather-token> \
  TELEGRAM_CHAT_ID=<your-chat-id> \
  PAGR_DB=/home/instapod/pagr.db \
  PAGR_PUBLIC_URL=https://pagr.sh
instapods pods reload pagr
```

### 2. Enroll a machine

Run this **on the machine you want to track** — the server hosts the installer,
so nothing needs to be checked out there:

```bash
curl -fsSL https://pagr.sh/enroll.sh | PAGR_TOKEN=<secret> bash -s -- --machine NAME
```

Prefer to push from your Mac over SSH (no shell on the box needed)?

```bash
hooks/install.sh --ssh user@host --machine NAME \
  --url https://pagr.sh --token <secret>
```

Either way, it drops `~/.claude/pagr-hook` and adds an `env` + `hooks` block to
that machine's `~/.claude/settings.json` — your existing settings are preserved,
with a `.pagr.bak` backup kept. It only affects **new** Claude Code sessions
started afterward, and `--machine` defaults to the hostname. Full details
(prerequisites, verifying, removing) are in [hooks/README.md](hooks/README.md).

### 3. Open the dashboard

```
https://pagr.sh/?key=<secret>
```

That's it — start a new `claude` session on any enrolled machine and watch it
show up.

## What's on the dashboard

- **Live board** — machine, title, folder, summary, and status for every
  session, grouped by status or by machine (toggle).
- **Machines** — a count and per-machine detail, plus a copy-paste one-liner to
  enroll a new box.
- **⚙ Settings** — set up the Telegram bot right in the browser (token, chat ID,
  **Detect** chat ID, **Send test**). Stored on the server, so you don't have to
  set `TELEGRAM_*` via `instapods env`.
- **Clear done** — clears finished/idle sessions, plus working ones stale >1h.
- **Skins** — switch between **Clean** (light) and **Mission Control** (dark ops,
  station rail + radar) themes, and **list / grid** feed views. Early design
  explorations live in [`concepts/`](concepts/).

## Status reference

How each Claude Code event maps to a status on the board — and whether it pings
Telegram:

| Claude Code event                  | Dashboard status | Telegram? |
|------------------------------------|------------------|-----------|
| `SessionStart`, `UserPromptSubmit` | working          | no        |
| `Notification` `permission_prompt` | needs input      | **yes**   |
| `Notification` `idle_prompt`       | waiting          | **yes**   |
| `Stop`                             | idle             | no¹       |
| `SessionEnd`                       | ended            | no        |

¹ `Stop` fires at the end of *every* turn, so pinging on it would be spam. Flip
the ⚙ Settings toggle (or set `NOTIFY_ON_STOP=1`) if you want a ping at every
turn anyway.

## Server env vars

| Var | Purpose |
|-----|---------|
| `PAGR_TOKEN` | shared secret for ingest + dashboard (required in production) |
| `PAGR_DB` | SQLite path — keep it outside `/home/instapod/app` so deploys don't wipe it |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | enable Telegram push |
| `PAGR_PUBLIC_URL` | dashboard URL included in Telegram messages |
| `NOTIFY_ON_STOP` | set to `1` to also push on every turn end |

## Run locally (dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8000           # dashboard at http://localhost:8000  (no token => open)
```
