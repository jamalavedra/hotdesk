FROM trycua/xfce-cua:0.1.0@sha256:274eb636f5cf3fc58f705916ee72b7a701270b3877369d08533a385c5325be9b
ARG HOTDESK_VERSION=development
LABEL org.opencontainers.image.title="Hot Desk desktop" org.opencontainers.image.version="${HOTDESK_VERSION}"
ARG COMPUTER_SERVER_VERSION=0.3.45
ARG PLAYWRIGHT_MCP_VERSION=0.0.80
ARG NODE_VERSION=22.23.2
ARG PIP_VERSION=26.2.1
USER root
ENV VNC_RESOLUTION=1280x800

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && apt-get purge -y firefox-esr \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gnome-screenshot xclip xdotool x11-utils build-essential linux-libc-dev chromium chromium-sandbox \
    && /opt/venv/bin/python3 -m pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" \
    && /opt/venv/bin/python3 -m pip install --no-cache-dir "cua-computer-server==${COMPUTER_SERVER_VERSION}" \
    && /opt/venv/bin/python3 -m pip show cua-computer-server fastmcp >/dev/null \
    && test -u /usr/lib/chromium/chrome-sandbox \
    && apt-get purge -y build-essential linux-libc-dev && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN case "$(dpkg --print-architecture)" in amd64) arch=x64;; arm64) arch=arm64;; *) exit 1;; esac \
    && cd /tmp && archive="node-v${NODE_VERSION}-linux-${arch}.tar.gz" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${archive}" \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" | grep " ${archive}$" | sha256sum -c - \
    && tar -xzf "$archive" -C /usr/local --strip-components=1 --no-same-owner \
    && rm "$archive" \
    && /usr/local/bin/npm install -g --ignore-scripts "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

COPY start-computer.sh start-browser.sh start-vnc.sh /usr/local/bin/
COPY gateway.py /usr/local/bin/hotdesk-gateway.py
COPY healthcheck.py /usr/local/bin/hotdesk-healthcheck.py
COPY supervisor.conf /tmp/hotdesk-supervisor.conf
RUN chmod 755 /usr/local/bin/start-computer.sh /usr/local/bin/start-browser.sh /usr/local/bin/start-vnc.sh \
    && cat /tmp/hotdesk-supervisor.conf >> /etc/supervisor/supervisord.conf \
    && rm /tmp/hotdesk-supervisor.conf \
    && /usr/local/bin/node /usr/local/lib/node_modules/@playwright/mcp/cli.js --version \
    && bash -n /usr/local/bin/start-computer.sh \
    && bash -n /usr/local/bin/start-browser.sh \
    && bash -n /usr/local/bin/start-vnc.sh

RUN /opt/venv/bin/python3 -c 'import json, subprocess, sys; from importlib.metadata import distributions; print(json.dumps({"python": sys.version.split()[0], "python_packages": {d.metadata["Name"]: d.version for d in distributions()}, "debian_packages": subprocess.check_output(["dpkg-query", "-W", "chromium", "novnc", "tigervnc-standalone-server", "xfce4"], text=True).splitlines(), "node": subprocess.check_output(["/usr/local/bin/node", "--version"], text=True).strip(), "playwright_mcp": subprocess.check_output(["/usr/local/bin/node", "/usr/local/lib/node_modules/@playwright/mcp/cli.js", "--version"], text=True).strip()}, sort_keys=True))' > /etc/hotdesk-versions.json

HEALTHCHECK --interval=5s --timeout=10s --start-period=60s --retries=30 \
    CMD /opt/venv/bin/python3 /usr/local/bin/hotdesk-healthcheck.py
EXPOSE 8001
