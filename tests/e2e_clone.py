"""Test checkpoint clones in a disposable Docker project: uv run python tests/e2e_clone.py."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hotdesk.config import load_config
from hotdesk.runtime import Runtime


def text(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


async def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="hotdesk-clone-e2e-") as directory:
        path = Path(directory)
        config_path = path / "hotdesk.toml"
        project = "hotdesk-clone-e2e-" + os.urandom(4).hex()
        image = os.environ.get("HOTDESK_TEST_IMAGE", "hotdesk-desktop:0.1.0")
        config_path.write_text(
            f'[project]\nname="{project}"\nimage={json.dumps(image)}\nmax_running=2\n'
            '[profiles.source]\ncpus=2\nmemory="2g"\n'
            '[desktops.source]\nprofile="source"\n'
        )
        config = load_config(config_path)
        runtime = Runtime(config)
        process = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=300, trust_env=False
        ) as http:

            async def start_manager():
                nonlocal process
                with (path / "manager.log").open("a") as log:
                    process = subprocess.Popen(
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
                    assert process.poll() is None, (path / "manager.log").read_text()
                    try:
                        connection = json.loads((config.state_dir / "connection.json").read_text())
                        http.headers["Authorization"] = "Bearer " + connection["token"]
                        if (await http.get("/api/health")).status_code == 200:
                            return
                    except (OSError, ValueError, httpx.RequestError):
                        pass
                    await asyncio.sleep(0.2)
                raise AssertionError("Manager startup timed out")

            async def stop_manager():
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)

            async def post(route, body):
                response = await http.post("/api/" + route, json=body)
                assert response.status_code == 200, response.text
                return response.json()

            async def client(name, owner):
                access = await post(
                    "agents", {"workspace": name, "owner": owner, "task": "clone E2E"}
                )
                return Client(
                    StreamableHttpTransport(
                        access["url"], headers={"Authorization": "Bearer " + access["token"]}
                    )
                )

            async def command(agent, value):
                result = await agent.call_tool("computer_run_command", {"command": value})
                assert not result.is_error, text(result)
                return text(result)

            async def browser_fixture(agent):
                await command(
                    agent,
                    "python3 -c \"import subprocess; subprocess.Popen(['python3','-m','http.server','8765','--bind','127.0.0.1','--directory','/home/cua'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)\"; sleep 1",
                )
                await agent.call_tool("browser_navigate", {"url": "http://127.0.0.1:8765/"})

            try:
                print(f"Starting isolated project {project}", flush=True)
                await start_manager()
                await post("apply", {"build": False})
                async with await client("source", "source-owner") as source:
                    await source.call_tool("workspace_acquire", {})
                    async with await client("source", "competitor") as competitor:
                        busy = await competitor.call_tool(
                            "workspace_acquire", {}, raise_on_error=False
                        )
                        assert busy.is_error, busy
                        details = busy.structured_content
                        assert details["code"] == "WORKSPACE_BUSY", details
                        assert details["checkpoint"]["available"] is False, details
                        assert details["owner"] == "source-owner", details
                    cli = await asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            "-m",
                            "hotdesk",
                            "--config",
                            str(config_path),
                            "agent",
                            "cli-competitor",
                            "--workspace",
                            "source",
                            "call",
                            "computer_run_command",
                            '{"command":"touch /home/cua/should-not-exist"}',
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    assert cli.returncode == 1, cli.stdout + cli.stderr
                    cli_busy = json.loads(cli.stdout)
                    assert (
                        cli_busy["isError"]
                        and cli_busy["structuredContent"]["code"] == "WORKSPACE_BUSY"
                    ), cli_busy
                    assert "not-executed" in await command(
                        source, "test ! -e /home/cua/should-not-exist && echo not-executed"
                    )
                    await command(source, "printf checkpoint > /home/cua/clone-marker")
                    await browser_fixture(source)
                    cookie = await source.call_tool(
                        "browser_evaluate",
                        {
                            "function": "() => { document.cookie = 'clone_auth=checkpoint; max-age=3600; path=/'; return document.cookie; }"
                        },
                    )
                    assert "clone_auth=checkpoint" in text(cookie), text(cookie)
                    await source.call_tool("workspace_release", {})
                await post("desktops/source/lifecycle", {"action": "stop"})
                checkpoint = await post("desktops/source/checkpoint", {})
                checkpoint_id = checkpoint["id"]
                assert checkpoint["created_at"], checkpoint
                await post("desktops/source/lifecycle", {"action": "start"})
                async with await client("source", "source-owner") as source:
                    await source.call_tool("workspace_acquire", {})
                    await command(source, "printf newer > /home/cua/clone-marker")
                    async with await client("source", "competitor") as competitor:
                        busy = await competitor.call_tool(
                            "workspace_acquire", {}, raise_on_error=False
                        )
                        details = busy.structured_content
                        assert busy.is_error and details["code"] == "WORKSPACE_BUSY", details
                        assert details["checkpoint"]["id"] == checkpoint_id, details
                        assert details["checkpoint"]["available"] and details["ask_user"], details
                    rows = (await http.get("/api/desktops")).json()
                    assert len(rows) == 1, "Busy acquisition created a desktop"
                    denied = await http.post(
                        "/api/desktops/source/clone",
                        json={
                            "name": "branch",
                            "checkpoint_id": checkpoint_id,
                            "user_approved": False,
                        },
                    )
                    assert denied.status_code in (400, 409), denied.text
                    await post(
                        "desktops/source/clone",
                        {
                            "name": "branch",
                            "checkpoint_id": checkpoint_id,
                            "user_approved": True,
                        },
                    )
                    rows = (await http.get("/api/desktops")).json()
                    original = next(row for row in rows if row["name"] == "source")
                    assert (
                        original["state"] == "reserved" and original["owner"] == "source-owner"
                    ), original
                    clone_config = load_config(config_path)
                    clone_runtime = Runtime(clone_config)
                    volume = clone_runtime.volume_name("branch")
                    assert volume != runtime.volume_name("source")
                    async with await client("branch", "clone-owner") as clone:
                        await clone.call_tool("workspace_acquire", {})
                        await browser_fixture(clone)
                        cookie = await clone.call_tool(
                            "browser_evaluate", {"function": "() => document.cookie"}
                        )
                        assert "clone_auth=checkpoint" in text(cookie), text(cookie)
                        assert "checkpoint" in await command(clone, "cat /home/cua/clone-marker")
                        await command(clone, "printf clone > /home/cua/clone-marker")
                        assert "newer" in await command(source, "cat /home/cua/clone-marker")
                        slow = asyncio.create_task(command(source, "sleep 5; echo source-complete"))
                        await asyncio.sleep(0.3)
                        assert "clone" in await command(clone, "cat /home/cua/clone-marker")
                        assert not slow.done(), "Clone tool waited for source operation"
                        assert "source-complete" in await slow
                        await clone.call_tool("workspace_release", {})
                    await source.call_tool("workspace_release", {})
                print(
                    "PASS explicit approval, CLI busy error, cloned browser cookie, stale checkpoint, isolated writes and parallel tools",
                    flush=True,
                )
                await stop_manager()
                await start_manager()
                rows = (await http.get("/api/desktops")).json()
                assert {row["name"] for row in rows} == {"source", "branch"}, rows
                async with await client("branch", "after-restart") as clone:
                    await clone.call_tool("workspace_acquire", {})
                    assert "clone" in await command(clone, "cat /home/cua/clone-marker")
                    await clone.call_tool("workspace_release", {})
                denied = await http.post(
                    "/api/desktops/branch/discard", json={"outputs_saved": False}
                )
                assert denied.status_code in (400, 409), denied.text
                await post("desktops/branch/discard", {"outputs_saved": True})
                assert "branch" not in load_config(config_path).desktops
                with runtime._client() as docker:
                    assert not docker.volumes.list(filters={"name": volume})
                    assert not docker.containers.list(
                        all=True,
                        filters={
                            "label": [
                                f"com.docker.compose.project={project}",
                                "io.hotdesk.desktop=branch",
                            ]
                        },
                    )
                await stop_manager()
                assert "ERROR:" not in (path / "manager.log").read_text(), (
                    path / "manager.log"
                ).read_text()[-14000:]
                print(
                    "PASS manager restart preserves clone; acknowledged discard removes clone container and volume",
                    flush=True,
                )
            except BaseException:
                print((path / "manager.log").read_text()[-14000:], file=sys.stderr)
                raise
            finally:
                await stop_manager()
                with runtime._client() as docker:
                    for container in docker.containers.list(
                        all=True, filters={"label": f"com.docker.compose.project={project}"}
                    ):
                        container.remove(force=True)
                    for volume in docker.volumes.list(
                        filters={"label": f"io.hotdesk.config={config.provenance}"}
                    ):
                        volume.remove()
                    for network in docker.networks.list(
                        filters={"label": f"com.docker.compose.project={project}"}
                    ):
                        network.remove()
    print(
        f"PASS clone Docker E2E in {time.monotonic() - started:.1f}s; disposable resources removed",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
