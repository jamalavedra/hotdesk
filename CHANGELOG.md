# Changelog

## Unreleased

Initial development version, `0.1.0`.

- Local Linux desktops configured in TOML and managed through Docker Compose.
- Persistent homes and Chromium profiles, with resource limits per desktop and project.
- MCP stdio connections with tool schemas, inline screenshots, task reservations, and browser and computer control.
- Direct browser viewing, read-only observation, and human takeover.
- Background manager startup, configuration discovery, and optional macOS login startup.
- Recovery for uncertain tool outcomes and credential revocation on manager restart.
- Stopped-workspace backups, checkpoints, and isolated clones requiring explicit user approval.
- Dependency inventories, pinned direct dependencies, and Docker checks for amd64 and arm64.
- Optional Valet 0.1.0 in the desktop image, enabled with `--build-arg HOTDESK_VALET=1`. It adds credential and payment tools to the workspace MCP server; `http_call` and `pay` need Agent Vault and VGS settings in `/home/cua/.valet/env`.
