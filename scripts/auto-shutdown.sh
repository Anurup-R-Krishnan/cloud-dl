#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${WATCHDOG_ENV_FILE:-/etc/cloud-dl/watchdog.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

IDLE_FILE="${IDLE_FILE:-/tmp/idle_since}"
THRESHOLD_SECONDS="${THRESHOLD_SECONDS:-1800}"
RPC_PORT="${RPC_PORT:-6800}"
RPC_SECRET="${RPC_SECRET:-}"
SHUTDOWN_CMD="${SHUTDOWN_CMD:-/sbin/shutdown}"

build_rpc_payload() {
    local method="$1"
    if [ -n "$RPC_SECRET" ]; then
        printf '{"jsonrpc":"2.0","id":"wd","method":"%s","params":["token:%s"]}' "$method" "$RPC_SECRET"
    else
        printf '{"jsonrpc":"2.0","id":"wd","method":"%s","params":[]}' "$method"
    fi
}

query_rpc_count() {
    local method="$1"
    local payload resp
    payload=$(build_rpc_payload "$method")
    resp=$(curl -sf -m 4 "http://127.0.0.1:${RPC_PORT}/jsonrpc" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null) || return 1
    printf '%s' "$resp" | jq -e '(.result | length)' 2>/dev/null || return 1
}

ACTIVE_JOBS=0
WAITING_JOBS=0
if ACTIVE_COUNT=$(query_rpc_count "aria2.tellActive"); then
    ACTIVE_JOBS="$ACTIVE_COUNT"
fi
if WAITING_COUNT=$(query_rpc_count "aria2.tellWaiting"); then
    WAITING_JOBS="$WAITING_COUNT"
fi

if [ "$ACTIVE_JOBS" -gt 0 ] || [ "$WAITING_JOBS" -gt 0 ]; then
    rm -f "$IDLE_FILE"
    exit 0
fi

if pgrep -x rsync >/dev/null 2>&1 || pgrep -x scp >/dev/null 2>&1; then
    rm -f "$IDLE_FILE"
    exit 0
fi

ACTIVE_SSH=$(ss -Htn state established '( sport = :22 )' 2>/dev/null | wc -l)
if [ "$ACTIVE_SSH" -gt 0 ]; then
    rm -f "$IDLE_FILE"
    exit 0
fi

NOW=$(date +%s)
if [ ! -f "$IDLE_FILE" ]; then
    printf '%s\n' "$NOW" > "$IDLE_FILE"
    exit 0
fi

IDLE_SINCE=$(cat "$IDLE_FILE")
DIFF=$((NOW - IDLE_SINCE))

if [ "$DIFF" -ge "$THRESHOLD_SECONDS" ]; then
    logger "Auto-shutdown: System idle for ${DIFF}s (threshold ${THRESHOLD_SECONDS}s). Shutting down."
    "$SHUTDOWN_CMD" -h now
fi
