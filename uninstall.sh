#!/bin/sh
# Remove what install.sh added. Leaves the tracker file and its archive alone:
# those are your data.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${INFLIGHT_BIN_DIR:-$HOME/.local/bin}"
link="$BIN_DIR/inflight"
STATE="$(dirname "$(python3 "$HERE/bin/inflight" path)")/inflight-state"
case "$(readlink "$link" 2>/dev/null)" in
    "$HERE/bin/inflight"|"$STATE/inflight") rm "$link"; echo "  removed $link" ;;
esac
[ -d "$STATE/venv" ] && { rm -rf "$STATE/venv"; echo "  removed $STATE/venv"; }
[ -f "$STATE/inflight" ] && { rm "$STATE/inflight"; echo "  removed $STATE/inflight"; }
dst="${HERMES_HOME:-$HOME/.hermes}/skills/agent/inflight-tracker"
[ -d "$dst" ] && { rm -rf "$dst"; echo "  removed $dst"; }
plug="${HERMES_HOME:-$HOME/.hermes}/plugins/agent-inflight"
if [ -L "$plug" ] && [ "$(readlink "$plug")" = "$HERE/adapters/hermes/plugin" ]; then
    rm "$plug"; echo "  removed $plug (also drop agent-inflight from plugins.enabled)"
fi
job="${HERMES_HOME:-$HOME/.hermes}/scripts/inflight-trim.sh"
[ -f "$job" ] && { rm "$job"; echo "  removed $job (also run: hermes cron remove inflight-trim)"; }
echo "  kept your data: $("$HERE/bin/inflight" path), its inflight-archive/, $STATE/sessions/"
