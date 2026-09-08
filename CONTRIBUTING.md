# Contributing

Use Python 3.11 or newer and a local Docker daemon. Start with:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
```

Ruff owns Python formatting and import checks. Format edited Python files with `uv run ruff format`. Keep the static HTML, CSS and JavaScript readable; there is no frontend build step.

Changes to desktop behavior, dependencies or the manager need the real Docker check:

```sh
docker build --build-arg HOTDESK_VERSION="$(uv run hotdesk --version)" -t "hotdesk-desktop:$(uv run hotdesk --version)" desktop
uv run python tests/e2e.py
uv run python tests/e2e_clone.py
```

`tests/e2e.py` creates two disposable desktops and tests task reservation races, actual input, screenshots, browser navigation, handoff, manager crash recovery, profile persistence, and backup/restore. It exercises the internal HTTP MCP gateway and MCP stdio connections from outside the checkout. It removes only its test project's resources. A successful tool response is not enough; checks must observe the effect in the guest.

The clone check tests the busy MCP response, explicit approval, checkpoint browser cookies, isolated writes, parallel actions, manager restart, and disposal. CI runs both Docker checks on native amd64 and arm64 Linux runners.

For viewer changes, install Playwright for the test process and run the disposable deployment with its browser check enabled:

```sh
HOTDESK_BROWSER_CHECK=1 uv run --with playwright python tests/e2e.py
```

The browser check requires Google Chrome installed on the test host. It opens a real viewer and captures desktop and mobile screenshots under ignored `.hotdesk/browser-check/`. Inspect those screenshots. To test an already running disposable deployment, run `uv run --with playwright python tests/browser_check.py --config PATH --workspace NAME`. Do not point failure or takeover tests at someone's active workspace.

In a pull request, describe the changed behavior, the tests run, and any remaining limits. Keep lifecycle in Docker Compose and desktop actions in upstream tools. Avoid new runtime dependencies when the existing stack covers the change. See [maintenance](MAINTENANCE.md) for dependency updates and releases.

Never commit local state or credentials. Report vulnerabilities through [SECURITY.md](SECURITY.md).

Launcher checks include real background manager subprocesses, port contention and authenticated shutdown against a deliberately unavailable Docker endpoint. Run `uv run python -m unittest discover -s tests -p test_launcher.py` for that path. The full suite also checks health deadlines and configuration precedence.

For login-service changes, follow the disposable macOS checks in [MAINTENANCE.md](MAINTENANCE.md#login-startup-checks).
