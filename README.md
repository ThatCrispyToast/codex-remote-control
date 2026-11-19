# codex-remote-control

Control a local Codex session from the official Claude apps or [claude.ai/code](https://claude.ai/code).

![Codex in a terminal beside the same session in Claude Desktop](docs/demo.gif)

## Quickstart

Needs [Codex CLI](https://github.com/openai/codex) 0.170+ and Claude Code on the
same machine, signed in (`claude`, then `/login`).

```sh
# Install
uv tool install git+https://github.com/ThatCrispyToast/codex-remote-control
```
```sh
# In Codex
!codex-rc "optional-session-name"
```

Open the link it prints, or pick the session under **Remote Control** in Claude.

## Features

- Prompts from your phone, desktop or browser; steer or stop a running turn
- Commands, edits and approval requests in the chat, and Codex's questions to
  answer there
- Changes and Files panels, `@` file mentions, attachments, `/btw` side questions
- Model, reasoning effort, compaction and review as slash commands
- Terminal and Claude on the same thread at once

## Installation

**uv:** `uv tool install git+https://github.com/ThatCrispyToast/codex-remote-control`

**pipx:** `pipx install git+https://github.com/ThatCrispyToast/codex-remote-control`

**Codex can't find `codex-rc`:** `uv tool update-shell`. A `!` command gets your
login shell's PATH.

## Usage

```sh
# In Codex
!codex-rc                              # put this session on Remote Control
!codex-rc My Project                   # same, under a name that survives reconnects
!codex-rc --stop                       # take it off; !codex-rc reopens the same session

# From a shell: prints the session link, runs until Ctrl-C
codex-rc                               # new thread in this directory
codex-rc --last                        # this directory's most recent thread
codex-rc --thread <thread-id>          # a specific thread
codex-rc -C ~/project My Project       # new thread in another directory, named
codex-rc -m <model>                    # new thread on that model
codex-rc -v                            # log what crosses the bridge

# Either
codex-rc --list                        # threads on Remote Control from this machine
codex-rc --stop                        # stop the only one
codex-rc --stop --thread <thread-id>   # stop that one
```

- A new session starts with the thread's latest turns: up to 30, fewer when they
  are long.
- Closing the terminal leaves the session up. `codex resume` rejoins it.
- Archiving the session in Claude stops the bridge.
- In a side conversation, `!codex-rc` serves the thread it came from. A temporary
  one can't tell which that is, and says to type it in the main thread.

### In Claude

Typed in the chat box:

| Command | Action |
| --- | --- |
| `/model <id>` | Change the Codex model |
| `/effort [level]` | Show or change reasoning effort |
| `/compact` | Compact the thread's context |
| `/review [instructions]` | Review the uncommitted changes, or what you describe |
| `/btw <question>` | Ask a side question without adding to the thread |
| `/codex` | Show model, sandbox, context, Codex usage limits and thread id |
| `/sandbox` | Show the sandbox and approval settings |

The mode menu:

| Claude mode | Codex behavior |
| --- | --- |
| Manual | Read-only. Asks before writing. |
| Accept edits | Can edit the workspace. Asks before working outside it. |
| Auto | Same workspace access. The Codex reviewer answers approval requests. |
| Plan | Plans changes without editing files. |

Approval cards offer **Deny** and **Allow once**. A command's card adds
**Always allow**, for that exact command while the thread is on Remote Control.

## Limitations

- Unofficial. Uses Anthropic's private Remote Control protocol and the
  experimental Codex app-server API. Tested with Codex 0.170.0 and
  claude.ai/code on 2026-11-18.
- A Codex started with `-c`, `--enable`, `--disable` or `--search` can't be
  joined ("already has an active writer").
- The model picker lists Claude models only and reads "Unsupported model".
  Use `/model`.
- **Stop** ends the turn, but a shell command already running can carry on.
- **Changes** needs a checkout of a GitHub repository.
- MCP forms with text fields have to be answered in the Codex terminal.
- No rewind, file undo, forks, or adding MCP servers from Claude.

## Development

```sh
git clone https://github.com/ThatCrispyToast/codex-remote-control && cd codex-remote-control
uv run --extra test pytest -q   # offline, a couple of seconds
uv run codex-rc --help          # the working tree's command
```

### Layout

| Path | Contents |
| --- | --- |
| `codex_remote_control/cli.py` | Command line; finds or creates a thread's session |
| `codex_remote_control/appserver.py` | Client for Codex's shared app-server daemon |
| `codex_remote_control/bridge.py` | Event loop between the thread and the session |
| `codex_remote_control/translate.py` | Codex items, approvals and settings in Claude Code's terms |
| `codex_remote_control/workspace.py` | Changes and Files panels, from the thread's directory |
| `codex_remote_control/background.py` | Background bridges: lock records, logs, attached files |
| `tests/` | Offline suite |

The Claude side is a Remote Control worker from
[claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api), pinned by
commit. Bridge state lives in `$XDG_STATE_HOME/codex-remote-control/`
(default `~/.local/state/codex-remote-control/`).

Protocol notes and testing against the real services @ [AGENTS.md](AGENTS.md)

## License

MIT. Not affiliated with OpenAI or Anthropic.
