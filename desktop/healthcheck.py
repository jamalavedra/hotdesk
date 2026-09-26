import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

components = {}
try:
    components["x11"] = (
        "ready"
        if subprocess.run(
            ["xdpyinfo", "-display", ":1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).returncode
        == 0
        else "failed"
    )
except (OSError, subprocess.TimeoutExpired):
    components["x11"] = "failed"
probes = [
    ("computer", 8000, "/status", 200),
    ("chromium", 9222, "/json/version", 200),
    ("viewer", 6901, "/vnc.html", 200),
    ("browser", 8931, "/mcp", 400),
    ("gateway", 8001, "/health", 200),
]
if os.path.exists("/usr/local/bin/valet"):
    probes.append(("valet", 14400, "/healthz", 200))
for name, port, path, expected in probes:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except (OSError, TimeoutError):
        status = 0
    components[name] = "ready" if status == expected else "failed"
print(json.dumps(components))
# Valet is optional: a failed Valet must not take the computer and browser tools down with it.
sys.exit(0 if all(v == "ready" for k, v in components.items() if k != "valet") else 1)
