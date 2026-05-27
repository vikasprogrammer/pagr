#!/usr/bin/env bash
# Enroll a machine into pagr: install the hook client and wire it into
# Claude Code's settings.json. Works locally or against a remote SSH host.
#
#   Local machine:
#     ./install.sh --url https://pagr.sh --token SECRET [--machine name]
#
#   Remote machine (over SSH):
#     ./install.sh --ssh user@host --url https://... --token SECRET [--machine name]
#
# Re-running is safe (idempotent). A backup of settings.json is kept once at
# ~/.claude/settings.json.pagr.bak on the target.
set -euo pipefail

URL="" TOKEN="" MACHINE="" SSH_HOST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url)     URL="$2"; shift 2 ;;
    --token)   TOKEN="$2"; shift 2 ;;
    --machine) MACHINE="$2"; shift 2 ;;
    --ssh)     SSH_HOST="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$URL" ]; then echo "error: --url is required" >&2; exit 1; fi

DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SRC="$DIR/pagr-hook"
MERGE_SRC="$DIR/merge_settings.py"
[ -f "$HOOK_SRC" ]  || { echo "missing $HOOK_SRC" >&2; exit 1; }
[ -f "$MERGE_SRC" ] || { echo "missing $MERGE_SRC" >&2; exit 1; }

if [ -n "$SSH_HOST" ]; then
  echo "→ enrolling remote: $SSH_HOST"
  ssh "$SSH_HOST" 'mkdir -p ~/.claude'
  scp -q "$HOOK_SRC" "$SSH_HOST:.claude/pagr-hook"
  ssh "$SSH_HOST" 'chmod +x ~/.claude/pagr-hook'
  # Run the merge on the remote, feeding the script over stdin.
  ssh "$SSH_HOST" "PAGR_URL='$URL' PAGR_TOKEN='$TOKEN' PAGR_MACHINE='$MACHINE' python3 -" < "$MERGE_SRC"
  echo "✓ remote $SSH_HOST enrolled"
else
  echo "→ enrolling local machine"
  mkdir -p ~/.claude
  cp "$HOOK_SRC" ~/.claude/pagr-hook
  chmod +x ~/.claude/pagr-hook
  PAGR_URL="$URL" PAGR_TOKEN="$TOKEN" PAGR_MACHINE="$MACHINE" python3 "$MERGE_SRC"
  echo "✓ local machine enrolled"
fi

echo "Done. New Claude Code sessions on this machine will report to $URL"
