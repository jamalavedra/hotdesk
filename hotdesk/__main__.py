import argparse
import asyncio
import errno
import json
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from collections import deque
from importlib.metadata import distributions, version
from pathlib import Path

import httpx

from hotdesk.config import load_config, render_compose
from hotdesk.connection import (
    ManagerRunning,
    check_port,
    ensure_manager,
    pin_docker_context,
    project_lock,
    read_connection,
    remove_connection,
    save_connection,
    select_config,
)
from hotdesk.runtime import Runtime


def request(connection, path, body=None):
    response = httpx.request(
        "POST" if body is not None else "GET",
        connection["url"] + "/api/" + path,
        headers={"Authorization": "Bearer " + connection["token"]},
        json=body,
        timeout=1800,
        trust_env=False,
    )
    data = response.json()
    if not response.is_success:
        raise RuntimeError(data.get("error", "Manager request failed"))
    return data


def open_workspace(connection, workspace, observe=False):
    rows = request(connection, "desktops")
    row = next((row for row in rows if row["name"] == workspace), None)
    if row is None:
        raise ValueError("Unknown workspace: " + workspace)
    if row["status"] != "running":
        request(connection, f"desktops/{workspace}/lifecycle", {"action": "start"})
    if not observe:
        request(connection, f"desktops/{workspace}/control", {"control": "human"})
    url = (
        connection["url"]
        + f"/viewer/{workspace}/?view_only="
        + ("true" if observe else "false")
        + "#token="
        + connection["token"]
    )
    if not webbrowser.open(url):
        raise RuntimeError(
            "Browser could not be opened. Set your default browser and retry hotdesk open "
            + workspace
            + ". Return control with hotdesk release "
            + workspace
            + "."
        )
    print(("Observing " if observe else "You have control of ") + workspace)


def stop_manager(config):
    connection = read_connection(config, allow_other_context=True)
    if not connection:
        return
    request(connection, "shutdown", {})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not (config.state_dir / "connection.json").exists():
            return
        time.sleep(0.25)
    raise RuntimeError("Manager is still stopping. Inspect hotdesk manager-logs before retrying.")


