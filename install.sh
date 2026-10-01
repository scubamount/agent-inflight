#!/bin/sh
# agent-inflight installer. Idempotent; safe to re-run. No root, no network.
#
#   ./install.sh                 install CLI + create the tracker + Hermes skill (if Hermes present)
#   ./install.sh --no-skill      CLI + tracker only
#   ./install.sh --hermes-cron   also copy the trim job script into <hermes-home>/scripts/
#   ./install.sh --no-plugin     skip the Hermes plugin symlink
#
# What it does:
#   1. Creates a private venv at <tracker dir>/inflight-state/venv with the
#      stdlib `venv` module (no pip download, no network: an unattended installer
#      may run this) and writes a launcher that pins `inflight` to
#      it, so every harness runs the same interpreter. Backend plugins are
#      pip-installed into that venv by the user (`<venv>/bin/python -m pip
#      install <pkg>`), never into the system Python. If `venv` is
#      unavailable the launcher uses python3 and `inflight check` says so.
#      Symlinks $INFLIGHT_BIN_DIR/inflight (default ~/.local/bin) to the
#      launcher. The launcher execs this checkout, so `git pull` is the
#      whole update.
#   2. Creates the tracker file if missing (`inflight init`); never overwrites.
#   3. If a Hermes home exists, copies skills/agent/inflight-tracker into
#      <hermes-home>/skills/agent/ (overwrites when it differs: this repo is
#      the source of truth for that skill; edits made live are lost here, so
#      port them back first).
#   4. If a Hermes home exists, symlinks adapters/hermes/plugin to
#      <hermes-home>/plugins/agent-inflight (runtime session tags for hand
#      edits). Discovery only: Hermes loads it once `plugins.enabled` lists
#      `agent-inflight` (add it to config.yaml, or let your config manager do it).
#   5. Proves it: runs `inflight check` through the installed symlink.
#
#   6. Re-run safe: an existing healthy venv and launcher are left as is.
#
# Env: INFLIGHT_BIN_DIR, INFLIGHT_HOME, INFLIGHT_FILE, HERMES_HOME.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${INFLIGHT_BIN_DIR:-$HOME/.local/bin}"
SKILL=1
CRON=0
PLUGIN=1
for arg in "$@"; do
    case "$arg" in
        --no-skill) SKILL=0 ;;
        --no-plugin) PLUGIN=0 ;;
        --hermes-cron) CRON=1 ;;
        *) echo "  !! unknown option: $arg" >&2; exit 2 ;;
    esac
done

command -v python3 >/dev/null 2>&1 || { echo "  !! python3 not found (need >= 3.9)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "  !! python3 >= 3.9 required" >&2; exit 1; }

chmod +x "$HERE/bin/inflight"
TRACKER="$(python3 "$HERE/bin/inflight" path)"
STATE="$(dirname "$TRACKER")/inflight-state"
VENV="$STATE/venv"
mkdir -p "$STATE" && chmod 700 "$STATE"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    echo "  ok venv $VENV ($("$VENV/bin/python" -c 'import platform; print(platform.python_version())'))"
else
    rm -rf "$VENV"
    # ensurepip installs pip from wheels bundled with Python: offline. Some
    # distro Pythons ship venv without ensurepip; fall back to --without-pip.
    if python3 -m venv "$VENV" >/dev/null 2>&1 || { rm -rf "$VENV"; python3 -m venv --without-pip "$VENV" >/dev/null 2>&1; }; then
        echo "  -> created venv $VENV"
    else
        rm -rf "$VENV"
        echo "  !! python3 -m venv failed; launcher uses python3 (inflight check will warn)" >&2
    fi
fi
LAUNCHER="$STATE/inflight"
tmp="$STATE/.inflight.$$"
cat > "$tmp" <<EOF
#!/bin/sh
# Written by agent-inflight install.sh; re-run it to regenerate.
PY="$VENV/bin/python"
[ -x "\$PY" ] || { echo "inflight: private venv missing (\$PY); using python3, backend plugins unavailable. Re-run install.sh." >&2; PY=python3; }
exec "\$PY" "$HERE/bin/inflight" "\$@"
EOF
chmod 755 "$tmp"
if cmp -s "$tmp" "$LAUNCHER" 2>/dev/null; then rm -f "$tmp"; echo "  ok launcher $LAUNCHER (up to date)"
else mv "$tmp" "$LAUNCHER"; echo "  -> wrote launcher $LAUNCHER"; fi

mkdir -p "$BIN_DIR"
link="$BIN_DIR/inflight"
if [ -L "$link" ] && [ "$(readlink "$link")" = "$LAUNCHER" ]; then
    echo "  ok $link (up to date)"
elif [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "  !! $link exists and is not a symlink; move it aside and re-run" >&2
    exit 1
else
    ln -sfn "$LAUNCHER" "$link"
    echo "  -> linked $link -> $LAUNCHER"
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

if [ "$PLUGIN" = 1 ]; then
    hh="${HERMES_HOME:-$HOME/.hermes}"
    if [ -d "$hh" ]; then
        src="$HERE/adapters/hermes/plugin"
        dst="$hh/plugins/agent-inflight"
        if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
            echo "  ok plugin $dst (up to date)"
        elif [ -e "$dst" ] && [ ! -L "$dst" ]; then
            echo "  !! $dst exists and is not a symlink; move it aside and re-run" >&2
            exit 1
        else
            mkdir -p "$hh/plugins"
            ln -sfn "$src" "$dst"
            echo "  -> linked plugin $dst -> $src"
        fi
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
