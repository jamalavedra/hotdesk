import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.metadata import version
from pathlib import Path


@dataclass(frozen=True)
class Config:
    path: Path
    project: str
    profiles: dict
    desktops: dict
    max_running: int | None = None
    memory_budget: int | None = None
    image: str | None = None
    clones: dict = field(default_factory=dict)

    @property
    def provenance(self) -> str:
        return hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()

    @property
    def state_dir(self) -> Path:
        return self.path.parent / ".hotdesk" / self.project


def _table(value, allowed, location):
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a table")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown fields in {location}: {', '.join(sorted(unknown))}")
    return value


def _name(value, location):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ValueError(
            f"{location} must start with a lowercase letter and contain 1-40 lowercase letters, digits or hyphens"
        )
    return value


def memory_bytes(value: str) -> int:
    match = (
        re.fullmatch(r"([1-9][0-9]*)([kmg])b?", value.lower()) if isinstance(value, str) else None
    )
    if not match:
        raise ValueError("Memory must use a positive size such as '512m' or '2g'")
    return int(match[1]) * 1024 ** ("kmg".index(match[2]) + 1)


def load_config(path="hotdesk.toml") -> Config:
    path = Path(path).expanduser().resolve()
    with path.open("rb") as file:
        data = tomllib.load(file)
    _table(data, {"project", "profiles", "desktops"}, "configuration")
    project_options = _table(
        data.get("project", {}), {"name", "max_running", "memory_budget", "image"}, "project"
    )
    project = _name(project_options.get("name", "hotdesk"), "project.name")
    max_running = project_options.get("max_running")
    if max_running is not None and (type(max_running) is not int or max_running < 1):
        raise ValueError("project.max_running must be a positive integer")
    budget = project_options.get("memory_budget")
    memory_budget = memory_bytes(budget) if budget is not None else None
    image = project_options.get("image")
    if image is not None and (
        not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image)
    ):
        raise ValueError("project.image must be a Docker image reference")
    profiles = data.get("profiles", {})
    desktops = data.get("desktops", {})
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Define at least one profile")
    if not isinstance(desktops, dict) or not desktops:
        raise ValueError("Define at least one desktop")
    normalized = {}
    for name, profile in profiles.items():
        _name(name, "Profile name")
        _table(profile, {"label", "cpus", "memory", "expected_account"}, f"profiles.{name}")
        label = profile.get("label", name)
        cpus = profile.get("cpus", 2)
        memory = profile.get("memory", "2g")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError(f"profiles.{name}.label must contain 1-100 characters")
        if type(cpus) not in (int, float) or not math.isfinite(cpus) or not 0 < cpus <= 256:
            raise ValueError(
                f"profiles.{name}.cpus must be a number greater than zero and at most 256"
            )
        try:
            memory_bytes(memory)
        except ValueError as error:
            raise ValueError(f"profiles.{name}.memory: {error}") from None
        normalized[name] = {"label": label, "cpus": cpus, "memory": memory.lower()}
        expected_account = profile.get("expected_account")
        if expected_account is not None:
            if (
                not isinstance(expected_account, str)
                or not expected_account.strip()
                or len(expected_account) > 200
            ):
                raise ValueError(f"profiles.{name}.expected_account must contain 1-200 characters")
            normalized[name]["expected_account"] = expected_account
    assigned = set()
    for name, desktop in desktops.items():
        _name(name, "Desktop name")
        _table(desktop, {"profile"}, f"desktops.{name}")
        profile = desktop.get("profile")
        if not isinstance(profile, str) or profile not in normalized:
            raise ValueError(f"desktops.{name}.profile must name a declared profile")
        if profile in assigned:
            raise ValueError(f"Profile '{profile}' is assigned to more than one desktop")
        assigned.add(profile)
    config = Config(path, project, normalized, desktops, max_running, memory_budget, image)
    registry = config.state_dir / "clones.json"
    return with_clones(config, json.loads(registry.read_text()) if registry.exists() else {})


