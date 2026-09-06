# Hot Desk

Run local Linux desktops for AI agents. Each workspace has a persistent home and Chromium profile. An agent reserves a desktop for its task; you can watch or take control in your browser.

Hot Desk uses Docker Compose for desktops, CUA for computer tools, Playwright MCP for browser tools, and noVNC for viewing. A Python manager coordinates ownership and proxies tool calls. SQLite stores reservations and operation history. Agents use the CLI without registering an MCP server. Hot Desk does not run a model or need a model API key.

This is a development project with no stable release. It runs on macOS and Linux with a local Docker daemon. Windows hosts and remote Docker engines are unsupported. Desktops are Linux containers sharing Docker's kernel, not separate virtual machines.

## Start

Install Docker with Compose, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/). Docker Desktop works on macOS. The example desktop needs a 2 GiB memory allowance, plus headroom for Docker and other workloads.

```sh
git clone https://github.com/jamalavedra/hotdesk.git
cd hotdesk
cp -n hotdesk.example.toml hotdesk.toml
uv sync --locked
uv run hotdesk open
uv run hotdesk up
uv run hotdesk open research
```

`open` starts the manager in the background. `up` builds the image and starts configured desktops. The first build downloads the desktop dependencies. `open research` opens that desktop with human control. Sign in to websites in Chromium, then return control using the viewer's **Return control to agents** button or:

```sh
uv run hotdesk release research
```

Use `uv run hotdesk open research --observe` to watch without taking control. Closing the viewer leaves its desktop running and does not release human control.

### Install the command for agents

From the checkout:

```sh
uv tool install --editable .
hotdesk default --config-path /absolute/path/to/hotdesk/hotdesk.toml
```

This installs `hotdesk` in a separate tool environment using the package's dependency constraints. For development with the exact lockfile, keep using `uv run hotdesk` from the checkout.

The installed command works from other directories. Configuration selection uses `--config PATH` first, then an existing `./hotdesk.toml`, then the remembered default. Invalid explicit or local configuration fails instead of switching projects. The default stores only a path in `~/.local/state/hotdesk/default.json`.

No port needs to be remembered. Commands find the manager through private local state. Startup prefers port 7890 and falls back to a free loopback port. `open --port 7891` requests an exact port for a new manager; it cannot change an existing manager's port. Omit `--port` for a registered login service.

## Use from an agent

Tell your agent: "Use the Research Hot Desk desktop for this task." It can read `hotdesk skill` for instructions and `hotdesk --help` for commands. Install the bundled skill in your agent application's supported skill directory. For Codex:

```sh
mkdir -p ~/.codex/skills/hotdesk
(set -C; hotdesk skill > ~/.codex/skills/hotdesk/SKILL.md)
```

This refuses to overwrite an existing skill. Start a new agent session after installation.

The agent chooses a unique session name for its task and reuses it:

```sh
hotdesk agent research-a7c9 --workspace research tools
hotdesk agent research-a7c9 tools browser_navigate
hotdesk agent research-a7c9 call browser_navigate '{"url":"https://example.com/"}'
hotdesk agent research-a7c9 call computer_screenshot
hotdesk agent research-a7c9 release
```

Browser and computer calls acquire or renew a five-minute reservation automatically. Concurrent commands in the same session are refused. Other sessions cannot act on that workspace while it is reserved or under human control. Renew with `call workspace_renew` during long pauses and release when finished. Detached guest processes must finish or be stopped before release or takeover.

`tools` lists names and descriptions; `tools TOOL_NAME` returns the argument schema. If a stopped desktop exposes only workspace tools, call `workspace_acquire` and list tools again. Tool arguments are a JSON object or `-` for stdin. Results are JSON; screenshots are saved to private local files. Tool errors produce a nonzero exit status and are not automatically retried.

### Busy desktops and clones

A busy desktop returns `WORKSPACE_BUSY` with ownership and the latest checkpoint's availability, ID, and timestamp. The agent must ask whether to wait or create a temporary clone, explaining the checkpoint's age. Cloning is never automatic.

Without a checkpoint, wait until the source is idle, then stop it and save one:

```sh
hotdesk stop research
hotdesk checkpoint research
hotdesk start research
```

A checkpoint saves the stopped home, including browser storage and files, but no running processes. Creating a checkpoint replaces the previous checkpoint. Keep separate backups if you need older copies.

After the user agrees, clone the exact checkpoint they approved:

```sh
hotdesk clone research research-copy --checkpoint CHECKPOINT_ID --user-approved
hotdesk agent copy-a7c9 --workspace research-copy tools
```

The clone gets a separate writable home and uses the checkpoint's image. It excludes later source changes and never merges local changes back. Both desktops can still use the same online accounts. Posts and cloud document edits persist online; discarding the clone cannot undo them. Verify the active account before acting.

Clones count toward resource limits. The example allows one running desktop; increase `max_running` and `memory_budget` before starting parallel desktops. Clones retain local state after release and manager restart. Save needed outputs outside the clone, release its session, then discard it:

