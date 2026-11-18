# codex-remote-control

Control a local Codex session from the Claude app or [claude.ai/code](https://claude.ai/code).
Send prompts, review diffs, and approve commands.

## Quickstart

Install [uv](https://docs.astral.sh/uv/) and [Codex CLI](https://github.com/openai/codex) 0.170+. On the same machine, sign in to Claude Code (`claude`, then `/login`).

1. Install the bridge from your shell.

   ```sh
   uv tool install git+https://github.com/ThatCrispyToast/codex-remote-control
   ```

2. At the Codex prompt, enter `!codex-rc`.
3. Open the printed link or choose the session under **Remote Control** in Claude.

You can close the terminal and keep working from Claude. Run `codex resume` to rejoin.

![Installing codex-rc, then sending a prompt from Claude Desktop and seeing the reply in both windows.](docs/demo.gif)

## Commands

| Where | Command | Action |
| --- | --- | --- |
| Codex | `!codex-rc My Project` | Name the session |
| Codex | `!codex-rc --stop` | Disconnect |
| Claude chat | `/model [id]` | Show or change the model |
| Claude chat | `/effort [level]` | Show or change reasoning effort |
| Claude chat | `/compact` | Compact the thread's context |
| Shell | `codex-rc --last` | Connect this directory's latest thread |
| Shell | `codex-rc --thread ID` | Connect a specific thread |

<details>
<summary>Troubleshooting</summary>

- **Command not found.** Run `uv tool update-shell`.
- **Already has an active writer.** Start Codex without `-c`, `--enable`, `--disable`, or `--search`.
- **Thread not materialized yet.** In Codex, run `!pwd`, then `!codex-rc`.
- **Unsupported model.** Type `/model` in Claude chat.

</details>

Unofficial integration using Anthropic's private Remote Control protocol and the experimental Codex app-server API.
