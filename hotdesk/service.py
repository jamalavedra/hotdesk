import hashlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hotdesk.connection import pin_docker_context, read_connection


def _require_macos():
    if sys.platform != "darwin":
        raise RuntimeError(
            "Login startup is macOS-only. Use hotdesk serve with your process manager on Linux."
        )


def service_label(config):
    return "io.hotdesk.manager." + hashlib.sha256(str(config.path).encode()).hexdigest()[:24]


def service_path(config):
    return Path.home() / "Library" / "LaunchAgents" / (service_label(config) + ".plist")


def _target(config):
    return f"gui/{os.getuid()}/{service_label(config)}"


def _launchctl(*args, check=True, timeout=10):
    result = subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(
            f"launchctl {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _job(config, timeout=10):
    result = _launchctl("print", _target(config), check=False, timeout=timeout)
    if result.returncode:
        return False, None
    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    return True, int(match[1]) if match else None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            plistlib.dump(value, output)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _plist(config, connection=None):
    if not connection or not (connection.get("docker_context") or connection.get("docker_host")):
        pin_docker_context()
    config.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    config.state_dir.chmod(0o700)
    log = config.state_dir / "manager.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")}
    for name in (
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    if connection and connection.get("docker_context"):
        environment["DOCKER_CONTEXT"] = connection["docker_context"]
        environment.pop("DOCKER_HOST", None)
    elif connection and connection.get("docker_host"):
        environment["DOCKER_HOST"] = connection["docker_host"]
        environment.pop("DOCKER_CONTEXT", None)
    return {
        "Label": service_label(config),
        "ProgramArguments": [
            os.path.abspath(sys.executable),
            "-m",
            "hotdesk",
            "--config",
            str(config.path),
            "serve",
        ],
        "WorkingDirectory": str(Path(__file__).absolute().parent.parent),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "Umask": 0o077,
    }


def status(config):
    _require_macos()
    registered, pid = _job(config)
    diagnostics = []
    path = service_path(config)
    if path.exists():
        try:
            definition = plistlib.loads(path.read_bytes())
            executable = definition["ProgramArguments"][0]
            if not os.access(executable, os.X_OK):
                diagnostics.append(
                    "Installed Python is missing or not executable. Reinstall Hot Desk and run service install."
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            plistlib.InvalidFileException,
        ):
            diagnostics.append("Invalid login job. Run service uninstall, then service install.")
    try:
        connection = read_connection(config, allow_other_context=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        connection = None
        diagnostics.append(str(error))
    if registered and not connection:
        diagnostics.append(
            "Manager is not ready. Run hotdesk doctor and hotdesk manager-logs; check Docker and the installed Python path."
        )
    return {
        "label": service_label(config),
        "plist": str(path),
        "installed": path.exists(),
        "registered": registered,
        "managed_running": pid is not None,
        "manager_ready": connection is not None,
        "unmanaged_manager": connection is not None and connection.get("pid") != pid,
        "diagnostics": diagnostics,
    }


def install(config):
    _require_macos()
    connection = read_connection(config, allow_other_context=True)
    definition = _plist(config, connection)
    registered, _ = _job(config)
    path = service_path(config)
    if registered:
        # Existing launchd jobs retain their loaded arguments until the next login.
        _write(path, definition)
        result = status(config)
        result["note"] = "Registered job retained. Updated settings apply at next login."
        return result
    _write(path, dict(definition, RunAtLoad=connection is None))
    try:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    finally:
        _write(path, definition)
    return status(config)


def start_registered(config, timeout=10, port=None):
    if sys.platform != "darwin":
        return False
    deadline = time.monotonic() + timeout
    registered, pid = _job(config, timeout=timeout)
    if not registered:
        return False
    if port is not None:
        raise RuntimeError(
            "Login startup manages the port. Omit --port or uninstall the login service first."
        )
    if pid is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out checking the login job. Run hotdesk service status.")
        _launchctl("kickstart", _target(config), timeout=remaining)
    return True


def uninstall(config, stop_manager):
    _require_macos()
    registered, pid = _job(config)
    if pid is not None:
        connection = read_connection(config, allow_other_context=True)
        if connection is None or connection.get("pid") != pid:
            raise RuntimeError(
                "The managed process is not ready. Check manager-logs before uninstalling; its work cannot be checked safely."
            )
        stop_manager(config)
        deadline = time.monotonic() + 30
        while _job(config)[1] is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Manager is still stopping. Retry service uninstall after shutdown completes."
                )
            time.sleep(0.25)
    if registered:
        _launchctl("bootout", _target(config))
    service_path(config).unlink(missing_ok=True)
    return status(config)