def main():
    parser = argparse.ArgumentParser(description="Local desktops for agents and people")
    parser.add_argument("--version", action="version", version=version("hotdesk"))
    parser.add_argument("--config", help="Path to desktop configuration")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help in {
        "plan": "Print generated Compose configuration",
        "down": "Remove containers, preserving homes",
        "manager-stop": "Stop the idle manager, preserving desktops",
        "doctor": "Check Docker and manager readiness",
        "status": "Show workspace status",
        "versions": "Report installed versions",
        "skill": "Print agent instructions for using Hot Desk directly",
    }.items():
        sub.add_parser(name, help=help)
    opening = sub.add_parser("open", help="Start the manager or open a workspace desktop")
    opening.add_argument("workspace", nargs="?")
    opening.add_argument("--observe", action="store_true", help="Open without taking control")
    opening.add_argument("--port", type=int)
    default = sub.add_parser("default", help="Remember a configuration for this user")
    default.add_argument("--config-path", required=True)
    logs = sub.add_parser("manager-logs", help="Show the private manager log tail")
    logs.add_argument("--lines", type=int, default=50)
    service_parser = sub.add_parser("service", help="Manage optional macOS login startup")
    service_parser.add_argument("action", choices=("install", "status", "uninstall"))
    for name in ("up", "apply"):
        command = sub.add_parser(name, help="Apply configuration through the manager")
        command.add_argument("--no-build", action="store_true")
    for name in ("start", "stop", "recover", "backup", "restore", "release", "checkpoint"):
        command = sub.add_parser(name, help=name.capitalize() + " a workspace")
        command.add_argument("workspace")
        if name in ("backup", "restore"):
            command.add_argument("path")
    clone = sub.add_parser("clone", help="Create an isolated desktop from a saved checkpoint")
    clone.add_argument("workspace", help="Source workspace")
    clone.add_argument("name", help="New workspace name")
    clone.add_argument("--checkpoint", required=True, help="Checkpoint ID approved by the user")
    clone.add_argument(
        "--user-approved",
        action="store_true",
        required=True,
        help="Confirm the user chose a clone after hearing its limitations",
    )
    discard = sub.add_parser("discard", help="Delete a clone and its local home")
    discard.add_argument("workspace")
    discard.add_argument(
        "--outputs-saved",
        action="store_true",
        required=True,
        help="Confirm needed outputs were saved elsewhere, or none are needed",
    )
    serve = sub.add_parser("serve", help="Run the local manager and agent gateway")
    serve.add_argument("--port", type=int)
    agent = sub.add_parser(
        "agent", help="Use a desktop through a named agent session; no MCP setup needed"
    )
    agent.add_argument("session", help="Unique name for this task; reuse across commands")
    agent.add_argument("--workspace", help="Workspace name, remembered for this session")
    agent.add_argument("--task", default="")
    actions = agent.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("tools", help="Discover tool names and argument schemas")
    listing.add_argument("tool", nargs="?")
    call = actions.add_parser("call", help="Call a tool; reserves the workspace automatically")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object, or - to read stdin")
    actions.add_parser("status", help="Read ownership and readiness")
    actions.add_parser("release", help="Release this task's workspace")
    args = parser.parse_args()
    try:
        if args.command == "skill":
            print((Path(__file__).parent / "skills/hotdesk/SKILL.md").read_text())
            return
        if args.command == "default":
            from hotdesk.server import save_json

            config = load_config(Path(args.config_path).expanduser())
            save_json(
                Path.home() / ".local/state/hotdesk/default.json", {"config": str(config.path)}
            )
            print("Default configuration: " + str(config.path))
            return
        config = load_config(select_config(args.config))
        if args.command == "agent":
            from hotdesk.agent import execute

            arguments = getattr(args, "arguments", None)
            if arguments is not None:
                arguments = json.loads(sys.stdin.read() if arguments == "-" else arguments)
            result = asyncio.run(
                execute(
                    config,
                    args.session,
                    args.workspace,
                    args.action,
                    tool=getattr(args, "tool", None),
                    arguments=arguments,
                    task=args.task,
                )
            )
            print(json.dumps(result, indent=2))
            if result.get("isError"):
                sys.exit(1)
            return
        if getattr(args, "port", None) is not None and not 1024 <= args.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        if args.command == "open":
            if args.observe and not args.workspace:
                raise ValueError("Choose a workspace with --observe")
            if args.workspace and args.workspace not in config.desktops:
                raise ValueError("Unknown workspace: " + args.workspace)
            connection = ensure_manager(config, args.port)
            if args.workspace:
                open_workspace(connection, args.workspace, args.observe)
            else:
                print("Hot Desk manager running at " + connection["url"])
            return
        if args.command == "manager-stop":
            stop_manager(config)
            print(
                "Manager stopped. Running desktops are unchanged; stop workspaces to free memory."
            )
            return
        if args.command == "manager-logs":
            if not 1 <= args.lines <= 1000:
                raise ValueError("lines must be between 1 and 1000")
            with (config.state_dir / "manager.log").open() as log:
                print("".join(deque(log, maxlen=args.lines)), end="")
            return
        if args.command == "service":
            from hotdesk import service

            result = (
                service.uninstall(config, stop_manager)
                if args.action == "uninstall"
                else getattr(service, args.action)(config)
            )
            print(json.dumps(result, indent=2))
            return
        runtime = Runtime(config)
        if args.command == "plan":
            print(json.dumps(render_compose(config), indent=2))
        elif args.command in {
            "up",
            "apply",
            "down",
            "start",
            "stop",
            "recover",
            "backup",
            "restore",
            "release",
            "checkpoint",
            "clone",
            "discard",
        }:
            connection = read_connection(config, allow_other_context=args.command == "release")
            if not connection:
                raise RuntimeError(
                    "Manager is not running. Run 'hotdesk open' with this configuration, then retry."
                )
            if args.command in {"up", "apply"}:
                print(
                    json.dumps(request(connection, "apply", {"build": not args.no_build}), indent=2)
                )
            elif args.command == "down":
                print(json.dumps(request(connection, "down", {}), indent=2))
            elif args.command in {"checkpoint", "clone", "discard"}:
                body = {}
                if args.command == "clone":
                    body = {
                        "name": args.name,
                        "checkpoint_id": args.checkpoint,
                        "user_approved": args.user_approved,
                    }
                elif args.command == "discard":
                    body = {"outputs_saved": args.outputs_saved}
                print(
                    json.dumps(
                        request(connection, f"desktops/{args.workspace}/{args.command}", body),
                        indent=2,
                    )
                )
            elif args.command == "release":
                print(
                    json.dumps(
                        request(
                            connection, f"desktops/{args.workspace}/control", {"control": "agent"}
                        ),
                        indent=2,
                    )
                )
            elif args.command in {"backup", "restore"}:
                print(
                    json.dumps(
                        request(
                            connection,
                            f"desktops/{args.workspace}/{args.command}",
                            {"path": str(Path(args.path).expanduser().resolve())},
                        ),
                        indent=2,
                    )
                )
            else:
                print(
                    json.dumps(
                        request(
                            connection,
                            f"desktops/{args.workspace}/lifecycle",
                            {"action": args.command},
                        ),
                        indent=2,
                    )
                )
        elif args.command == "status":
            connection = read_connection(config, allow_other_context=True)
            print(
                json.dumps(
                    request(connection, "desktops") if connection else runtime.status(), indent=2
                )
            )
        elif args.command == "doctor":
            checks = {"configuration": str(config.path), "project": config.project}
            for name, command in {
                "docker": ["docker", "info", "--format", "{{.ServerVersion}}"],
                "compose": ["docker", "compose", "version", "--short"],
                "context": ["docker", "context", "show"],
                "image": [
                    "docker",
                    "image",
                    "inspect",
                    next(iter(render_compose(config)["services"].values()))["image"],
                    "--format",
                    "{{.Id}}",
                ],
            }.items():
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                    checks[name] = (
                        result.stdout.strip()
                        if result.returncode == 0
                        else (
                            "Unavailable. Start Docker."
                            if name == "docker"
                            else "Unavailable. Run hotdesk up after starting the manager."
                            if name == "image"
                            else result.stderr.strip()
                        )
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    checks[name] = str(exc)
            try:
                connection = read_connection(config)
                checks["manager"] = (
                    connection["url"]
                    if connection
                    else "Not running or stale connection. Run hotdesk open."
                )
                checks["desktops"] = (
                    request(connection, "desktops") if connection else runtime.status()
                )
            except (OSError, RuntimeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
                checks["readiness"] = str(exc)
            print(json.dumps(checks, indent=2))
        elif args.command == "versions":
            report = {
                "hotdesk": version("hotdesk"),
                "python": sys.version.split()[0],
                "host_packages": {d.metadata["Name"]: d.version for d in distributions()},
            }
            image = next(iter(render_compose(config)["services"].values()))["image"]
            inspected = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20
            )
            if inspected.returncode:
                report["desktop"] = {
                    "image": image,
                    "error": inspected.stderr.strip() or "Docker image inspection failed",
                }
            else:
                info = json.loads(inspected.stdout)[0]
                manifest = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--entrypoint",
                        "cat",
                        info["Id"],
                        "/etc/hotdesk-versions.json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                report["desktop"] = {
                    "image": image,
                    "id": info["Id"],
                    "architecture": info["Architecture"],
                    "packages": json.loads(manifest.stdout) if manifest.returncode == 0 else None,
                }
                if manifest.returncode:
                    report["desktop"]["error"] = (
                        manifest.stderr.strip() or "Desktop version manifest could not be read"
                    )
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.command == "serve":
            import uvicorn

            from hotdesk.server import create_app

            connection = read_connection(config)
            if connection:
                check_port(connection, args.port)
                print("Hot Desk is already running at " + connection["url"])
                return
            pin_docker_context()
            with project_lock(config, wait=30):
                token = secrets.token_urlsafe(32)
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.bind(("127.0.0.1", args.port if args.port is not None else 7890))
                    except OSError as exc:
                        if args.port is not None or exc.errno != errno.EADDRINUSE:
                            raise
                        sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                    url = f"http://127.0.0.1:{port}"
                    sock.listen(128)
                    server = None

                    async def shutdown():
                        server.should_exit = True

                    app = create_app(config, token, port, shutdown=shutdown)
                    server = uvicorn.Server(
                        uvicorn.Config(app, log_level="warning", fd=sock.fileno())
                    )
                    connection = {"url": url, "token": token}
                    save_connection(config, connection)
                    print(f"Hot Desk running at {url}. Reopen with 'hotdesk open'.", flush=True)

                    try:
                        asyncio.run(server.serve())
                    finally:
                        remove_connection(config)
    except ManagerRunning as exc:
        try:
            check_port(exc.connection, getattr(args, "port", None))
            print("Hot Desk is already running at " + exc.connection["url"])
        except RuntimeError as error:
            print(f"hotdesk: {error}", file=sys.stderr)
            sys.exit(1)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        print(f"hotdesk: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
