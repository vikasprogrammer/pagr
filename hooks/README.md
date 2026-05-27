# Adding the pagr hook to a machine

This guide explains how to enroll a machine — local or remote — so its Claude Code
sessions show up on the pagr dashboard and trigger Telegram alerts.

- **Dashboard / server URL:** `https://pagr.sh`
- **Token:** the `PAGR_TOKEN` secret. On a machine that's already enrolled you
  can read it with:
  ```bash
  python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['env']['PAGR_TOKEN'])"
  ```
  (It also lives in the pod's env: `instapods env list pagr`.)

---

## Quickest: one-liner (run on the target machine)

SSH into the box (or run it locally) and paste — the pod serves the hook + the
installer, so nothing needs to be checked out on the target:

```bash
curl -fsSL https://pagr.sh/enroll.sh | PAGR_TOKEN=<PAGR_TOKEN> bash -s -- --machine NAME
```

`--machine NAME` is optional (defaults to the box's hostname). Requires `curl`,
`python3`, and outbound HTTPS. Re-running is safe (idempotent). Use the
`install.sh` methods below if you'd rather push *from* your Mac over SSH.

---

## What enrollment does

Running `install.sh` on a target machine:

1. Copies the hook client to `~/.claude/pagr-hook` (`chmod +x`).
2. Merges two things into that machine's `~/.claude/settings.json`, **without
   touching anything else** (a one-time backup is written to
   `~/.claude/settings.json.pagr.bak`):
   - an **`env`** block with `PAGR_URL`, `PAGR_TOKEN`, `PAGR_MACHINE`
   - a **`hooks`** block wiring `SessionStart`, `UserPromptSubmit`, `Stop`,
     `SessionEnd`, and `Notification` (`permission_prompt` / `idle_prompt`) to the hook
3. Is **idempotent** — re-running adds nothing new.

> ⚠️ Hooks attach to **new** Claude Code sessions only. Sessions already running
> when you enroll won't report until they start a new turn (and most pick it up
> automatically when Claude re-reads settings).

## Prerequisites (per target machine)

- Claude Code is installed and used there.
- `python3` is on `PATH` (the hook is pure standard library — no `pip install`).
- The machine has outbound HTTPS to the server URL. Verify with:
  ```bash
  curl -fsS https://pagr.sh/healthz && echo " OK"
  ```
- **Remote only:** key-based SSH access (the installer runs `ssh`/`scp`
  non-interactively — a password prompt would stall it).

---

## Local machine

From the repo root:

```bash
hooks/install.sh \
  --url   https://pagr.sh \
  --token <PAGR_TOKEN> \
  --machine my-mac          # optional; defaults to this host's hostname
```

Expected output:

```
→ enrolling local machine
pagr: settings updated (6 hook(s) added) machine=my-mac -> /Users/you/.claude/settings.json
✓ local machine enrolled
```

## Remote machine (over SSH)

Add `--ssh <target>`. The target can be an alias from your `~/.ssh/config`
(e.g. `vapps`) or a full `user@host`:

```bash
hooks/install.sh \
  --ssh   user@1.2.3.4 \
  --url   https://pagr.sh \
  --token <PAGR_TOKEN> \
  --machine prod-box        # optional; defaults to the remote's hostname
```

This `scp`s the hook to the remote's `~/.claude/pagr-hook` and runs the same
settings merge there over SSH. Repeat once per remote machine.

```
→ enrolling remote: user@1.2.3.4
pagr: settings updated (6 hook(s) added) machine=prod-box -> /home/user/.claude/settings.json
✓ remote user@1.2.3.4 enrolled
```

---

## Verify it worked

1. Start a **new** `claude` session on the enrolled machine.
2. It appears on the dashboard within a second or two; let it ask for a
   permission and you should get a Telegram ping.

To test the pipe without a real session, run the installed hook by hand (it reads
its config from the machine's `settings.json` env):

```bash
eval "$(python3 -c "import json,os;e=json.load(open(os.path.expanduser('~/.claude/settings.json')))['env'];print('export PAGR_URL=%s PAGR_TOKEN=%s PAGR_MACHINE=%s'%(e['PAGR_URL'],e['PAGR_TOKEN'],e.get('PAGR_MACHINE','')))")"
echo '{"session_id":"manual-test","cwd":"'"$PWD"'","message":"manual enrollment check"}' | ~/.claude/pagr-hook needs_input
# -> a "manual-test" row appears on the dashboard (and a Telegram ping)
```

## Update the hook later

After pulling a newer `pagr-hook`, just re-run the same `install.sh` command
(idempotent), or copy it directly:

```bash
cp hooks/pagr-hook ~/.claude/pagr-hook            # local
scp hooks/pagr-hook <target>:.claude/pagr-hook    # remote
```

## Remove / unenroll a machine

Quickest is to restore the backup written at enrollment:

```bash
mv ~/.claude/settings.json.pagr.bak ~/.claude/settings.json   # (discards settings changed since)
rm -f ~/.claude/pagr-hook
```

Or surgically strip only pagr's bits (keeps any later settings changes):

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p))
for k in ("PAGR_URL", "PAGR_TOKEN", "PAGR_MACHINE"):
    d.get("env", {}).pop(k, None)
for ev, arr in list(d.get("hooks", {}).items()):
    arr[:] = [b for b in arr
              if not any("pagr-hook" in h.get("command", "") for h in b.get("hooks", []))]
    if not arr:
        d["hooks"].pop(ev, None)
json.dump(d, open(p, "w"), indent=2)
print("unenrolled")
PY
rm -f ~/.claude/pagr-hook
```

---

## Reference

`install.sh` flags:

| Flag | Required | Meaning |
|------|----------|---------|
| `--url <url>` | yes | pagr server URL |
| `--token <secret>` | recommended | `PAGR_TOKEN` (sent as `Authorization: Bearer`) |
| `--machine <name>` | no | label shown in the **Machine** column (default: hostname) |
| `--ssh <target>` | no | enroll a remote host instead of this machine |

How config is stored on each machine (`~/.claude/settings.json`):

```jsonc
"env": {
  "PAGR_URL": "https://pagr.sh",
  "PAGR_TOKEN": "…",
  "PAGR_MACHINE": "my-mac"
},
"hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook start"  }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook prompt" }] }],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook stop"   }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook end"    }] }],
  "Notification": [
    { "matcher": "permission_prompt", "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook needs_input" }] },
    { "matcher": "idle_prompt",       "hooks": [{ "type": "command", "command": "/Users/you/.claude/pagr-hook idle"        }] }
  ]
}
```

## Troubleshooting

- **A machine isn't showing up.** Hooks only apply to sessions started *after*
  enrollment — start a fresh `claude`. Then check `curl …/healthz`, that
  `python3` exists, and that the token matches the server's `PAGR_TOKEN`.
- **Multiple rows for one machine.** Expected — each Claude session is tracked
  separately (keyed by `session_id`); the `#id` chip and title disambiguate them.
- **Remote install hangs.** SSH isn't key-based (it's waiting on a password), or
  `python3`/`~/.claude` isn't available on the remote.
- **No Telegram, but rows update.** Telegram is configured on the *server*, not
  per machine — set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` on the pod.