def with_clones(config: Config, clones: dict) -> Config:
    profiles = {name: value for name, value in config.profiles.items() if name not in config.clones}
    desktops = {name: value for name, value in config.desktops.items() if name not in config.clones}
    if not isinstance(clones, dict):
        raise ValueError("Invalid clone registry")
    for name, clone in clones.items():
        _name(name, "Clone name")
        if name in profiles or name in desktops:
            raise ValueError(f"Clone name conflicts with configuration: {name}")
        _table(
            clone,
            {"source", "checkpoint_id", "created_at", "image_id", "profile", "ready"},
            "clone",
        )
        _name(clone.get("source"), "Clone source")
        if (
            not re.fullmatch(r"[0-9a-f]{32}", str(clone.get("checkpoint_id", "")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(clone.get("image_id", "")))
            or not isinstance(clone.get("created_at"), str)
            or type(clone.get("ready")) is not bool
        ):
            raise ValueError("Invalid clone metadata")
        if datetime.fromisoformat(clone["created_at"]).tzinfo is None:
            raise ValueError("Clone checkpoint time must include a timezone")
        profile = _table(
            clone.get("profile"), {"label", "cpus", "memory", "expected_account"}, "clone profile"
        )
        cpus = profile.get("cpus")
        if type(cpus) not in (int, float) or not math.isfinite(cpus) or not 0 < cpus <= 256:
            raise ValueError("Invalid clone CPU limit")
        memory_bytes(profile.get("memory"))
        if not isinstance(profile.get("label"), str) or not 0 < len(profile["label"]) <= 100:
            raise ValueError("Invalid clone label")
        account = profile.get("expected_account")
        if account is not None and (not isinstance(account, str) or not 0 < len(account) <= 200):
            raise ValueError("Invalid clone account")
        profiles[name] = dict(profile)
        desktops[name] = {"profile": name, "image": clone["image_id"]}
    return replace(config, profiles=profiles, desktops=desktops, clones=dict(clones))


def render_compose(config: Config) -> dict:
    desktop_dir = Path(__file__).resolve().parent / "desktop"
    if not desktop_dir.is_dir():
        desktop_dir = desktop_dir.parent.parent / "desktop"
    services = {}
    for name, desktop in sorted(config.desktops.items()):
        profile_name = desktop["profile"]
        profile = config.profiles[profile_name]
        services[f"desktop-{name}"] = {
            "image": desktop.get("image")
            or config.image
            or f"hotdesk-desktop:{version('hotdesk')}",
            "build": {"context": str(desktop_dir), "args": {"HOTDESK_VERSION": version("hotdesk")}},
            "pull_policy": "never",
            "stop_grace_period": "20s",
            "restart": "unless-stopped",
            "hostname": f"desktop-{name}",
            "environment": {
                "VNC_RESOLUTION": "1280x800",
                "VNC_PW": "${HOTDESK_VNC_PASSWORD:?Use hotdesk up}",
            },
            "security_opt": [f"seccomp={desktop_dir / 'chrome-seccomp.json'}"],
            "shm_size": "1g",
            "mem_limit": profile["memory"],
            "cpus": profile["cpus"],
            "volumes": [f"home-{profile_name}:/home/cua"],
            "networks": [f"desktop-{name}"],
            "ports": ["127.0.0.1::8001"],
            "labels": {
                "io.hotdesk.desktop": name,
                "io.hotdesk.profile": profile_name,
                "io.hotdesk.config": config.provenance,
            },
        }
        if config.image or desktop.get("image"):
            services[f"desktop-{name}"].pop("build")
    return {
        "name": config.project,
        "services": services,
        "volumes": {
            f"home-{name}": {
                "labels": {"io.hotdesk.config": config.provenance, "io.hotdesk.profile": name}
            }
            for name in sorted(config.profiles)
        },
        "networks": {
            f"desktop-{name}": {"labels": {"io.hotdesk.config": config.provenance}}
            for name in sorted(config.desktops)
        },
    }
