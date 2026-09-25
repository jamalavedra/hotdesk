import contextlib
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import docker

from hotdesk.config import Config, _name, memory_bytes, render_compose, with_clones


class CapacityError(ValueError):
    pass


def _write_private(path: Path, content: str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self._initial_environment = os.environ.copy()
        self._docker_environment = None
        self._environment_lock = threading.Lock()
        self._cpu_samples = {}

    def _secret(self, filename, length):
        directory = self.config.state_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / filename
        if not path.exists():
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as file:
                temporary = Path(file.name)
                try:
                    file.write(secrets.token_urlsafe(length))
                    file.flush()
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        pass
                finally:
                    temporary.unlink(missing_ok=True)
        path.chmod(0o600)
        value = path.read_text()
        if not re.fullmatch(r"[A-Za-z0-9_-]{%d}" % ((length * 8 + 5) // 6), value):
            raise RuntimeError(f"Invalid credential file: {path}")
        return value

    def viewer_password(self, name, read_only=False):
        self._workspace(name)
        return self._secret(
            ("viewer-read-password-" if read_only else "viewer-password-") + name, 6
        )

    def gateway_token(self, name):
        self._workspace(name)
        return self._secret(f"gateway-{name}", 32)

    def _workspace(self, name):
        if name not in self.config.desktops:
            raise ValueError(f"Unknown workspace: {name}")
        return self.config.desktops[name]["profile"]

    def _volume_overrides(self):
        path = self.config.state_dir / "volumes.json"
        if not path.exists():
            return {}
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", value)
            for key, value in values.items()
        ):
            raise RuntimeError("Invalid restored-volume mapping")
        return values

    def volume_name(self, name):
        profile = self._workspace(name)
        return self._volume_overrides().get(profile, f"{self.config.project}_home-{profile}")

    def write_compose(self) -> Path:
        rendered = render_compose(self.config)
        for service in rendered["services"].values():
            name = service["labels"]["io.hotdesk.desktop"]
            service["environment"].update(
                VNC_PW=self.viewer_password(name),
                VNC_VIEW_PW=self.viewer_password(name, read_only=True),
                HOTDESK_GATEWAY_TOKEN=self.gateway_token(name),
            )
        for profile, volume in self._volume_overrides().items():
            if profile in self.config.profiles:
                rendered["volumes"][f"home-{profile}"] = {"name": volume, "external": True}
        path = self.config.state_dir / "compose.json"
        _write_private(path, json.dumps(rendered, indent=2) + "\n")
        return path

    def _run(self, args, timeout=60, stream=False, **kwargs):
        try:
            result = subprocess.run(
                ["docker", *args],
                stdout=kwargs.pop("stdout", subprocess.PIPE),
                stderr=None if stream else subprocess.PIPE,
                timeout=timeout,
                env=self._environment(),
                **kwargs,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Docker is not installed or is not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Docker timed out after {timeout} seconds") from error
        if result.returncode:
            message = result.stderr or b"See Docker output for details"
            if isinstance(message, bytes):
                message = message.decode(errors="replace")
            raise RuntimeError(f"Docker failed: {message.strip()}")
        return result.stdout

    def _compose(self, *args, timeout=60, stream=False):
        return self._run(
            [
                "compose",
                "--project-name",
                self.config.project,
                "--file",
                str(self.write_compose()),
                *args,
            ],
            timeout=timeout,
            stream=stream,
            text=True,
        )

    def _environment(self):
        with self._environment_lock:
            if self._docker_environment is not None:
                return self._docker_environment
            environment = self._initial_environment.copy()
            if environment.get("DOCKER_CONTEXT") or not environment.get("DOCKER_HOST"):
                try:
                    context = json.loads(
                        subprocess.check_output(
                            ["docker", "context", "inspect"],
                            env=environment,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=10,
                        )
                    )[0]
                except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
                    raise RuntimeError(
                        "Cannot inspect the Docker context; check Docker installation and context"
                    ) from error
                endpoint = context["Endpoints"]["docker"]
                environment["DOCKER_HOST"] = endpoint["Host"]
                for key in (
                    "DOCKER_CONTEXT",
                    "DOCKER_TLS",
                    "DOCKER_TLS_VERIFY",
                    "DOCKER_CERT_PATH",
                ):
                    environment.pop(key, None)
                if context.get("TLSMaterial", {}).get("docker"):
                    environment["DOCKER_CERT_PATH"] = str(
                        Path(context["Storage"]["TLSPath"]) / "docker"
                    )
                    environment["DOCKER_TLS"] = "1"
                    environment["DOCKER_TLS_VERIFY"] = "" if endpoint.get("SkipTLSVerify") else "1"
            self._local_endpoint(environment["DOCKER_HOST"])
            url = urlsplit(environment["DOCKER_HOST"])
            if url.scheme == "unix":
                environment["DOCKER_HOST"] = "unix://" + str(Path(unquote(url.path)).resolve())
            self._docker_environment = environment
            return environment

    @contextlib.contextmanager
    def _client(self):
        try:
            client = docker.from_env(environment=self._environment(), timeout=10)
            with contextlib.closing(client):
                yield client
        except docker.errors.DockerException as error:
            raise RuntimeError(f"Cannot connect to Docker: {error}") from error

    @staticmethod
    def _local_endpoint(endpoint):
        url = urlsplit(endpoint)
        if url.scheme == "unix" or (
            url.scheme in {"tcp", "http", "https"}
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            return
        raise RuntimeError(
            "Hot Desk requires a local Docker socket or loopback TCP endpoint; remote Docker contexts are unsupported"
        )

    def _containers(self, client):
        containers = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={self.config.project}"}
        )
        expected_file = str(self.config.state_dir / "compose.json")
        for container in containers:
            labels = container.labels
            provenance = labels.get("io.hotdesk.config")
            legacy_file = labels.get("com.docker.compose.project.config_files")
            if provenance != self.config.provenance and not (
                not provenance and legacy_file == expected_file
            ):
                raise RuntimeError(
                    f"Docker project '{self.config.project}' belongs to another configuration; use a different project.name"
                )
        return containers

    def assert_project(self):
        with self._client() as client:
            self._containers(client)
            for volume in client.volumes.list(
                filters={"label": f"com.docker.compose.project={self.config.project}"}
            ):
                provenance = (volume.attrs.get("Labels") or {}).get("io.hotdesk.config")
                if provenance and provenance != self.config.provenance:
                    raise RuntimeError("Project volumes belong to another configuration")
                if not provenance and not (self.config.state_dir / "compose.json").is_file():
                    raise RuntimeError(
                        "Existing project volumes have no configuration label; use their original configuration or a new project.name"
                    )

    def validate_start(self, name=None):
        if name is not None:
            self._workspace(name)
        for clone_name, clone in self.config.clones.items():
            if (name is None or clone_name == name) and not clone["ready"]:
                raise ValueError(
                    f"Clone {clone_name} did not finish restoring; discard it and create a new clone"
                )
        self.assert_project()
        rows = self.status()
        capacity = self.capacity(rows)
        selected = [row for row in rows if name is None or row["name"] == name]
        additional = [row for row in selected if row["status"] != "running"]
        needed = sum(
            memory_bytes(self.config.profiles[row["profile"]]["memory"]) for row in additional
        )
        if capacity["running"] + len(additional) > capacity["max_running"]:
            raise CapacityError(
                "Workspace limit reached; stop a workspace or increase project.max_running"
            )
        projected = needed + sum(
            memory_bytes(self.config.profiles[row["profile"]]["memory"])
            if name is None or row["name"] == name
            else row["memory_limit"]
            for row in rows
            if row["status"] == "running"
        )
        if projected > capacity["memory_budget"]:
            raise CapacityError(
                "Memory budget exceeded; stop a workspace or increase Docker memory/project.memory_budget"
            )
        needed += sum(
            max(
                0,
                memory_bytes(self.config.profiles[row["profile"]]["memory"]) - row["memory_limit"],
            )
            for row in selected
            if row["status"] == "running"
        )
        if needed > capacity["docker_memory_available"]:
            raise CapacityError(
                "Docker has insufficient free container memory; stop another workspace first"
            )

    def up(self, build=True, name=None):
        self.validate_start(name)
        args = ["up", "--detach", "--wait", "--wait-timeout", "180"]
        if name is None:
            args.append("--remove-orphans")
        args.append("--build" if build and not self.config.image else "--no-build")
        if name is not None:
            args.append(f"desktop-{name}")
        self._compose(*args, timeout=1800, stream=True)
        not_ready = [
            row["name"]
            for row in self.status()
            if (name is None or row["name"] == name)
            and (row["status"] != "running" or row["health"] != "healthy")
        ]
        if not_ready:
            raise RuntimeError(
                "Workspaces did not become healthy: "
                + ", ".join(not_ready)
                + ". Check component health and available resources."
            )

    def stop(self, name):
        self._workspace(name)
        self.assert_project()
        self._compose("stop", f"desktop-{name}", timeout=180)

    def down(self):
        self.assert_project()
        self._compose("down", "--remove-orphans", timeout=180)

    def set_viewer_control(self, name, enabled):
        self._workspace(name)
        with self._client() as client:
            containers = self._containers(client)
            container = next(
                (
                    item
                    for item in containers
                    if item.labels.get("com.docker.compose.service") == f"desktop-{name}"
                ),
                None,
            )
            if container is None or container.attrs.get("State", {}).get("Status") != "running":
                raise RuntimeError("Start the workspace before changing viewer control")
            value = "1" if enabled else "0"
            self._run(
                [
                    "exec",
                    "--user",
                    "cua",
                    container.id,
                    "vncconfig",
                    "-display",
                    ":1",
                    "-set",
                    f"AcceptKeyEvents={value}",
                    f"AcceptPointerEvents={value}",
                    f"AcceptCutText={value}",
                ]
            )
            for parameter in ("AcceptKeyEvents", "AcceptPointerEvents", "AcceptCutText"):
                observed = self._run(
                    [
                        "exec",
                        "--user",
                        "cua",
                        container.id,
                        "vncconfig",
                        "-display",
                        ":1",
                        "-get",
                        parameter,
                    ],
                    text=True,
                ).strip()
                if observed != value:
                    raise RuntimeError(
                        f"Viewer input control failed for {parameter}; stop the workspace to prevent input"
                    )

    def status(self) -> list[dict]:
        desired = render_compose(self.config)["services"]
        with self._client() as client:
            observed = {
                container.labels.get("com.docker.compose.service"): container
                for container in self._containers(client)
            }
            rows = []
            for name, desktop in sorted(self.config.desktops.items()):
                profile_name = desktop["profile"]
                profile = self.config.profiles[profile_name]
                container = observed.get(f"desktop-{name}")
                attrs = container.attrs if container else {}
                state = attrs.get("State", {})
                health = state.get("Health", {})
                components = dict.fromkeys(
                    ("x11", "chromium", "computer", "browser", "viewer", "gateway", "valet"),
                    "unknown",
                )
                if health.get("Log"):
                    try:
                        report = json.loads(health["Log"][-1]["Output"])
                        components.update(
                            {key: value for key, value in report.items() if key in components}
                        )
                    except (ValueError, KeyError, AttributeError):
                        pass
                row = {
                    "name": name,
                    "profile": profile_name,
                    "label": profile["label"],
                    "expected_account": profile.get("expected_account"),
                    "status": state.get("Status", "stopped"),
                    "health": health.get("Status", ""),
                    "container_id": attrs.get("Id"),
                    "image_id": attrs.get("Image"),
                    "desired_image": desired[f"desktop-{name}"]["image"],
                    "components": components,
                    "oom_killed": state.get("OOMKilled", False),
                    "memory_usage": 0,
                    "memory_limit": attrs.get("HostConfig", {}).get("Memory")
                    or memory_bytes(profile["memory"]),
                    "cpu_percent": 0.0,
                    "cpu_limit": profile["cpus"],
                    "clone": self.clone_info(name),
                }
                bindings = attrs.get("NetworkSettings", {}).get("Ports", {}).get("8001/tcp") or []
                port = next(
                    (item["HostPort"] for item in bindings if item["HostIp"] == "127.0.0.1"), None
                )
                for key in ("viewer", "computer", "browser"):
                    row[f"{key}_url"] = (
                        f"http://127.0.0.1:{port}/{key}"
                        if port and row["status"] == "running"
                        else None
                    )
                row["valet_url"] = (
                    f"http://127.0.0.1:{port}/valet"
                    if port and row["status"] == "running" and components.get("valet") == "ready"
                    else None
                )
                if row["status"] == "running":
                    try:
                        stats = container.stats(stream=False, one_shot=True)
                        memory = stats.get("memory_stats", {})
                        row["memory_usage"] = max(
                            0,
                            memory.get("usage", 0)
                            - memory.get("stats", {}).get("inactive_file", 0),
                        )
                        row["memory_limit"] = memory.get("limit", row["memory_limit"])
                        cpu = stats.get("cpu_stats", {})
                        current = (
                            cpu.get("cpu_usage", {}).get("total_usage", 0),
                            cpu.get("system_cpu_usage", 0),
                        )
                        previous = self._cpu_samples.get(row["container_id"], current)
                        self._cpu_samples[row["container_id"]] = current
                        delta, system = current[0] - previous[0], current[1] - previous[1]
                        if system > 0:
                            row["cpu_percent"] = round(
                                max(0, delta / system * cpu.get("online_cpus", 1) * 100), 2
                            )
                    except docker.errors.NotFound:
                        row["status"] = "stopped"
                        for key in ("viewer_url", "computer_url", "browser_url", "valet_url"):
                            row[key] = None
                rows.append(row)
            return rows

    def capacity(self, rows=None):
        rows = self.status() if rows is None else rows
        with self._client() as client:
            info = client.info()
            tracked = {self.volume_name(name) for name in self.config.desktops}
            volumes = client.volumes.list()
            owned = [
                volume
                for volume in volumes
                if (
                    (volume.attrs.get("Labels") or {}).get("com.docker.compose.project")
                    == self.config.project
                    or (volume.attrs.get("Labels") or {}).get("io.hotdesk.config")
                    == self.config.provenance
                )
            ]
            total = info.get("MemTotal", 0)
            running = [row for row in rows if row["status"] == "running"]
            used = sum(row["memory_usage"] for row in running)
            tracked_containers = {row["container_id"] for row in running}
            for container in client.containers.list():
                if container.id not in tracked_containers:
                    try:
                        memory = container.stats(stream=False, one_shot=True).get(
                            "memory_stats", {}
                        )
                        used += max(
                            0,
                            memory.get("usage", 0)
                            - memory.get("stats", {}).get("inactive_file", 0),
                        )
                    except docker.errors.NotFound:
                        pass
            return {
                "running": len(running),
                "max_running": self.config.max_running or len(self.config.desktops),
                "memory_usage": sum(row["memory_usage"] for row in running),
                "memory_reserved": sum(row["memory_limit"] for row in running),
                "memory_budget": min(self.config.memory_budget or total, total),
                "docker_memory_total": total,
                "docker_memory_used": used,
                "docker_memory_available": max(0, total - used),
                "docker_cpus": info.get("NCPU", 0),
                "host_disk_free": shutil.disk_usage(self.config.path.parent).free,
                "retained_volumes": sorted(
                    volume.name for volume in owned if volume.name not in tracked
                ),
            }

    def _stopped(self, name):
        self._workspace(name)
        self.assert_project()
        row = next(row for row in self.status() if row["name"] == name)
        if row["status"] not in ("stopped", "exited", "created", "dead"):
            raise RuntimeError("Stop the workspace before backup or restore")
        with self._client() as client:
            if client.containers.list(filters={"volume": self.volume_name(name)}):
                raise RuntimeError("A running container uses this workspace volume; stop it first")
        return row

    def backup(self, name, path):
        row = self._stopped(name)
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        image = (
            row["image_id"] or render_compose(self.config)["services"][f"desktop-{name}"]["image"]
        )
        volume = self.volume_name(name)
        with self._client() as client:
            client.volumes.get(volume)
            image_id = client.images.get(image).id
        manifest = {
            "format": 1,
            "workspace": name,
            "profile": row["profile"],
            "image_id": image_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "volume": volume,
        }
        with (
            tempfile.TemporaryFile() as home,
            tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output,
        ):
            temporary = Path(output.name)
            try:
                self._run(
                    [
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "tar",
                        "--mount",
                        f"type=volume,source={volume},target=/home/cua,readonly",
                        image,
                        "-C",
                        "/home/cua",
                        "--exclude=./.cache",
                        "--exclude=./.config/pulse/*-runtime",
                        "--exclude=./.config/chromium/Singleton*",
                        "-cf",
                        "-",
                        ".",
                    ],
                    stdout=home,
                    timeout=600,
                )
                home.seek(0)
                manifest["sha256"] = hashlib.file_digest(home, "sha256").hexdigest()
                home.seek(0)
                with tarfile.open(fileobj=output, mode="w") as archive:
                    data = json.dumps(manifest).encode()
                    entry = tarfile.TarInfo("manifest.json")
                    entry.size = len(data)
                    entry.mode = 0o600
                    archive.addfile(entry, io.BytesIO(data))
                    entry = tarfile.TarInfo("home.tar")
                    entry.size = os.fstat(home.fileno()).st_size
                    entry.mode = 0o600
                    archive.addfile(entry, home)
                output.flush()
                os.fsync(output.fileno())
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return manifest

    def checkpoint(self, name):
        self._workspace(name)
        previous = self.checkpoint_info(name)
        checkpoint_id = secrets.token_hex(16)
        directory = self.config.state_dir / "checkpoints" / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"{checkpoint_id}.tar"
        manifest = self.backup(name, path)
        info = {key: manifest[key] for key in ("created_at", "workspace", "image_id")}
        info["id"] = checkpoint_id
        _write_private(directory / "latest.json", json.dumps(info) + "\n")
        if previous is not None:
            Path(previous["path"]).unlink(missing_ok=True)
        return {**info, "path": str(path)}

    def checkpoint_info(self, name):
        self._workspace(name)
        directory = self.config.state_dir / "checkpoints" / name
        pointer = directory / "latest.json"
        if not pointer.exists():
            return None
        info = json.loads(pointer.read_text())
        if (
            not isinstance(info, dict)
            or not re.fullmatch(r"[0-9a-f]{32}", str(info.get("id", "")))
            or info.get("workspace") != name
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(info.get("image_id", "")))
            or not isinstance(info.get("created_at"), str)
        ):
            raise ValueError(f"Invalid checkpoint metadata for {name}")
        if datetime.fromisoformat(info["created_at"]).tzinfo is None:
            raise ValueError("Checkpoint time must include a timezone")
        path = directory / f"{info['id']}.tar"
        if not path.is_file():
            raise ValueError(f"Checkpoint archive is missing for {name}")
        return {**info, "path": str(path)}

    def clone_info(self, name):
        clone = self.config.clones.get(name)
        return {key: value for key, value in clone.items() if key != "profile"} if clone else None

    def _save_clones(self, clones):
        updated = with_clones(self.config, clones)
        _write_private(self.config.state_dir / "clones.json", json.dumps(clones) + "\n")
        self.config = updated

    def clone(self, source, name, checkpoint_id):
        _name(name, "Clone name")
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[0-9a-f]{32}", checkpoint_id):
            raise ValueError("Checkpoint ID must contain 32 lowercase hexadecimal characters")
        self._workspace(source)
        if name in self.config.desktops or name in self.config.profiles:
            raise ValueError(f"Workspace or profile already exists: {name}")
        checkpoint = self.checkpoint_info(source)
        if checkpoint is None:
            raise ValueError("No checkpoint is available; stop the source and create one first")
        if checkpoint["id"] != checkpoint_id:
            raise ValueError(
                "The checkpoint changed; ask the user to approve the current checkpoint"
            )
        retained = (
            name in self._volume_overrides()
            or (self.config.state_dir / "checkpoints" / name).exists()
        )
        with self._client() as client:
            retained |= any(
                volume.name == f"{self.config.project}_home-{name}"
                or (
                    (volume.attrs.get("Labels") or {}).get("io.hotdesk.config")
                    == self.config.provenance
                    and (volume.attrs.get("Labels") or {}).get("io.hotdesk.profile") == name
                )
                for volume in client.volumes.list()
            )
            retained |= any(
                item.labels.get("com.docker.compose.service") == f"desktop-{name}"
                for item in self._containers(client)
            )
            retained |= any(
                network.name == f"{self.config.project}_desktop-{name}"
                or (
                    (network.attrs.get("Labels") or {}).get("com.docker.compose.project")
                    == self.config.project
                    and (network.attrs.get("Labels") or {}).get("com.docker.compose.network")
                    == f"desktop-{name}"
                )
                for network in client.networks.list()
            )
        if retained:
            raise ValueError(f"Clone name has retained resources; choose another name: {name}")
        metadata = {
            "source": source,
            "checkpoint_id": checkpoint_id,
            "created_at": checkpoint["created_at"],
            "image_id": checkpoint["image_id"],
            "profile": dict(self.config.profiles[self._workspace(source)]),
            "ready": True,
        }
        previous = self.config
        clones = {**previous.clones, name: metadata}
        self.config = with_clones(previous, clones)
        try:
            self.validate_start(name)
        finally:
            self.config = previous
        metadata["ready"] = False
        self._save_clones(clones)
        self.restore(name, checkpoint["path"])
        clones[name] = {**metadata, "ready": True}
        self._save_clones(clones)
        self.up(build=False, name=name)
        return {"name": name, **self.clone_info(name)}

    def discard_clone(self, name):
        if name not in self.config.clones:
            raise ValueError(f"Workspace is not a temporary clone: {name}")
        self._save_clones(
            {**self.config.clones, name: {**self.config.clones[name], "ready": False}}
        )
        self.stop(name)
        self._compose("rm", "--force", f"desktop-{name}", timeout=180)
        with self._client() as client:
            for volume in client.volumes.list(
                filters={
                    "label": [
                        f"io.hotdesk.config={self.config.provenance}",
                        f"io.hotdesk.profile={name}",
                    ]
                }
            ):
                labels = volume.attrs.get("Labels") or {}
                if (
                    labels.get("io.hotdesk.config") != self.config.provenance
                    or labels.get("io.hotdesk.profile") != name
                ):
                    raise RuntimeError("Clone volume ownership does not match")
                volume.remove()
            for network in client.networks.list(
                filters={
                    "label": [
                        f"com.docker.compose.project={self.config.project}",
                        f"com.docker.compose.network=desktop-{name}",
                    ]
                }
            ):
                if (network.attrs.get("Labels") or {}).get(
                    "io.hotdesk.config"
                ) != self.config.provenance:
                    raise RuntimeError("Clone network ownership does not match")
                network.remove()
        overrides = self._volume_overrides()
        overrides.pop(name, None)
        _write_private(self.config.state_dir / "volumes.json", json.dumps(overrides) + "\n")
        for prefix in ("viewer-password-", "viewer-read-password-", "gateway-"):
            (self.config.state_dir / f"{prefix}{name}").unlink(missing_ok=True)
        checkpoint_directory = self.config.state_dir / "checkpoints" / name
        if checkpoint_directory.exists():
            shutil.rmtree(checkpoint_directory)
        self._save_clones({key: value for key, value in self.config.clones.items() if key != name})
        self.write_compose()
        return {"name": name, "discarded": True}

    @staticmethod
    def _validate_home(file, normalized):
        file.seek(0)
        with (
            tarfile.open(fileobj=file, mode="r:") as archive,
            tarfile.open(fileobj=normalized, mode="w") as destination,
        ):
            for member in archive:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
                ):
                    raise ValueError("Backup contains an unsafe archive entry")
                if member.issym() or member.islnk():
                    target = posixpath.normpath(member.linkname)
                    if target.startswith("/home/cua/"):
                        target = target.removeprefix("/home/cua/")
                        member.linkname = (
                            posixpath.relpath(target, str(name.parent))
                            if member.issym()
                            else target
                        )
                    target = posixpath.normpath(
                        posixpath.join(str(name.parent) if member.issym() else ".", member.linkname)
                    )
                    if target.startswith("/") or target == ".." or target.startswith("../"):
                        raise ValueError(f"Backup contains an unsafe link: {member.name}")
                destination.addfile(
                    member, archive.extractfile(member) if member.isfile() else None
                )
        file.seek(0)
        normalized.seek(0)

    def restore(self, name, path):
        self._stopped(name)
        profile = self._workspace(name)
        with (
            tarfile.open(Path(path).expanduser(), "r:") as archive,
            tempfile.TemporaryFile() as home,
            tempfile.TemporaryFile() as normalized,
        ):
            members = archive.getmembers()
            if (
                len(members) != 2
                or {item.name for item in members} != {"manifest.json", "home.tar"}
                or any(not item.isfile() for item in members)
            ):
                raise ValueError("Not a Hot Desk backup")
            if archive.getmember("manifest.json").size > 65536:
                raise ValueError("Backup manifest is too large")
            manifest = json.load(archive.extractfile("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != 1
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("image_id", "")))
            ):
                raise ValueError("Unsupported backup manifest")
            shutil.copyfileobj(archive.extractfile("home.tar"), home)
            home.seek(0)
            if hashlib.file_digest(home, "sha256").hexdigest() != manifest.get("sha256"):
                raise ValueError("Backup checksum does not match")
            self._validate_home(home, normalized)
            image = manifest["image_id"]
            with self._client() as client:
                client.images.get(image)
            volume = f"{self.config.project}_home-{profile}-restored-{secrets.token_hex(6)}"
            self._run(
                [
                    "volume",
                    "create",
                    "--label",
                    f"io.hotdesk.config={self.config.provenance}",
                    "--label",
                    f"io.hotdesk.profile={profile}",
                    volume,
                ]
            )
            try:
                script = "import os,sys,tarfile; os.chown('/restore',1000,1000); os.setgid(1000); os.setuid(1000); tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/restore',filter='data'); os.makedirs('/restore/.cache',mode=0o700,exist_ok=True)"
                self._run(
                    [
                        "run",
                        "--rm",
                        "-i",
                        "--network",
                        "none",
                        "--user",
                        "root",
                        "--entrypoint",
                        "/opt/venv/bin/python3",
                        "--mount",
                        f"type=volume,source={volume},target=/restore",
                        image,
                        "-c",
                        script,
                    ],
                    stdin=normalized,
                    timeout=600,
                )
                overrides = self._volume_overrides()
                overrides[profile] = volume
                _write_private(self.config.state_dir / "volumes.json", json.dumps(overrides) + "\n")
            except BaseException:
                self._run(["volume", "rm", volume])
                raise
        return {**manifest, "restored_volume": volume}
