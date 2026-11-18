# Notes for agents working on this repo

codex-remote-control serves a Codex thread as a Claude Code Remote Control
session. `README.md` says what it does and how it is used. This file holds
what neither the README nor the code will tell you.

## Working on it

- `uv run --extra test pytest -q` runs the suite: offline, a couple of seconds.
  `uv run codex-rc` runs the working tree's command.
- The Claude half is `RemoteControlWorker` from
  [claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api), pinned by
  commit in `pyproject.toml`. A change there needs a new pin and `uv lock`
  here. The protocol is written up in that repo's `API_REFERENCE.md`, §5.
- The README is the project's front page. Its quickstart installs from the
  repository URL, and must not depend on a checkout or on one machine's paths.
- Examples and fixtures use made-up ids. Never paste a real thread id, session
  id, account detail or local path into the repo.

## What Codex allows (checked against 0.170.0)

- **Codex takes no third-party slash commands.** Its `/` commands are an enum
  compiled into the TUI (`codex-rs/tui/src/slash_command.rs`), `/presets`
  refuses shell commands, and an unknown `/name` is turned away before any
  hook sees it. That is why the way in is `!codex-rc`: a `!` command runs
  outside the sandbox, without a model turn, with `CODEX_THREAD_ID` set to the
  session it was typed in.
- A `!` command gets the **login shell's PATH**, not the environment `codex`
  was launched with. Changing PATH for the `codex` process does not reach it.
- The bridge joins Codex's **shared app-server daemon**, the one every TUI is
  a client of: `~/.codex/app-server-control/app-server-control.sock`,
  WebSocket over a Unix socket. `codex app-server daemon start` is idempotent.
  `codex app-server proxy` over stdio does not speak JSON lines.
- A Codex started with `-c`, `--enable`, `--disable` or `--search` runs
  **embedded**, off the shared daemon, and cannot be joined: `thread/resume`
  answers "already has an active writer". `-m` alone keeps it on the daemon.
- `thread/settings/update` does not validate a model id, so the bridge checks
  one against `model/list` itself.

## What claude.ai expects

- The session page fails to load ("Something went wrong loading this session")
  if `get_context_usage` is answered with only some of Claude Code's keys.
  Refuse the request or send every key, as `translate.context_usage` does.
- The app's model menu lists Claude models only, so a Codex session reads
  "Unsupported model". `/model`, `/effort` and `/compact`, typed in the app's
  message box, are the bridge's own commands.

## Where state lives

- Which session belongs to which thread is kept on the server, as the session
  tag `codex-thread:<thread id>`. A session's title says whether it was named:
  one starting with `Codex: ` follows its thread, any other is kept.
- Each running bridge holds a locked record in
  `~/.local/state/codex-remote-control/` (`$XDG_STATE_HOME`). The lock, not
  the pid in the record, is what says a bridge is alive.

## Testing against the real services

The offline suite covers the logic. A change to how the bridge talks to Codex
or to claude.ai also needs a run against both, and that run touches a real
Codex plan and a real Claude account:

- **Test turns spend the account's Codex quota.** Run them on the cheapest
  model at low effort, set explicitly from the first turn (`gpt-6.2-sol` at
  `low` when this was written). In a TUI that is `codex -m <model>` and then
  `/model <model> --reasoning=low`, because `-c model_reasoning_effort=low`
  would put the session in embedded mode. Most checks need no turn at all:
  `!` commands are free.
- **Sessions cannot be deleted, only archived.** Every test leaves an archived
  session on the account. Use one thread for a whole run, since serving it
  again finds its session again, then archive the test sessions and the Codex
  test threads.
- A directory Codex has not seen makes the TUI ask for folder trust, and the
  answer is saved to the user's Codex config. Run a test TUI in a directory
  that is already trusted.
- Leave nothing running: stop the bridges a test started (`codex-rc --stop`)
  and check the state directory is empty.
