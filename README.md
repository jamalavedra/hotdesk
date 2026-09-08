# Hot Desk

Persistent local Linux desktops for AI agents. Each desktop keeps its own home directory and Chromium profile across sessions. An agent reserves a desktop through the CLI, and you watch or take over in the browser when it gets stuck.

![Claude Code researching posts on X and writing a Google Doc inside a Hot Desk desktop, then a human taking over in the viewer](docs/demo.gif)

Demo from [Agents love computers](https://www.jamalavedra.com/blog/agents-love-computers). Video: [hotdesk-x-to-docs.mp4](https://www.jamalavedra.com/blog/agents-love-computers/hotdesk-x-to-docs.mp4).

## How it works

- Desktops are Docker Compose services built on [trycua/xfce-cua](https://github.com/trycua/cua) with Chromium, TigerVNC, and noVNC.
- Inside each desktop, `cua-computer-server` provides computer tools (screenshot, click, type) and [Playwright MCP](https://github.com/microsoft/playwright-mcp) provides browser tools.
- A Python manager on the host owns reservations, proxies tool calls, and serves the noVNC viewer. It binds to `127.0.0.1` with token auth and stores reservations and operation history in SQLite.
- Agents call `hotdesk agent ... call TOOL '{...}'` from a shell. No MCP server registration, no model, no model API key.

Status: development, no stable release. Requires macOS or Linux with a local Docker daemon. Windows hosts and remote Docker engines are unsupported. Desktops are containers sharing Docker's kernel, not VMs.

## Quick start

Requires Docker with Compose, Python 3.11+, and [uv](https://docs.astral.sh/uv/). The example desktop needs 2 GiB of memory on top of Docker's own usage.

```sh
git clone https://github.com/jamalavedra/hotdesk.git
cd hotdesk
cp -n hotdesk.example.toml hotdesk.toml
uv sync --locked
uv run hotdesk open            # start the manager
uv run hotdesk up              # build the image, start configured desktops
uv run hotdesk open research   # take control and open the viewer
```

Sign in to websites in Chromium, then hand the desktop back with the viewer's **Return control to agents** button or `uv run hotdesk release research`. `open research --observe` opens a read-only viewer. Closing the viewer neither stops the desktop nor releases control.

Install the command for use outside the checkout:

```sh
uv tool install --editable .
hotdesk default --config-path /absolute/path/to/hotdesk/hotdesk.toml
```

Config resolution order: `--config PATH`, then `./hotdesk.toml`, then the remembered default in `~/.local/state/hotdesk/default.json`. The CLI finds the manager through private local state, so no port is needed. The manager prefers port 7890 and falls back to a free loopback port.

## Agent usage

Tell the agent "Use the Research Hot Desk desktop for this task." `hotdesk skill` prints the instructions it needs. For Codex:

```sh
mkdir -p ~/.codex/skills/hotdesk
(set -C; hotdesk skill > ~/.codex/skills/hotdesk/SKILL.md)
```

The agent picks a session name and reuses it:

```sh
hotdesk agent research-a7c9 --workspace research tools      # list tools
hotdesk agent research-a7c9 tools browser_navigate           # argument schema
hotdesk agent research-a7c9 call browser_navigate '{"url":"https://example.com/"}'
hotdesk agent research-a7c9 call computer_screenshot
hotdesk agent research-a7c9 release
```

Rules the manager enforces:

- The first browser or computer call takes a five-minute reservation. Every call renews it. `call workspace_renew` renews it during long pauses.
- One in-flight command per session. Other sessions get `WORKSPACE_BUSY` with the owner and latest checkpoint while the desktop is reserved or under human control.
- Tool errors exit nonzero and are not retried. Results are JSON. Screenshots go to `.hotdesk/<project>/agent-sessions/artifacts/`.
- A stopped desktop exposes only workspace tools. Call `workspace_acquire`, then list tools again.
- Detached guest processes must finish or be stopped before release or takeover.

### Checkpoints and clones

A checkpoint is a copy of a stopped desktop's home, browser storage included. Each workspace keeps one checkpoint. Taking a new one replaces it.

```sh
hotdesk stop research
hotdesk checkpoint research
hotdesk start research
```

When a desktop is busy, the agent must ask the user whether to wait or clone the checkpoint, stating the checkpoint's age. Clones are never automatic.

```sh
hotdesk clone research research-copy --checkpoint CHECKPOINT_ID --user-approved
hotdesk agent copy-a7c9 --workspace research-copy tools
hotdesk agent copy-a7c9 release
hotdesk discard research-copy --outputs-saved
```

A clone gets its own writable home from the checkpoint and never merges back. It shares the source's signed-in accounts, so anything posted or edited online from the clone stays online after discard. Clones count against `max_running` and `memory_budget`. `--user-approved` and `--outputs-saved` record the user's decisions. They do not verify them, and agents must not pass them without asking. `discard` refuses to delete a configured source workspace.

## Configuration

```toml
[project]
name = "hotdesk"        # unique per Docker engine; one manager per project
max_running = 1
memory_budget = "2g"

[profiles.research]
label = "Research"
cpus = 2                # CPU-time cap, not a core reservation
memory = "2g"

[desktops.research]
profile = "research"
```

A profile names a persistent Docker volume and its limits. Each profile belongs to one desktop, and the desktop name is the workspace identifier. Renaming a label keeps the volume. Renaming the project or profile selects a different volume.

Apply changes while every workspace is idle:

```sh
hotdesk plan              # print the generated Compose config
hotdesk apply --no-build
```

`up` builds `hotdesk-desktop:<package version>` unless `project.image` names an existing tag, image ID, or digest. `up --no-build` reuses the built image.

## Commands

| Command | Effect |
|---|---|
| `hotdesk status` | Readiness, ownership, and resource usage as JSON |
| `hotdesk doctor` | Check Docker, Compose, image, and manager |
| `hotdesk open research` | Start if stopped, take control, open the viewer |
| `hotdesk open research --observe` | Read-only viewer |
| `hotdesk release research` | Return control to agents |
| `hotdesk stop` / `start research` | Stop or start a desktop, keeping its home |
| `hotdesk recover research` | Restart a desktop stuck in recovery |
| `hotdesk backup research FILE.tar` | Archive a stopped, idle workspace |
| `hotdesk restore research FILE.tar` | Write the archive to a fresh volume, keeping the old one |
| `hotdesk down` | Remove containers and networks, keeping homes |
| `hotdesk manager-stop` | Stop an idle manager, leaving desktops running |
| `hotdesk manager-logs --lines 50` | Tail the manager log |
| `hotdesk versions` | Host dependencies, image ID, and the image's package inventory |

Lifecycle commands need a running manager. Human takeover waits for the in-flight agent action, then blocks new ones on that workspace only. Takeover survives manager restarts. After a restart, reopen viewer tabs; CLI sessions refresh credentials on their next call.

An uncertain tool outcome puts the workspace into recovery. Inspect it in the viewer and `status` before running `recover`, which restarts the desktop and drops unsaved work. Nothing undoes actions already taken online.

## State and security

Homes are Docker volumes mounted at `/home/cua`. Files and browser profiles survive container recreation. Process memory and packages installed outside the home do not. Bake system packages into `desktop/Dockerfile`.

Runtime state lives in `.hotdesk/<project>/` next to the config. Manager discovery lives in `~/.local/state/hotdesk/projects/`. The manager pins the Docker context it started on and refuses lifecycle commands from another. Volumes do not move between engines.

Runtime state, checkpoints, and backups contain signed-in browser profiles. Keep them private and never expose Hot Desk ports beyond loopback. Agents have administrator access inside the guest and can reach the host's network. See [SECURITY.md](SECURITY.md).

## Login startup

macOS only:

```sh
hotdesk service install
hotdesk service status
hotdesk service uninstall
```

This starts the manager at login, not Docker or the desktops. Docker's `unless-stopped` policy restarts desktops on its own. Reinstall after moving the checkout, interpreter, or config. On Linux, run `hotdesk serve` under your process manager.

See [MAINTENANCE.md](MAINTENANCE.md) for updates and release checks, and [CONTRIBUTING.md](CONTRIBUTING.md) for tests.

Hot Desk's own code is MIT. The desktop image bundles software under other licenses; see [NOTICE](NOTICE) and [desktop/NOTICE](desktop/NOTICE).
