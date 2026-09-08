"""Exercise a disposable Docker project: uv run python tests/e2e.py."""

import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from websockets.asyncio.client import connect

from hotdesk.config import load_config
from hotdesk.runtime import Runtime


def result_text(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


def jpeg_size(data):
    assert data[:2] == b"\xff\xd8"
    offset = 2
    while offset < len(data):
        assert data[offset] == 255
        marker = data[offset + 1]
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if marker in (192, 194):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    raise AssertionError("JPEG has no frame dimensions")


async def viewer_pointer(runtime, name, read_only, accepts_input):
    row = next(row for row in await asyncio.to_thread(runtime.status) if row["name"] == name)
    await asyncio.to_thread(
        runtime._run,
        ["exec", "--user", "cua", row["container_id"], "xdotool", "mousemove", "100", "100"],
    )
    url = row["viewer_url"].replace("http:", "ws:") + "/websockify"
    async with connect(
        url,
        additional_headers={"Authorization": "Bearer " + runtime.gateway_token(name)},
        subprotocols=["binary"],
    ) as ws:
        buffered = bytearray()

        async def read(length):
            while len(buffered) < length:
                buffered.extend(await asyncio.wait_for(ws.recv(), 10))
            result = bytes(buffered[:length])
            del buffered[:length]
            return result

        assert (await read(12)).startswith(b"RFB ")
        await ws.send(b"RFB 003.008\n")
        count = (await read(1))[0]
        assert 2 in await read(count)
        await ws.send(b"\x02")
        challenge = await read(16)
        password = runtime.viewer_password(name, read_only=read_only).encode()
        key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in password)
        encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
        await ws.send(encryptor.update(challenge) + encryptor.finalize())
        assert await read(4) == b"\x00" * 4
        await ws.send(b"\x01")
        header = await read(24)
        await read(struct.unpack(">I", header[20:24])[0])
        await ws.send(struct.pack(">BBHH", 5, 0, 250, 250))
        await asyncio.sleep(0.2)
        output = await asyncio.to_thread(
            runtime._run,
            [
                "exec",
                "--user",
                "cua",
                row["container_id"],
                "xdotool",
                "getmouselocation",
                "--shell",
            ],
            text=True,
        )
        position = dict(line.split("=", 1) for line in output.strip().splitlines())
        expected = "250" if accepts_input else "100"
        assert position["X"] == position["Y"] == expected, position


def failure_diagnostics(runtime):
    try:
        with runtime._client() as docker:
            containers = docker.containers.list(
                all=True, filters={"label": f"com.docker.compose.project={runtime.config.project}"}
            )
            for container in containers:
                print("Failure diagnostics: " + container.name, file=sys.stderr)
                print(json.dumps(container.attrs.get("State", {})), file=sys.stderr)
                print(container.logs(tail=80).decode(errors="replace"), file=sys.stderr)
                if container.status != "running":
                    continue
                try:
                    stats = container.stats(stream=False, one_shot=True)
                    print(
                        json.dumps({"memory_stats": stats.get("memory_stats", {})}), file=sys.stderr
                    )
                    diagnostic = container.exec_run(
                        [
                            "sh",
                            "-c",
                            'supervisorctl status; cat /sys/fs/cgroup/memory.events; stat /home/cua/.cache /home/cua/.cache/ms-playwright; for file in /var/log/supervisor/*playwright* /var/log/supervisor/*computer* /tmp/*playwright*.log /tmp/*computer*.log; do if test -f "$file"; then tail -80 "$file"; fi; done',
                        ]
                    )
                    print(diagnostic.output.decode(errors="replace"), file=sys.stderr)
                except Exception as error:
                    print("Guest diagnostics unavailable: " + str(error), file=sys.stderr)
    except Exception as error:
        print("Docker diagnostics unavailable: " + str(error), file=sys.stderr)


