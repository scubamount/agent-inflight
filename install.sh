#!/bin/sh
# agent-inflight installer. Idempotent; safe to re-run. No root, no network.
#
#   ./install.sh                 install CLI + create the tracker + Hermes skill (if Hermes present)
#   ./install.sh --no-skill      CLI + tracker only
#   ./install.sh --hermes-cron   also copy the trim job script into <hermes-home>/scripts/
#
# What it does:
#   1. Symlinks bin/inflight into $INFLIGHT_BIN_DIR (default ~/.local/bin).
#      A symlink, so `git pull` in this checkout is the whole update.
#   2. Creates the tracker file if missing (`inflight init`); never overwrites.
#   3. If a Hermes home exists, copies skills/agent/inflight-tracker into
#      <hermes-home>/skills/agent/ (overwrites when it differs: this repo is
#      the source of truth for that skill; edits made live are lost here, so
#      port them back first).
#   4. Proves it: runs `inflight check` through the installed symlink.
#
# Env: INFLIGHT_BIN_DIR, INFLIGHT_HOME, HERMES_HOME.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${INFLIGHT_BIN_DIR:-$HOME/.local/bin}"
SKILL=1
CRON=0
for arg in "$@"; do
    case "$arg" in
        --no-skill) SKILL=0 ;;
        --hermes-cron) CRON=1 ;;
        *) echo "  !! unknown option: $arg" >&2; exit 2 ;;
    esac
done

command -v python3 >/dev/null 2>&1 || { echo "  !! python3 not found (need >= 3.9)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "  !! python3 >= 3.9 required" >&2; exit 1; }

mkdir -p "$BIN_DIR"
chmod +x "$HERE/bin/inflight"
link="$BIN_DIR/inflight"
if [ -L "$link" ] && [ "$(readlink "$link")" = "$HERE/bin/inflight" ]; then
    echo "  ok $link (up to date)"
elif [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "  !! $link exists and is not a symlink; move it aside and re-run" >&2
    exit 1
else
    ln -sfn "$HERE/bin/inflight" "$link"
    echo "  -> linked $link -> $HERE/bin/inflight"
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  note: $BIN_DIR is not on PATH; add it to your shell profile" ;;
esac

"$link" init

if [ "$SKILL" = 1 ]; then
    hh="${HERMES_HOME:-$HOME/.hermes}"
    if [ -d "$hh" ]; then
        src="$HERE/skills/agent/inflight-tracker"
        dst="$hh/skills/agent/inflight-tracker"
        if [ -d "$dst" ] && diff -rq "$src" "$dst" >/dev/null 2>&1; then
            echo "  ok skill $dst (up to date)"
        else
            mkdir -p "$hh/skills/agent"
            rm -rf "$dst"
            cp -R "$src" "$dst"
            echo "  -> installed skill $dst"
        fi
    else
        echo "  (no Hermes home at $hh; skill not installed — see adapters/generic/AGENTS-snippet.md)"
    fi
fi

if [ "$CRON" = 1 ]; then
    hh="${HERMES_HOME:-$HOME/.hermes}"
    if [ -d "$hh" ]; then
        mkdir -p "$hh/scripts"
        if cmp -s "$HERE/adapters/hermes/inflight-trim.sh" "$hh/scripts/inflight-trim.sh" 2>/dev/null; then
            echo "  ok cron script $hh/scripts/inflight-trim.sh (up to date)"
        else
            cp "$HERE/adapters/hermes/inflight-trim.sh" "$hh/scripts/inflight-trim.sh"
            chmod +x "$hh/scripts/inflight-trim.sh"
            echo "  -> installed cron script $hh/scripts/inflight-trim.sh"
        fi
        echo "     register once: hermes cron create \"0 9 * * *\" --name inflight-trim --script inflight-trim.sh --no-agent --deliver local"
    else
        echo "  !! --hermes-cron: no Hermes home at $hh" >&2; exit 1
    fi
fi

rc=0
"$link" check >/dev/null 2>&1 || rc=$?
case "$rc" in
    0) echo "  ok inflight check clean" ;;
    1) echo "  ok installed; 'inflight check' has findings (run it to see them)" ;;
    *) echo "  !! installed CLI failed to run (rc=$rc)" >&2; exit 1 ;;
esac
echo "  agent-inflight $(cat "$HERE/VERSION") installed; tracker at $("$link" path)"
