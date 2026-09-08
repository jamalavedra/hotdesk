# Maintenance

## Version sources

```sh
uv run hotdesk --version
uv run hotdesk versions
```

`--version` reads the installed Hot Desk package metadata. `versions` reports host dependencies and inspects the local desktop image. It briefly runs `cat` in a read-only container with networking disabled to read the image's build manifest. It does not start a desktop.

| Component | Source of truth | Update method |
|---|---|---|
| Hot Desk | `project.version` in `pyproject.toml` | Change with a release entry in `CHANGELOG.md` |
| Host Python dependencies | Constraints in `pyproject.toml`, exact versions and hashes in `uv.lock` | `uv lock --upgrade`, then `uv sync --locked` and tests |
| CUA base image | Tag and digest in `desktop/Dockerfile` | Review upstream image changes and replace the digest |
| CUA computer server, Playwright MCP, Node, pip | Named `ARG` values in `desktop/Dockerfile` | Review release notes, edit the pin, rebuild and run E2E |
| Debian packages, Chromium, XFCE, noVNC | Debian repositories at build time | Rebuild with `--pull --no-cache`; inspect the build manifest |
| GitHub Actions | Commit SHAs and version comments in `.github/workflows/` | Review Dependabot pull requests |

The image records `/etc/hotdesk-versions.json` during its build, including Python packages and desktop program versions. This is build-time evidence, not a live inventory of modifications made inside a running guest. `hotdesk-desktop:<Hot Desk version>` is a mutable local build tag; use the image ID from `versions` when reporting a bug.

The desktop build is not bit-for-bit reproducible. Its base is digest-pinned and direct application dependencies are pinned, but Debian packages and transitive Python/npm dependencies resolve during the build. Retain the exact tested image to reproduce a deployment. `project.image` accepts an explicit Docker image reference, including an image ID or registry digest. Pin that reference in deployment configuration after testing. Keep the previous image and a stopped-workspace backup before applying an update. To roll back, restore the previous image reference and apply with `--no-build`. Browser profile formats can change across versions; restore the corresponding backup into a fresh home if the old browser cannot read an upgraded profile. Do not describe a rebuilt tag as the same artifact without comparing image IDs.

## Update cadence

Dependabot checks host dependencies, GitHub Actions and the Docker base weekly. It does not update application pins declared in Dockerfile `ARG` values or refresh Debian layers. Review those manually at least monthly and when upstream publishes a relevant security fix. Scheduled CI rebuilds the image weekly to catch dependency drift.

Track [CUA releases](https://github.com/trycua/cua/releases), [Playwright MCP releases](https://github.com/microsoft/playwright-mcp/releases), [Node releases](https://nodejs.org/en/about/previous-releases), and [MCP Python SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases).

The host pins standalone FastMCP in `pyproject.toml` for proxying and middleware. The underlying MCP protocol SDK remains a separate dependency. Test upgrades together, including tool discovery, images, reservation gates, and MCP stdio connections through the internal HTTP gateway. Track [FastMCP releases](https://github.com/PrefectHQ/fastmcp/releases) as well as the protocol SDK.

For a runtime update:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
docker build --pull --no-cache --build-arg HOTDESK_VERSION="$(uv run hotdesk --version)" -t "hotdesk-desktop:$(uv run hotdesk --version)" desktop
uv run python tests/e2e.py
uv run python tests/e2e_clone.py
uv run hotdesk --config hotdesk.example.toml versions
uv build
```

Audit host dependencies and the built desktop separately:

```sh
uv export --no-dev --no-emit-project --format requirements-txt -o /tmp/hotdesk-requirements.txt
uvx pip-audit -r /tmp/hotdesk-requirements.txt --no-deps --disable-pip
trivy image --scanners vuln "hotdesk-desktop:$(uv run hotdesk --version)"
```

Review advisories against the installed files and Debian's security status. Scanner results can include stale package inventories or code that is not used. Do not add blanket suppressions or treat passing functional tests as a clean vulnerability scan. The build applies available Debian updates and removes unused Firefox and build-only npm/Corepack. The pinned base still determines which distribution packages and security fixes are available.

## First public release

Before tagging a release:

1. Check package metadata links against [the repository](https://github.com/jamalavedra/hotdesk), and update the changelog.
2. Enable GitHub private vulnerability reporting and verify that maintainers receive reports. Check `SECURITY.md` against that configuration.
3. Require the Python and both architecture E2E jobs before merging. Check their results on the commit being released.
4. Run the browser check from `CONTRIBUTING.md`. Inspect package contents, dependency inventories and third-party notices. Obtain source and required notices for any binary images you redistribute.
5. Build and test each architecture image, retain its immutable digest, and attach the package inventory and test evidence to the release. Publish only tested artifacts. A scheduled rebuild is a new artifact and needs its own E2E result. Do not claim identical Debian or transitive dependencies across rebuilds.
6. Confirm package-name availability before uploading to a registry. Build and inspect the wheel and source archive. Set the release version, move relevant changelog entries under that version, and tag the tested commit.

Do not publish `hotdesk.toml`, desktop profiles, `.hotdesk/`, local plans, model transcripts, or credentials. Git ignores local state; still inspect the exact staged files and build artifacts.

## Image release workflow

Pushing a `v*` tag runs `.github/workflows/release.yml`. The tag must match the Python package version. Native amd64 and arm64 jobs build once, run checks, and push that exact tested image to a commit-specific architecture tag. The version manifest is created only after both jobs pass, using their registry digests. It publishes no `latest` tag and does not upload the Python package.

Each job retains its E2E logs, image ID, and installed-package inventory as workflow artifacts. Image labels record the source repository and commit. This inventory is not a formal SBOM or a signed provenance attestation. Registry access uses the tag job's `GITHUB_TOKEN` with `packages: write`; pull requests have no release path. Configure protected release tags and package visibility in the public repository before running it. Check the workflow result before announcing an image. Repository publication does not publish a container image.

The workflow follows Docker's [multi-platform image guidance](https://docs.docker.com/build/ci/github-actions/multi-platform/) and GitHub's [container publishing documentation](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images). Consumers should record the resulting immutable registry digest in `project.image`.

## Login startup checks

The macOS LaunchAgent stores absolute interpreter, configuration, and working-directory paths, the selected Docker context or host, and an explicit executable search path. Reinstall after moving these files. Reinstalling an existing job updates its plist for the next login. To apply changed launch settings immediately, uninstall and install after work finishes.

Run login-service tests with a temporary configuration and disposable launchd job. A bootstrap test does not verify an actual logout/login cycle. Test that cycle in a disposable macOS user session and record it separately. Native Linux launcher checks also need their own result.
