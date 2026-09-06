# Security

Hot Desk is a local, single-owner desktop service. Its supported boundary is a trusted host running cooperating agents. Desktop containers share Docker's kernel. They are not isolation for hostile tenants, and they can reach the host owner's network.

The manager binds to IPv4 loopback, checks Host and browser Origin headers, and requires a bearer token. Agent credentials are scoped to a workspace. Reservations and the manager control access to computer, browser, and viewer actions. Manager restarts revoke agent credentials. CLI sessions obtain fresh credentials on their next tool command. The viewer uses a same-origin HttpOnly cookie and separate read-only and interactive VNC access.

Guest computer and browser APIs sit behind an authenticated gateway. Only that gateway is published on loopback. Its credential is local runtime state. A process with host shell or Docker access can read it or enter a container, so these controls coordinate trusted clients rather than contain a hostile local agent. Guest administrator access includes browser credentials and persistent files.

Operation history excludes tool arguments and page contents. Unknown operation outcomes require recovery before reassignment. Stopping a container cannot undo remote effects such as a submitted form.

Backups and checkpoints contain persistent home data, including signed-in browser profiles, and must be treated as credentials. A clone copies that saved access into another writable home. Its online actions use the same accounts and remain after local disposal. The clone approval flag is an acknowledgement by the caller, not independent proof of human consent.

CLI screenshot files can contain private page data and remain on disk after a session is released. Restore validates the archive and writes a fresh volume; the old home is retained.

Do not expose these ports externally or run untrusted workloads. Prefer a separate machine or a purpose-built hardened VM environment when you need a stronger boundary.

## Image dependencies

The Debian-based desktop image has upstream vulnerability findings, including packages without a Debian fix. It is not a hardened or vulnerability-free image. Apply available updates and review the scan of the exact image you deploy. See [MAINTENANCE.md](MAINTENANCE.md) for scan commands and version tracking.

## Reporting

Do not put credentials, exploit details, or private desktop data in public issues. Use [GitHub private vulnerability reporting](https://github.com/jamalavedra/hotdesk/security/advisories/new). Include the Hot Desk version, image ID, host platform, reproduction steps, and impact. If the reporting form is unavailable, contact the maintainer through [their GitHub profile](https://github.com/jamalavedra) to arrange a private channel before sharing details.

There are no supported stable releases yet. Report issues against the current development version.
