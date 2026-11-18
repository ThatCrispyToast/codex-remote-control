# codex-remote-control

Open a local Codex thread in the Claude app or at [claude.ai/code](https://claude.ai/code).
Send a prompt from your phone, watch Codex run commands on your machine, and
review diffs and approval requests in the chat.

![Replay of a live Remote Control session. Codex receives a request to read VERSION, runs cat VERSION, and replies that the release is 0.4.2.](docs/demo.gif)

## Quickstart

You need the [Codex CLI](https://github.com/openai/codex) (0.170 or later) and
Claude Code on the same machine. Run `claude`, sign in with `/login`, then quit.

1. Install the bridge with [uv](https://docs.astral.sh/uv/).

   ```sh
   uv tool install git+https://github.com/ThatCrispyToast/codex-remote-control
   ```

2. At the Codex prompt, enter `!codex-rc`.

3. Open the link Codex prints, or select the session under **Remote Control**
   in your Claude account.

If Codex can't find `codex-rc`, run `uv tool update-shell`. For pipx, use
`pipx install git+https://github.com/ThatCrispyToast/codex-remote-control`.

## Managing a session

The default session title is `Codex: <thread name>`, or `Codex: <directory>`
for an unnamed thread. Run `!codex-rc My Project` to set a name that survives
reconnects. A new session includes up to 30 turns of history.

You can close the Codex terminal and keep working from Claude. To rejoin from
the terminal, use `codex resume`. Both show the same thread, and you can answer
an approval in either one.

Run `!codex-rc --stop` inside Codex to disconnect. Running `!codex-rc` again
reopens the same Remote Control session and prints its link. Archiving the
session in Claude stops the bridge. The next connection creates a new session.

### From a shell

`codex-rc` runs in the foreground until you press Ctrl-C.

```sh
codex-rc --last                  # Use this directory's most recent thread
codex-rc --thread <thread-id>    # Use a specific thread
codex-rc                         # Start a thread in this directory
codex-rc -C ~/project My Project # Start a named session in another directory
```

The command prints the session URL and the `codex resume` command for the thread.
Use `-m MODEL` to choose a model for a new thread or `-v` for verbose logs.
To stop a background bridge from a shell, run `codex-rc --stop --thread <thread-id>`.

On Codex 0.170.0, starting a new thread from the shell can fail with
“not materialized yet”. Open Codex, run `!pwd` once, then `!codex-rc`.

## Controls in Claude

Send another message during a turn to steer it. **Stop** interrupts the turn,
though a shell command Codex already started can keep running. The context
meter shows the thread's token usage.

The model picker shows “Unsupported model” for Codex. Use the chat box for
these commands.

| Command | Action |
| --- | --- |
| `/model [id]` | Show or change the Codex model |
| `/effort [level]` | Show or change reasoning effort |
| `/compact` | Compact the thread's context |

The mode menu starts at the closest match to your Codex settings.

| Claude mode | Codex behavior |
| --- | --- |
| Manual | Read-only. Asks before writing. |
| Accept edits | Can edit the workspace. Asks before working outside it. |
| Auto | Same workspace access. The Codex reviewer handles approval requests. |
| Plan | Plans changes without editing files. |

## Compatibility

This is an unofficial integration. It uses Anthropic's private Remote Control
protocol and the experimental Codex app-server API. Tested with Codex 0.170.0
and claude.ai/code on 2026-11-18.

- **“Already has an active writer”** means the Codex session has its own
  app-server. Start Codex without `-c`, `--enable`, `--disable` or `--search`
  so the bridge can join it.
- Answer MCP forms in the Codex terminal. Rewind, file undo, side questions
  (`/btw`), forks, and adding MCP servers from Claude are unsupported.
- If the Codex daemon restarts mid-turn, the bridge reconnects. The interrupted
  turn ends with an error.

## Development

The bridge connects the shared Codex app-server to a Remote Control worker
from [claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api).
[bridge.py](codex_remote_control/bridge.py) handles session events and
[translate.py](codex_remote_control/translate.py) turns Codex commands, edits,
and approval requests into Claude transcript entries and permission cards.

Each thread has one bridge. Its lock file and background log live under
`$XDG_STATE_HOME/codex-remote-control/`, which defaults to
`~/.local/state/codex-remote-control/`.

```sh
git clone https://github.com/ThatCrispyToast/codex-remote-control
cd codex-remote-control
uv run --extra test pytest -q
uv run codex-rc --help
```
