#!/usr/bin/env python3
"""Idempotently merge pagr's env + hooks into a Claude Code settings.json.

Runs on the target machine (locally, or piped to a remote `python3 -` over ssh).
Preserves every existing key; only adds pagr's env vars and hook entries it
doesn't already find. Reads config from the environment:

    PAGR_URL       (required)  -> settings.env.PAGR_URL
    PAGR_TOKEN                  -> settings.env.PAGR_TOKEN
    PAGR_MACHINE               -> settings.env.PAGR_MACHINE (default: hostname)
    PAGR_HOOK_PATH             -> path to the hook (default: ~/.claude/pagr-hook)
    PAGR_SETTINGS              -> settings file (default: ~/.claude/settings.json)
"""
import json
import os
import socket
import sys

SETTINGS = os.environ.get("PAGR_SETTINGS") or os.path.expanduser("~/.claude/settings.json")
HOOK = os.environ.get("PAGR_HOOK_PATH") or os.path.expanduser("~/.claude/pagr-hook")
URL = os.environ.get("PAGR_URL", "").strip()
TOKEN = os.environ.get("PAGR_TOKEN", "").strip()
MACHINE = os.environ.get("PAGR_MACHINE", "").strip() or socket.gethostname().replace(".local", "")

# event name -> list of (matcher, hook-arg)
DESIRED = {
    "SessionStart":     [("", "start")],
    "UserPromptSubmit": [("", "prompt")],
    "Stop":             [("", "stop")],
    "SessionEnd":       [("", "end")],
    "Notification":     [("permission_prompt", "needs_input"), ("idle_prompt", "idle")],
}

if not URL:
    sys.stderr.write("merge_settings: PAGR_URL is required\n")
    sys.exit(1)

# Load existing settings (tolerate missing / empty / invalid -> start fresh).
settings = {}
if os.path.exists(SETTINGS):
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f) or {}
    except Exception:
        settings = {}
if not isinstance(settings, dict):
    settings = {}

# env block
env = settings.setdefault("env", {})
env["PAGR_URL"] = URL
if TOKEN:
    env["PAGR_TOKEN"] = TOKEN
env["PAGR_MACHINE"] = MACHINE

# hooks block (non-destructive: add our commands if absent, keep everything else)
hooks = settings.setdefault("hooks", {})
added = 0
for event, entries in DESIRED.items():
    arr = hooks.setdefault(event, [])
    for matcher, arg in entries:
        cmd = f"{HOOK} {arg}"
        already = any(
            isinstance(block, dict)
            and block.get("matcher", "") == matcher
            and any(isinstance(h, dict) and h.get("command") == cmd
                    for h in (block.get("hooks") or []))
            for block in arr
        )
        if already:
            continue
        block = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            block["matcher"] = matcher
        arr.append(block)
        added += 1

# Write back atomically, keeping a one-time-ish backup.
os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
if os.path.exists(SETTINGS) and not os.path.exists(SETTINGS + ".pagr.bak"):
    try:
        with open(SETTINGS, encoding="utf-8") as a, open(SETTINGS + ".pagr.bak", "w", encoding="utf-8") as b:
            b.write(a.read())
    except Exception:
        pass
tmp = SETTINGS + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, SETTINGS)

# register the machine so it appears in the dashboard's Machines view right away
try:
    import urllib.request
    req = urllib.request.Request(
        URL.rstrip("/") + "/api/enroll",
        data=json.dumps({"machine": MACHINE}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    urllib.request.urlopen(req, timeout=5).read()
except Exception:
    pass

print(f"pagr: settings updated ({added} hook(s) added) machine={MACHINE} -> {SETTINGS}")
