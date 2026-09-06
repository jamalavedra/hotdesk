import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def pin_docker_context(timeout=10):
    if not os.environ.get("DOCKER_CONTEXT") and not os.environ.get("DOCKER_HOST"):
        result = subprocess.run(
            ["docker", "context", "show"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        context = result.stdout.strip()
        if not context:
            raise RuntimeError("Docker did not report an active context")
        os.environ["DOCKER_CONTEXT"] = context


def project_dir(config, timeout=10):
    endpoint = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    if not endpoint:
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        endpoint = result.stdout.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme == "unix":
        endpoint = "unix://" + str(Path(parsed.path).resolve())
    key = hashlib.sha256(f"{endpoint}\n{config.project}".encode()).hexdigest()[:24]
    directory = Path.home() / ".local" / "state" / "hotdesk" / "projects" / key
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory


class ManagerRunning(RuntimeError):
    def __init__(self, connection):
        self.connection = connection
        super().__init__("Manager is already running")


@contextmanager
def project_lock(config, wait=0):
    config.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    config.state_dir.chmod(0o700)
    deadline = time.monotonic() + wait
    with ExitStack() as stack:
        for path in (project_dir(config) / "server.lock", config.state_dir / "server.lock"):
            lock = stack.enter_context(path.open("a"))
            path.chmod(0o600)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if wait:
                        connection = read_connection(
                            config, allow_other_context=True, deadline=deadline
                        )
                        if connection:
                            raise ManagerRunning(connection)
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "This Docker project or configuration already has a manager. "
                            "Use its original configuration and DOCKER_CONTEXT."
                        ) from None
                    time.sleep(0.25)
        yield


def _read_health(data, deadline):
    async def fetch():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Manager health deadline expired")
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    data["url"] + "/api/health",
                    headers={"Authorization": "Bearer " + data["token"]},
                )
                response.raise_for_status()
                return response.json()

    def run():
        return asyncio.run(fetch())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="hotdesk-health") as executor:
        return executor.submit(run).result()


def read_connection(config, allow_other_context=False, deadline=None):
    def remaining():
        if deadline is None:
            return 3
        return max(0.01, min(3, deadline - time.monotonic()))

    local = config.state_dir / "connection.json"
    # Reopening a known configuration should also work while Docker is unavailable.
    primary = None if allow_other_context else project_dir(config, remaining()) / "connection.json"
    paths = [local] if allow_other_context else [primary, local]
    for path in dict.fromkeys(paths):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            url = urlsplit(data["url"])
            if (
                url.scheme != "http"
                or url.hostname != "127.0.0.1"
                or not url.port
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
                or not isinstance(data["token"], str)
            ):
                continue
            health_deadline = time.monotonic() + remaining()
            if deadline is not None:
                health_deadline = min(health_deadline, deadline)
            health = _read_health(data, health_deadline)
            if not isinstance(health, dict):
                continue
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            continue
        if health.get("project") != config.project or health.get("config") != str(config.path):
            raise RuntimeError(
                "This Docker project has a manager using another configuration. Use its original configuration."
            )
        if path != primary and not allow_other_context:
            raise RuntimeError(
                "This configuration has a running manager on another Docker context. "
                f"Use DOCKER_CONTEXT={data.get('docker_context', '<original context>')} or run hotdesk open to view it."
            )
        return data
    return None


def save_connection(config, data):
    from hotdesk.server import save_json

    data = dict(
        data,
        docker_context=os.environ.get("DOCKER_CONTEXT", ""),
        pid=os.getpid(),
        docker_host=os.environ.get("DOCKER_HOST", ""),
    )
    save_json(project_dir(config) / "connection.json", data)
    save_json(config.state_dir / "connection.json", data)


def remove_connection(config):
    (project_dir(config) / "connection.json").unlink(missing_ok=True)
    (config.state_dir / "connection.json").unlink(missing_ok=True)


def select_config(path=None):
    if path is not None:
        return Path(path).expanduser()
    local = Path("hotdesk.toml")
    if local.exists() or local.is_symlink():
        return local
    default = Path.home() / ".local/state/hotdesk/default.json"
    if default.exists():
        value = json.loads(default.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("config"), str):
            raise ValueError("Invalid saved default configuration")
        return Path(value["config"])
    raise ValueError(
        "Choose a configuration with --config PATH or hotdesk default --config-path PATH"
    )


def check_port(connection, port):
    if port is not None and urlsplit(connection["url"]).port != port:
        raise RuntimeError("Manager already uses another port. Omit --port to reopen it.")
    return connection


def ensure_manager(config, port=None):
    from hotdesk.service import start_registered

    deadline = time.monotonic() + 30
    connection = read_connection(config, allow_other_context=True, deadline=deadline)
    if connection:
        return check_port(connection, port)
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.chmod(0o700)
    log_path = config.state_dir / "manager.log"
    with (config.state_dir / "launch.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Manager startup timed out. Inspect {log_path}; run hotdesk doctor."
                    )
                time.sleep(0.25)
        connection = read_connection(config, allow_other_context=True, deadline=deadline)
        if connection:
            return check_port(connection, port)
        process = None
        if not start_registered(config, port=port, timeout=max(0.01, deadline - time.monotonic())):
            pin_docker_context(timeout=max(0.01, deadline - time.monotonic()))
            command = [sys.executable, "-m", "hotdesk", "--config", str(config.path), "serve"]
            if port is not None:
                command += ["--port", str(port)]
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as log:
                process = subprocess.Popen(
                    command,
                    cwd=config.path.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
        while time.monotonic() < deadline:
            connection = read_connection(config, allow_other_context=True, deadline=deadline)
            if connection:
                return check_port(connection, port)
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    f"Manager exited with status {process.returncode}. Inspect {log_path}; run hotdesk doctor."
                )
            time.sleep(0.25)
    raise RuntimeError(f"Manager startup timed out. Inspect {log_path}; run hotdesk doctor.")