async def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="hotdesk-e2e-") as directory:
        config_path = Path(directory) / "hotdesk.toml"
        project = "hotdesk-e2e-" + os.urandom(4).hex()
        image_setting = (
            f"image={json.dumps(os.environ['HOTDESK_TEST_IMAGE'])}\n"
            if os.environ.get("HOTDESK_TEST_IMAGE")
            else ""
        )
        config_path.write_text(
            f'[project]\nname="{project}"\n{image_setting}'
            '[profiles.alpha]\ncpus=2\nmemory="2g"\n'
            '[profiles.beta]\ncpus=2\nmemory="2g"\n'
            '[desktops.alpha]\nprofile="alpha"\n'
            '[desktops.beta]\nprofile="beta"\n'
        )
        config = load_config(config_path)
        runtime = Runtime(config)
        service = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=240, trust_env=False
        ) as http:

            async def start_manager():
                nonlocal service
                with (Path(directory) / "service.log").open("a") as log:
                    service = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "hotdesk",
                            "--config",
                            str(config_path),
                            "serve",
                            "--port",
                            str(port),
                        ],
                        stdout=log,
                        stderr=log,
                    )
                for _ in range(150):
                    assert service.poll() is None, (Path(directory) / "service.log").read_text()
                    try:
                        connection = json.loads((config.state_dir / "connection.json").read_text())
                        http.headers["Authorization"] = "Bearer " + connection["token"]
                        response = await http.get("/api/health")
                        if response.status_code == 200:
                            return
                    except (OSError, ValueError, httpx.RequestError):
                        pass
                    await asyncio.sleep(0.2)
                raise AssertionError("Manager did not become ready")

            async def post(path, body):
                response = await http.post("/api/" + path, json=body)
                assert response.status_code == 200, response.text
                return response.json()

            async def register(name, owner):
                access = await post("agents", {"workspace": name, "owner": owner, "task": "E2E"})
                return Client(
                    StreamableHttpTransport(
                        access["url"], headers={"Authorization": "Bearer " + access["token"]}
                    )
                ), access

            try:
                print(f"Starting isolated project {project}", flush=True)
                await start_manager()
                await post("apply", {"build": False})
                rows = (await http.get("/api/desktops")).json()
                assert len(rows) == 2 and all(row["health"] == "healthy" for row in rows), rows
                print(
                    "Tested images: " + ", ".join(sorted({row["image_id"] for row in rows})),
                    flush=True,
                )
                denied = await http.get("/api/desktops", headers={"Authorization": "Bearer wrong"})
                assert denied.status_code == 401
                foreign = await http.get("/api/desktops", headers={"Origin": "https://example.com"})
                assert foreign.status_code == 403
                await post("session", {})
                observe = await http.get(f"/viewer/{rows[0]['name']}/?view_only=true")
                assert observe.status_code == 200
                print(
                    "PASS manager-mediated startup, two healthy desktops, authentication and viewer route",
                    flush=True,
                )
                async with AsyncExitStack() as stack:
                    clients = {}
                    for name in ("alpha", "beta"):
                        client, _ = await register(name, name)
                        clients[name] = await stack.enter_async_context(client)
                    competitor, _ = await register("alpha", "competitor")
                    competitor = await stack.enter_async_context(competitor)
                    races = await asyncio.gather(
                        clients["alpha"].call_tool("workspace_acquire", {}, raise_on_error=False),
                        competitor.call_tool("workspace_acquire", {}, raise_on_error=False),
                    )
                    assert sum(not result.is_error for result in races) == 1, races
                    if races[0].is_error:
                        await competitor.call_tool("workspace_release", {})
                        await clients["alpha"].call_tool("workspace_acquire", {})
                    await clients["beta"].call_tool("workspace_acquire", {})
                    refused = await http.post(
                        "/api/desktops/alpha/lifecycle", json={"action": "stop"}
                    )
                    assert refused.status_code == 409

                    async def call(desktop, tool, arguments=None, kind="computer"):
                        result = await clients[desktop].call_tool(
                            tool, arguments or {}, raise_on_error=False
                        )
                        assert not result.is_error, result_text(result)
                        return result

                    for name in clients:
                        tools = {tool.name for tool in await clients[name].list_tools()}
                        assert {
                            "computer_screenshot",
                            "browser_navigate",
                            "workspace_acquire",
                        } <= tools
                        shot = await call(name, "computer_screenshot")
                        image = next(block for block in shot.content if block.type == "image")
                        assert jpeg_size(base64.b64decode(image.data)) == (1280, 800)
                    busy = asyncio.create_task(
                        call(
                            "alpha",
                            "computer_run_command",
                            {"command": "sleep 6; printf parallel-complete"},
                        )
                    )
                    await asyncio.sleep(0.5)
                    await call("beta", "computer_get_screen_size")
                    assert not busy.done(), "Second workspace waited for the first"
                    assert "parallel-complete" in result_text(await busy)
                    print(
                        "PASS reservation race, busy lifecycle refusal, screenshots, parallel workspace tools",
                        flush=True,
                    )
                    marker = "hotdesk-persistent-" + project
                    await call(
                        "alpha",
                        "computer_run_command",
                        {"command": f"printf %s {marker} > /home/cua/hotdesk-e2e-marker"},
                    )
                    isolated = await call(
                        "beta",
                        "computer_run_command",
                        {"command": "test ! -e /home/cua/hotdesk-e2e-marker && echo isolated"},
                    )
                    assert "isolated" in result_text(isolated), isolated
                    html = '<!doctype html><title>Hot Desk E2E</title><h1>Hot Desk E2E</h1><input autofocus aria-label="Test input">'
                    await call(
                        "alpha",
                        "computer_file_write",
                        {"path": "/home/cua/hotdesk-e2e.html", "content": html},
                    )

                    async def start_fixture():
                        await asyncio.to_thread(
                            runtime._compose,
                            "exec",
                            "--detach",
                            "--user",
                            "cua",
                            "desktop-alpha",
                            "python",
                            "-m",
                            "http.server",
                            "8765",
                            "--bind",
                            "127.0.0.1",
                            "--directory",
                            "/home/cua",
                        )
                        ready = await asyncio.to_thread(
                            runtime._compose,
                            "exec",
                            "-T",
                            "--user",
                            "cua",
                            "desktop-alpha",
                            "curl",
                            "--fail",
                            "--silent",
                            "--show-error",
                            "--max-time",
                            "3",
                            "--retry",
                            "20",
                            "--retry-all-errors",
                            "--retry-delay",
                            "1",
                            "--retry-max-time",
                            "60",
                            "http://127.0.0.1:8765/hotdesk-e2e.html",
                            timeout=80,
                        )
                        assert "Hot Desk E2E" in ready, "Local browser fixture is not ready"

                    await start_fixture()
                    navigated = await call(
                        "alpha",
                        "browser_navigate",
                        {"url": "http://127.0.0.1:8765/hotdesk-e2e.html"},
                        "browser",
                    )
                    assert "Hot Desk E2E" in result_text(navigated), navigated
                    cookie_set = await call(
                        "alpha",
                        "browser_evaluate",
                        {
                            "function": "() => { document.cookie = 'hotdesk_cookie=persisted; max-age=3600; path=/'; document.cookie = 'hotdesk_session=restored; path=/'; return document.cookie; }"
                        },
                        "browser",
                    )
                    assert "hotdesk_cookie=persisted" in result_text(cookie_set), cookie_set
                    assert "hotdesk_session=restored" in result_text(cookie_set), cookie_set
                    await call(
                        "alpha",
                        "computer_run_command",
                        {
                            "command": "xdotool search --onlyvisible --class chromium windowactivate --sync"
                        },
                    )
                    position = await call(
                        "alpha",
                        "browser_evaluate",
                        {
                            "function": "() => { const r = document.querySelector('input').getBoundingClientRect(); return {x: Math.round(screenX + r.x + r.width / 2), y: Math.round(screenY + outerHeight - innerHeight + r.y + r.height / 2)}; }"
                        },
                        "browser",
                    )
                    coordinates = json.loads(
                        result_text(position).split("### Result\n", 1)[1].split("\n###", 1)[0]
                    )
                    await call("alpha", "computer_click", coordinates)
                    await call("alpha", "computer_type", {"text": "typed-by-hotdesk"})
                    typed = await call(
                        "alpha",
                        "browser_evaluate",
                        {"function": "() => document.querySelector('input').value"},
                        "browser",
                    )
                    assert "typed-by-hotdesk" in result_text(typed), typed
                    await call("alpha", "computer_click", {"x": 1000, "y": 700})
                    cursor = await call("alpha", "computer_get_cursor_position")
                    assert "1000" in result_text(cursor) and "700" in result_text(cursor), cursor
                    public = await call(
                        "alpha", "browser_navigate", {"url": "https://example.com"}, "browser"
                    )
                    assert "Example Domain" in result_text(public), public
                    print(
                        "PASS shell/files, profile isolation, local/public browser navigation, actual keyboard and pointer input",
                        flush=True,
                    )
                    takeover = await post("desktops/alpha/control", {"control": "human"})
                    assert takeover["state"] == "human"
                    await viewer_pointer(runtime, "alpha", False, True)
                    await viewer_pointer(runtime, "alpha", True, False)
                    blocked = await clients["alpha"].call_tool(
                        "computer_screenshot", {}, raise_on_error=False
                    )
                    assert blocked.is_error
                    await call("beta", "computer_get_screen_size")
                    await post("desktops/alpha/control", {"control": "agent"})
                    await viewer_pointer(runtime, "alpha", False, False)
                    await clients["alpha"].call_tool("workspace_acquire", {})
                    await call("alpha", "computer_get_screen_size")
                    for client in clients.values():
                        await client.call_tool("workspace_release", {})
                    await post("down", {})
                    await post("apply", {"build": False})
                    await clients["alpha"].call_tool("workspace_acquire", {})
                    persisted = await call(
                        "alpha", "computer_file_read", {"path": "/home/cua/hotdesk-e2e-marker"}
                    )
                    assert marker in result_text(persisted)
                    await start_fixture()
                    await call(
                        "alpha",
                        "browser_navigate",
                        {"url": "http://127.0.0.1:8765/hotdesk-e2e.html"},
                    )
                    cookie = await call(
                        "alpha", "browser_evaluate", {"function": "() => document.cookie"}
                    )
                    assert "hotdesk_cookie=persisted" in result_text(cookie)
                    assert "hotdesk_session=restored" in result_text(cookie)
                    await clients["alpha"].call_tool("workspace_release", {})
                    await post("desktops/alpha/lifecycle", {"action": "stop"})
                    archive = str(Path(directory) / "profile.tar")
                    manifest = await post("desktops/alpha/backup", {"path": archive})
                    assert manifest["image_id"].startswith("sha256:")
                    restored = await post("desktops/alpha/restore", {"path": archive})
                    assert restored
                    await post("desktops/alpha/lifecycle", {"action": "start"})
                    await clients["alpha"].call_tool("workspace_acquire", {})
                    persisted = await call(
                        "alpha", "computer_file_read", {"path": "/home/cua/hotdesk-e2e-marker"}
                    )
                    assert marker in result_text(persisted)
                    await start_fixture()
                    await call(
                        "alpha",
                        "browser_navigate",
                        {"url": "http://127.0.0.1:8765/hotdesk-e2e.html"},
                    )
                    cookie = await call(
                        "alpha", "browser_evaluate", {"function": "() => document.cookie"}
                    )
                    assert "hotdesk_cookie=persisted" in result_text(cookie)
                    assert "hotdesk_session=restored" in result_text(cookie)
                    await clients["alpha"].call_tool("workspace_release", {})
                    print(
                        "PASS takeover, profile/cookie recreation, stopped backup and fresh-volume restore",
                        flush=True,
                    )

                await post("desktops/alpha/lifecycle", {"action": "stop"})
                transport = StdioTransport(
                    sys.executable,
                    ["-m", "hotdesk", "--config", str(config_path), "mcp", "--workspace", "alpha"],
                    cwd=directory,
                    keep_alive=False,
                )
                async with Client(transport) as agent:
                    stopped_tools = await agent.list_tools()
                    assert all(t.name.startswith("workspace_") for t in stopped_tools)
                    await agent.call_tool("workspace_acquire")
                    schemas = await agent.list_tools()
                    assert next(t for t in schemas if t.name == "computer_screenshot").inputSchema
                    screenshot = await agent.call_tool("computer_screenshot")
                    image = next(block for block in screenshot.content if block.type == "image")
                    assert len(base64.b64decode(image.data)) > 1000
                    tabs = await agent.call_tool("browser_tabs", {"action": "list"})
                    assert tabs.content and not tabs.is_error
                    await agent.call_tool(
                        "browser_evaluate",
                        {"function": "() => { document.body.dataset.hotdeskMcp = 'verified'; }"},
                    )
                    observed = await agent.call_tool(
                        "browser_evaluate", {"function": "() => document.body.dataset.hotdeskMcp"}
                    )
                    assert "verified" in result_text(observed)
                    await agent.call_tool("workspace_release")
                    # Leave a reservation to verify graceful stdio disconnect releases it.
                    await agent.call_tool("computer_screenshot")
                rows = (await http.get("/api/desktops")).json()
                assert next(row for row in rows if row["name"] == "alpha")["state"] == "idle"
                print(
                    "PASS MCP stdio outside checkout, tool schemas, inline screenshot and disconnect release",
                    flush=True,
                )

                await post("desktops/beta/control", {"control": "human"})
                crashing, old_access = await register("alpha", "crash-test")
                async with crashing:
                    await crashing.call_tool("workspace_acquire", {})
                    task = asyncio.create_task(
                        crashing.call_tool(
                            "computer_run_command",
                            {"command": "sleep 20; printf unknown-complete"},
                            raise_on_error=False,
                        )
                    )
                    for _ in range(100):
                        history = (await http.get("/api/events")).json()
                        if any(
                            operation["workspace"] == "alpha" and operation["outcome"] == "running"
                            for operation in history["operations"]
                        ):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise AssertionError("No active operation recorded")
                    service.kill()
                    await asyncio.to_thread(service.wait, timeout=10)
                    try:
                        await asyncio.wait_for(task, timeout=10)
                    except Exception:
                        pass
                await start_manager()
                recovered_rows = {
                    row["name"]: row for row in (await http.get("/api/desktops")).json()
                }
                assert recovered_rows["alpha"]["state"] == "recovery", recovered_rows
                assert recovered_rows["beta"]["state"] == "human", recovered_rows
                old_response = await http.post(
                    old_access["url"],
                    headers={"Authorization": "Bearer " + old_access["token"]},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                assert old_response.status_code in (401, 403)
                await post("desktops/alpha/lifecycle", {"action": "recover"})
                await post("desktops/beta/control", {"control": "agent"})
                fresh, _ = await register("alpha", "after-recovery")
                async with fresh:
                    await fresh.call_tool("workspace_acquire", {})
                    result = await fresh.call_tool("computer_get_screen_size", {})
                    assert "1280" in result_text(result)
                    await fresh.call_tool("workspace_release", {})
                print(
                    "PASS manager crash reconciliation, unknown outcome recovery, human state persistence and credential revocation",
                    flush=True,
                )
                if os.environ.get("HOTDESK_BROWSER_CHECK") == "1":
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            "tests/browser_check.py",
                            "--config",
                            str(config_path),
                            "--workspace",
                            "alpha",
                        ],
                        timeout=120,
                    )
                    assert result.returncode == 0

                config_path.write_text(config_path.read_text().replace("beta", "gamma"))
                changed = await post("apply", {"build": False})
                assert {row["name"] for row in changed} == {"alpha", "gamma"}, changed
                assert all(row["health"] == "healthy" for row in changed), changed
                gamma, _ = await register("gamma", "new-workspace")
                async with gamma:
                    await gamma.call_tool("workspace_acquire", {})
                    shot = await gamma.call_tool("computer_screenshot", {})
                    assert any(block.type == "image" for block in shot.content)
                    await gamma.call_tool("workspace_release", {})
                removed = await http.post(
                    "/api/agents", json={"workspace": "beta", "owner": "removed"}
                )
                assert removed.status_code in (400, 404)
                capacity = (await http.get("/api/capacity")).json()
                assert f"{project}_home-beta" in capacity["retained_volumes"], capacity
                print(
                    "PASS live configuration add/remove, new MCP workspace, removed workspace refusal and retained home",
                    flush=True,
                )
            except BaseException:
                await asyncio.to_thread(failure_diagnostics, runtime)
                log_path = Path(directory) / "service.log"
                if log_path.exists():
                    print(log_path.read_text()[-12000:], file=sys.stderr)
                raise
            finally:
                if service and service.poll() is None:
                    service.terminate()
                    try:
                        await asyncio.to_thread(service.wait, timeout=10)
                    except subprocess.TimeoutExpired:
                        service.kill()
                        await asyncio.to_thread(service.wait)
                await asyncio.to_thread(
                    runtime._compose, "down", "--volumes", "--remove-orphans", timeout=180
                )
                with runtime._client() as docker:
                    for volume in docker.volumes.list(
                        filters={"label": f"io.hotdesk.config={config.provenance}"}
                    ):
                        volume.remove()
    print(
        f"PASS complete Docker E2E in {time.monotonic() - started:.1f}s; isolated resources removed",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
