---
name: hotdesk
description: Use a local Hot Desk desktop and its authenticated browser for a task. Use when the user asks to use Hot Desk or control one of its workspaces. Supports browser interaction, screenshots, keyboard, pointer, and guest files through the Hot Desk CLI.
---

Use the `hotdesk` CLI directly. No MCP registration is needed.

Run `hotdesk status` to see configured workspaces and ownership. Use the workspace the user requested. If several exist and none is specified, ask which account/workspace to use. Do not switch to Helium or another profile because a desktop is busy.

Choose a unique session name for this task, such as `property-research-a7c9`, and reuse it for every command. Separate tasks need separate session names. The first `tools` or `call` command binds the session to its selected workspace until release; `status` only reads it.

```sh
hotdesk agent property-research-a7c9 --workspace research status
hotdesk agent property-research-a7c9 --workspace research tools
hotdesk agent property-research-a7c9 tools browser_navigate
hotdesk agent property-research-a7c9 call browser_navigate '{"url":"https://www.idealista.com/"}'
hotdesk agent property-research-a7c9 call browser_snapshot
hotdesk agent property-research-a7c9 call computer_screenshot
hotdesk agent property-research-a7c9 release
```

Use `tools` to list names and descriptions, then `tools TOOL_NAME` to read the exact JSON argument schema. Do not invent tool names or arguments. If a stopped desktop only exposes workspace tools, run `hotdesk agent SESSION call workspace_acquire` and list tools again. Commands accept a JSON object, or `-` to read it from stdin. Image results contain local file paths; open those images with the agent's image viewer.

The CLI starts the manager as needed, stores its credentials privately, and acquires or renews a five-minute reservation before browser/computer calls. Keep the session for the whole task and release when finished. Use `call workspace_renew` during long pauses to retain the reservation. A task that finishes or is abandoned must release its workspace. Do not run concurrent commands with the same session name.

Busy or human-controlled workspaces reject agent actions. On `WORKSPACE_BUSY`, read the owner, task, and checkpoint details. Ask the user whether to wait or create a temporary clone. Give the checkpoint's creation time and explain that newer source changes are missing, local clone changes never merge back, and online account actions still persist. Never clone automatically or switch accounts to bypass ownership.

If the user chooses cloning, run `hotdesk clone SOURCE CLONE --checkpoint CHECKPOINT_ID --user-approved` with the exact approved checkpoint ID. Use a new agent session bound to `CLONE`. The clone has an independent writable home and retains local state after release. Save needed outputs outside it, release its agent session, then run `hotdesk discard CLONE --outputs-saved` only when those outputs are safe or none are needed. Discard cannot undo online actions.

Without a checkpoint, report that one requires the source to become idle and stopped. Once it is available, use `hotdesk stop SOURCE`, `hotdesk checkpoint SOURCE`, then `hotdesk start SOURCE`. Do not interrupt an active task to create a checkpoint. Clones use the same resource limits as other desktops.

An acknowledged tool failure preserves the reservation so you can inspect the browser before deciding what to do next. It does not guarantee the action had no effects. Unknown operation outcomes require inspection and recovery; do not retry an action that may have succeeded. After the user returns control, a new call can reacquire the workspace. Manager restart invalidates credentials; the CLI reconnects, while recovery rules still apply.

Guest commands must return before the next call. For a background clipboard process, launch `xclip` with Python `subprocess.Popen`, all three standard streams set to `subprocess.DEVNULL`, `close_fds=True`, and `start_new_session=True`. Shell redirection alone can leave inherited descriptors open and stall the computer tool.

Use the desktop's existing browser and signed-in profile. Check the visible account before account-specific actions. Cookie import or access to an account does not authorize posting, messaging, purchases, or changing settings beyond the user's task.

Humans can run `hotdesk open WORKSPACE` to take control and open its viewer, or add `--observe` for read-only viewing. They return control with `hotdesk release WORKSPACE`. Closing the viewer does not release human control. Ports and tokens do not belong in prompts. If Hot Desk cannot find configuration, use `--config /absolute/path/to/hotdesk.toml` before `agent`, or set the user's selected default with `hotdesk default --config-path PATH`.
