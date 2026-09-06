import asyncio
import base64
import fcntl
import hashlib
import json
import os
import secrets
from functools import partial

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hotdesk.connection import ensure_manager
from hotdesk.server import save_json
from hotdesk.state import validate_agent


def render_result(result, directory):
    content = []
    for block in result.content:
        item = block.model_dump(mode="json", exclude_none=True)
        if block.type == "image":
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(block.mimeType, ".img")
            path = directory / (secrets.token_hex(12) + suffix)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(base64.b64decode(block.data, validate=True))
            item = {"type": "image", "path": str(path), "mimeType": block.mimeType}
        content.append(item)
    return {
        "content": content,
        "structuredContent": result.structured_content,
        "isError": result.is_error,
    }


async def execute(config, session, workspace, action, tool=None, arguments=None, task=""):
    validate_agent(session, task)
    if action not in {"tools", "call", "release", "status"}:
        raise ValueError("Unknown agent action")
    if action == "call" and (
        not isinstance(tool, str) or not tool or not isinstance(arguments, dict)
    ):
        raise ValueError("A tool name and JSON object are required")
    directory = config.state_dir / "agent-sessions"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    path = directory / (key + ".json")
    with (directory / (key + ".lock")).open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "This agent session already has a command running. Wait for it to finish."
            ) from None
        saved = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(saved, dict):
            raise ValueError("Invalid agent session file")
        workspace = workspace or saved.get("workspace")
        if workspace is None and len(config.desktops) == 1:
            workspace = next(iter(config.desktops))
        if workspace not in config.desktops:
            raise ValueError("Choose --workspace from: " + ", ".join(config.desktops))
        if saved and saved.get("workspace") != workspace:
            raise ValueError(
                "This session belongs to another workspace. Release it or choose a new session name."
            )
        if action == "release" and not saved:
            return {"released": False, "isError": False}
        connection = await asyncio.to_thread(ensure_manager, config)
        identity = hashlib.sha256((connection["url"] + connection["token"]).encode()).hexdigest()
        async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
            headers = {"Authorization": "Bearer " + connection["token"]}
            if action == "status":
                response = await http.get(connection["url"] + "/api/desktops", headers=headers)
                response.raise_for_status()
                return {
                    "workspace": next(row for row in response.json() if row["name"] == workspace)
                }
            if saved.get("manager") != identity:
                if action == "release":
                    path.unlink(missing_ok=True)
                    return {"released": False, "isError": False}
                response = await http.post(
                    connection["url"] + "/api/agents",
                    headers=headers,
                    json={"workspace": workspace, "owner": session, "task": task},
                )
                response.raise_for_status()
                saved = {
                    "session": session,
                    "workspace": workspace,
                    "manager": identity,
                    "token": response.json()["token"],
                }
                save_json(path, saved)
        transport = StreamableHttpTransport(
            connection["url"] + "/mcp/" + workspace + "/",
            headers={"Authorization": "Bearer " + saved["token"]},
            httpx_client_factory=partial(
                httpx.AsyncClient, trust_env=False, timeout=httpx.Timeout(30, read=180)
            ),
        )
        async with Client(transport) as client:
            if action == "tools":
                tools = await client.list_tools()
                if tool:
                    tools = [entry for entry in tools if entry.name == tool]
                    if not tools:
                        raise ValueError(
                            "Tool not found. List tools again after acquiring the workspace."
                        )
                return {
                    "tools": [
                        {
                            "name": entry.name,
                            "description": entry.description,
                            **({"inputSchema": entry.inputSchema} if tool else {}),
                        }
                        for entry in tools
                    ]
                }
            if action == "call" and not tool.startswith("workspace_"):
                acquired = await client.call_tool(
                    "workspace_acquire", {"seconds": 300}, raise_on_error=False
                )
                if acquired.is_error:
                    return render_result(acquired, directory / "artifacts")
            result = await client.call_tool(
                "workspace_release" if action == "release" else tool,
                {} if action == "release" else arguments,
                raise_on_error=False,
            )
            if not result.is_error and (action == "release" or tool == "workspace_release"):
                path.unlink(missing_ok=True)
            return render_result(result, directory / "artifacts")
