#!/bin/sh
# Hermes cron job body: trim the tracker, stay silent unless it fails.
# install.sh copies this to <hermes-home>/scripts/inflight-trim.sh. Register once:
#   hermes cron create "0 9 * * *" --name inflight-trim \
#       --script inflight-trim.sh --no-agent --deliver local
# Empty stdout = no delivery (Hermes --no-agent contract), so a clean run is silent.
bin="$(command -v inflight 2>/dev/null || echo "$HOME/.local/bin/inflight")"
out="$("$bin" trim --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] || printf 'inflight trim failed (rc=%s):\n%s\n' "$rc" "$out"
exit 0
