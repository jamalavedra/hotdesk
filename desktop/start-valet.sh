#!/bin/bash
set -euo pipefail
state=/home/cua/.valet
if [[ "$(id -u)" = "0" ]]; then
    # Supervisor runs this as root so a pre-existing volume can be re-owned;
    # drop to the dedicated valet user before touching any state.
    install -d -o valet -g valet -m 700 "$state"
    chown -R valet:valet "$state"
    exec setpriv --reuid=valet --regid=valet --clear-groups --reset-env \
        env HOME=/home/cua/.valet "$0" "$@"
fi
mkdir -p "$state"
chmod 700 "$state"
if [[ ! -s "$state/master.key" ]]; then
    # Both supervised programs start together; mv -n keeps whichever key lands first.
    temporary="$(mktemp "$state/.master.key.XXXXXX")"
    head -c 32 /dev/urandom | base64 -w0 >"$temporary"
    mv -n "$temporary" "$state/master.key"
    rm -f "$temporary"
fi
VALET_MASTER_PASSWORD="$(<"$state/master.key")"
export VALET_MASTER_PASSWORD
export VALET_DB="$state/valet.db"
export VALET_LISTEN=127.0.0.1:14400
export VALET_ADDR=http://127.0.0.1:14400
export VALET_CDP_URL=http://127.0.0.1:9222
case "${1:-}" in
server)
    exec /usr/local/bin/valet server
    ;;
mcp)
    until curl -fs "$VALET_ADDR/healthz" >/dev/null; do sleep 1; done
    token="$state/hotdesk-agent.token"
    if [[ ! -s "$token" ]]; then
        temporary="$(mktemp "$state/.hotdesk-agent.token.XXXXXX")"
        /usr/local/bin/valet agent create hotdesk >"$temporary"
        mv "$temporary" "$token"
    fi
    VALET_AGENT_TOKEN="$(<"$token")"
    export VALET_AGENT_TOKEN
    exec /usr/local/bin/valet mcp --http 127.0.0.1:14401
    ;;
cli)
    shift
    until curl -fs "$VALET_ADDR/healthz" >/dev/null; do sleep 1; done
    exec /usr/local/bin/valet "$@"
    ;;
*)
    echo "usage: start-valet.sh server|mcp|cli <valet args>" >&2
    exit 2
    ;;
esac
