# codex-remote-control

Steer a [Codex](https://github.com/openai/codex) session from claude.ai/code and
the Claude apps, the way you steer a Claude Code session there.

It serves one Codex thread as one Claude Code Remote Control session. The
session is listed under Remote Control next to your Claude Code ones. What you
type there runs as a Codex turn, Codex's replies, commands and edits fill the
transcript, and its approval requests come up as permission cards.

> [!WARNING]
> Unofficial on both sides. The Claude half speaks Anthropic's private Remote
> Control protocol through
> [claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api), and the
> Codex half uses app-server calls Codex marks experimental. Either can change
> without notice. The session shows in your own Claude account only.

## Requirements

- Codex CLI 0.170 or later, signed in (`codex login`).
- Claude Code signed in with a claude.ai account (`claude`, then `/login`).
  The bridge reads that login from `~/.claude`; it does not need `claude` running.
- [uv](https://docs.astral.sh/uv/), or any Python 3.10+ environment.

## Use

From a checkout, `uv run --project <checkout> codex-remote-control` runs it in
whatever directory you are in, and `uv tool install <checkout>` puts the
command on your PATH:

```bash
codex-remote-control                 # a new thread, working in this directory
codex-remote-control -C ~/project    # ... in another one
codex-remote-control --last          # this directory's most recent thread
codex-remote-control <thread-id>     # a given thread, by the id `codex resume` takes
```

It prints where to find both ends, then runs until interrupted:

```
thread   01a1ed4f-3634-7eb3-9b4d-2a3a5f85632e  (/home/me/project)
session  https://claude.ai/code/session_01B2vst…
tui      codex resume 01a1ed4f-3634-7eb3-9b4d-2a3a5f85632e
```

`--name TITLE` sets the session's title (otherwise `Codex: ` and the thread's
name, or its directory until Codex names it), `-m MODEL` the model of a new
thread, `-v` logs what crosses the bridge.

A thread taken up mid-conversation brings its latest 30 turns with it, so the
session reads from near the start rather than from the moment it was served.
Serving the same thread again finds its session again.

## What the app's controls do

| In claude.ai/code | In Codex |
| --- | --- |
| A message | A turn. Sent while one is running, it joins that turn, as typing at the TUI does |
| Stop | Interrupts the turn |
| Mode menu | The thread's settings, below |
| A permission card | Codex's approval request: a command, a patch, extra permissions, or a question it asks you |
| Context meter | The thread's token use against the model's window |
| Rename | The thread's name |
| `/model [id]` | Show or set the model |
| `/effort [level]` | Show or set the reasoning effort |
| `/compact` | Compact the thread's context |

The three slash commands are answered by the bridge, not sent to the model.
They exist because the app's own model menu lists Claude models only: picking
one there is refused, and the session's model label reads "Unsupported model".

Each of the app's modes is one of Codex's own presets:

| Mode | Codex | |
| --- | --- | --- |
| Manual | Read Only | Asks before it changes anything |
| Accept edits | Auto | Works freely inside the workspace, asks to leave it |
| Auto | `--approve-for-me` | The same, with Codex's reviewer agent answering the requests |
| Plan | Plan mode | Plans, changes nothing |

A thread starts in whatever your Codex config gives it, and the session shows
the nearest of these.

Codex's work shows in the transcript as Claude Code's would: a command as a
command with its output, a patch as a file diff, an MCP call and a web search
by name, a reasoning summary as a thinking block.

## With the TUI

Codex keeps one app-server per user and every `codex` TUI is a client of it.
The bridge is another, so the thread it serves is the one a TUI opens:

```bash
codex resume <thread-id>
```

Both then follow the thread live. A message typed at the TUI shows in the app
and one sent from the app shows in the TUI, a mode or model changed in one
moves in the other, and an approval can be answered at either (the first
answer takes the prompt down at the other).

## How it works

```
claude.ai/code ──► Anthropic ──► codex-remote-control ──► Codex app-server ──► the model
  and the apps      session       worker        client      (shared daemon)
                                                               ▲
                                                           codex TUI
```

- **Towards Codex** it is an app-server client on the daemon's control socket
  (`codex_remote_control/appserver.py`): `thread/start` or `thread/resume`,
  then `turn/start`, `turn/steer`, `turn/interrupt`, `thread/settings/update`,
  and answers to the server's approval requests.
- **Towards Anthropic** it holds the session's worker seat, the part a
  `claude` process plays (`RemoteControlWorker` in claude-rc-api): it receives
  what controllers send and writes the transcript they read.
- **In between**, `translate.py` restates Codex's items, approvals and
  settings in Claude Code's terms, and `bridge.py` runs both off one queue.

A session carries the tag `codex-thread:<thread id>`, which is how a thread
finds its session again. Sessions can't be deleted, only archived (in the app):
archive one and the bridge serving it stops, and the thread gets a new session
the next time it is served.

## Limits

- The app's features with no Codex counterpart are refused: rewind and the
  Undo on a file card, side questions (`/btw`), fork, and the MCP servers the
  app tries to add.
- A form an MCP server asks Codex for is not shown. The bridge says so in the
  transcript and leaves it for a TUI to answer.
- Interrupting a turn doesn't kill a command it was running: Codex leaves it
  in the background. The transcript marks it interrupted.
- If the Codex daemon restarts (it updates itself), the bridge reconnects, and
  a turn that was running then ends as an error.

## Development

```bash
uv sync --extra test
uv run --extra test pytest -q    # offline: no Codex, no network
```

```
codex_remote_control/
  appserver.py   # Codex app-server client (JSON-RPC over the daemon's socket)
  translate.py   # Codex's vocabulary in Claude Code's: pure functions
  bridge.py      # one thread served as one session
  cli.py         # the command
tests/           # the bridge between a recorded app-server and worker
```

Checked against Codex 0.170.0 and claude.ai/code as of 2026-11-18.
`codex app-server generate-ts --experimental --out DIR` writes the app-server
protocol's types.
