---
name: hotdesk
description: Use a local Hot Desk desktop and its authenticated browser for a task. Use when the user asks to use Hot Desk or control one of its workspaces. Supports browser interaction, screenshots, keyboard, pointer, and guest files through Hot Desk MCP tools.
---

Use the connected Hot Desk MCP tools for browser and desktop work. Each MCP connection is bound to one workspace. Use `workspace_status` to check its ownership and readiness. Use the workspace the user requested; if several are connected and none is specified, ask which account/workspace to use. Do not switch accounts because a desktop is busy.

Read the tool schemas supplied by MCP; do not invent tool names or arguments. Prefer browser tools for page interaction and computer tools for desktop actions or coordinate input. Screenshots arrive as image content. If a stopped desktop exposes only workspace tools, call `workspace_acquire` and refresh the tool list.

Browser and computer calls acquire or renew a five-minute reservation. Keep the connection for the task, call `workspace_renew` during long pauses, and call `workspace_release` when finished or abandoned. Do not run concurrent calls on the same connection. Disconnecting attempts release; an abrupt exit leaves expiry and recovery to the manager.

Busy or human-controlled workspaces reject agent actions. On `WORKSPACE_BUSY`, read the owner, task, and checkpoint details. Ask the user whether to wait or create a temporary clone. Give the checkpoint's creation time and explain that newer source changes are missing, local clone changes never merge back, and online account actions still persist. Never clone automatically or switch accounts to bypass ownership.

If the user chooses cloning, run `hotdesk clone SOURCE CLONE --checkpoint CHECKPOINT_ID --user-approved` with the exact approved checkpoint ID. Connect a separate MCP server with `--workspace CLONE`. The clone has an independent writable home and retains local state after release. Save needed outputs outside it, call its `workspace_release` tool, then run `hotdesk discard CLONE --outputs-saved` only when those outputs are safe or none are needed. Discard cannot undo online actions.

Without a checkpoint, report that one requires the source to become idle and stopped. Once it is available, use `hotdesk stop SOURCE`, `hotdesk checkpoint SOURCE`, then `hotdesk start SOURCE`. Do not interrupt an active task to create a checkpoint. Clones use the same resource limits as other desktops.

An acknowledged tool failure preserves the reservation so you can inspect the browser before deciding what to do next. It does not guarantee the action had no effects. Unknown operation outcomes require inspection and recovery; do not retry an action that may have succeeded. After the user returns control, a new call can reacquire the workspace. Manager restart invalidates credentials; the MCP connection reconnects, while recovery rules still apply.

Guest commands must return before the next call. For a background clipboard process, launch `xclip` with Python `subprocess.Popen`, all three standard streams set to `subprocess.DEVNULL`, `close_fds=True`, and `start_new_session=True`. Shell redirection alone can leave inherited descriptors open and stall the computer tool.

Use the desktop's existing browser and signed-in profile. Check the visible account before account-specific actions. Cookie import or access to an account does not authorize posting, messaging, purchases, or changing settings beyond the user's task.

Humans can run `hotdesk open WORKSPACE` to take control and open its viewer, or add `--observe` for read-only viewing. They return control with `hotdesk release WORKSPACE`. Closing the viewer does not release human control. Ports and tokens do not belong in prompts.
