#!/bin/bash
set -euo pipefail
export DISPLAY=:1
until xdpyinfo >/dev/null 2>&1; do sleep 1; done
mkdir -p "$HOME/.config/xfce4"
helpers="$HOME/.config/xfce4/helpers.rc"
touch "$helpers"
sed -i '/^WebBrowser=/d' "$helpers"
printf 'WebBrowser=chromium\n' >> "$helpers"
xdg-mime default chromium.desktop x-scheme-handler/http x-scheme-handler/https text/html
profile="$HOME/.config/chromium"
# Moving a profile to another desktop can leave its previous hostname in the lock.
if [[ -L "$profile/SingletonLock" && "$(readlink "$profile/SingletonLock")" != "$(hostname)-"* ]]; then
    rm -f "$profile/SingletonLock" "$profile/SingletonCookie" "$profile/SingletonSocket"
fi
close_browser() {
    # CDP closes Chromium cleanly so recent cookies reach the profile volume.
    timeout 5 /usr/local/bin/node -e '
        (async () => {
            const info = await (await fetch("http://127.0.0.1:9222/json/version")).json();
            const socket = new WebSocket(info.webSocketDebuggerUrl);
            socket.onopen = () => socket.send(JSON.stringify({id: 1, method: "Browser.close"}));
            socket.onclose = () => process.exit(0);
            socket.onerror = () => process.exit(1);
        })().catch(() => process.exit(1));
    ' || kill "$browser_pid" 2>/dev/null || true
    wait "$browser_pid" || true
    exit 0
}
trap close_browser TERM INT
chromium --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
    --no-first-run --no-default-browser-check --hide-crash-restore-bubble --password-store=basic \
    --restore-last-session &
browser_pid=$!
wait "$browser_pid"