```sh
hotdesk agent copy-a7c9 release
hotdesk discard research-copy --outputs-saved
```

`--user-approved` acknowledges the user's choice; it cannot verify who made it. Agents must not supply it without asking. `--outputs-saved` confirms needed outputs are preserved elsewhere or none are needed. Discard removes the clone's container and home, but cannot delete a configured source workspace.

## Configure workspaces

```toml
[project]
name = "hotdesk"
max_running = 1
memory_budget = "2g"

[profiles.research]
label = "Research"
cpus = 2
memory = "2g"

[desktops.research]
profile = "research"
```

Each profile names a persistent home and resource limits. Assign each profile to one desktop. Desktop names are the workspace identifiers. Changing a label keeps the home; changing the project or profile name selects a different Docker volume.

Use a unique project name per Docker engine. One manager controls each project. CPU limits cap CPU time rather than reserve cores. Project limits bound running desktops and their memory allowances. On macOS, Docker's VM allocation also limits available memory.

Edit the configuration, then apply it while all workspaces are idle:

```sh
hotdesk plan
hotdesk apply --no-build
```

`plan` prints Compose configuration. `apply` coordinates changes through the manager. `project.image` can select an existing image by tag, image ID, or registry digest. With no explicit image, `up` builds `hotdesk-desktop:<package version>`; `up --no-build` reuses it.

## Control and recovery

| Command | Effect |
|---|---|
| `hotdesk status` | Show readiness, ownership, and resource usage as JSON |
| `hotdesk doctor` | Check Docker, Compose, image, and manager availability |
| `hotdesk open research` | Start if stopped, take control, and open the viewer |
| `hotdesk open research --observe` | Open a read-only viewer |
| `hotdesk release research` | Return human control to agents |
| `hotdesk stop research` | Stop the desktop, preserving its home |
| `hotdesk start research` | Start an existing desktop image |
| `hotdesk down` | Remove project containers and networks, preserving homes |
| `hotdesk manager-stop` | Stop an idle manager, leaving desktops running |
| `hotdesk manager-logs --lines 50` | Show the private manager log |

Lifecycle commands require a running manager; `hotdesk open` starts it. Human takeover blocks new agent actions and waits for the current action. Other workspaces remain available. Human control survives manager restarts. Reopen viewer tabs after a restart; old credentials are revoked, and CLI sessions refresh their credentials on the next tool command.

An uncertain tool outcome puts the workspace in recovery. Observe it and inspect status before running `hotdesk recover research`, which restarts the desktop. Unsaved work can be lost. Recovery cannot undo online actions or work already completed by a detached process.

Back up and restore stopped, idle workspaces:

```sh
hotdesk stop research
hotdesk backup research /absolute/path/research.tar
hotdesk restore research /absolute/path/research.tar
hotdesk start research
```

Restore validates the archive and writes a fresh volume, retaining the previous home. Test restoration in a separate workspace before relying on a backup.

## Persistence and access

Homes live in Docker volumes at `/home/cua`. Files and browser profiles survive container recreation. Process memory and packages installed elsewhere do not. Add durable system packages to `desktop/Dockerfile` and rebuild. Website sessions can expire or require authentication again; sign in through the viewer.

Runtime state lives beside the configuration in `.hotdesk/<project>/`. Manager discovery lives in `~/.local/state/hotdesk/projects/`. The manager stays on the Docker context selected at startup. Lifecycle commands refuse a different context and show the `DOCKER_CONTEXT` to use. Stop the manager before moving a configuration to another engine; volumes do not move automatically.

The manager binds to `127.0.0.1` and requires authentication. Keep runtime state, checkpoints, and backups private because they include access to signed-in profiles. CLI screenshots remain in `.hotdesk/<project>/agent-sessions/artifacts/` after release; delete unneeded captures yourself. Do not expose Hot Desk ports to the network. Agents can administer guests and reach the owner's network. See [SECURITY.md](SECURITY.md) for the trust boundary.

## Login startup and maintenance

On macOS, opt into manager startup at user login:

```sh
hotdesk service install
hotdesk service status
hotdesk service uninstall
```

This starts only the manager. It does not start Docker or build images. Docker's `unless-stopped` policy can independently restart desktops when its engine starts. `manager-stop` leaves login registration installed; `service uninstall` removes it. Both refuse to stop an active managed session. Reinstall the service after moving the checkout, interpreter, or configuration.

On Linux, use `hotdesk serve` with your existing process manager. There is no automatic Linux service installer.

`hotdesk versions` reports host dependencies, the desktop image ID, and its build-time package inventory. See [MAINTENANCE.md](MAINTENANCE.md) for updates and release checks, and [CONTRIBUTING.md](CONTRIBUTING.md) for tests.

Original Hot Desk code is MIT licensed. Desktop components retain their upstream licenses; see [NOTICE](NOTICE) and [desktop/NOTICE](desktop/NOTICE).
