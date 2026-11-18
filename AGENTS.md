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
- A thread nobody has written to yet is "not materialized": `thread/turns/list`
  refuses it. It has no turns, which is how the bridge reads that refusal.
- An approval's `availableDecisions` for a command are `accept`, a rule
  (`acceptWithExecpolicyAmendment`) and `cancel`. There is no
  `acceptForSession` and no `decline`. The rule is one Codex keeps after the
  session, so the bridge never sends it: "Always allow" is the bridge's own
  memory of that exact command.
- `review/start` reports a review's findings twice, as the `exitedReviewMode`
  item and as an `agentMessage` with the same text, and gives the turn a
  `userMessage` of Codex's own wording.
- `thread/sample` answers a prompt from the thread's context without adding a
  turn. It is what `/btw` runs on, and it spends quota like a turn.
- `thread/compact/start` answers `{}` and the compaction then runs as a turn
  of its own, with one `contextCompaction` item.
- Nothing reports a thread's token usage until its next turn, so the context
  meter reads empty on a bridge that has just attached.

## What claude.ai expects

- The session page fails to load ("Something went wrong loading this session")
  if `get_context_usage` is answered with only some of Claude Code's keys.
  Refuse the request or send every key, as `translate.context_usage` does.
- The app's model menu lists Claude models only, so a Codex session reads
  "Unsupported model".
- The app has its own `/` commands. It runs many itself (`/status`, `/usage`,
  `/help`, `/config`), sends `/model <id>` as `set_model`, and passes others
  on as the text of a user message (`/effort`, `/compact`, `/sandbox`). So a
  message whose first word is a `/command` is never a prompt: the bridge runs
  it or turns it away. Leaving one to the model spends a turn on nonsense.
- The Changes panel needs two things of a session. It must have been created
  with its GitHub repository (`create_session(repository=…)`), else the server
  tags it `config:no-git-repo`. And the worker must report
  `container_cc_version` in its metadata: the app asks for
  `get_workspace_diff` only once it knows that version. A checkout with no
  GitHub remote gets no Changes panel, as with Claude Code.
- `@` mentions and the Files panel are `file_suggestions` (`""`, typed text,
  or `"dir/"`) and `read_file`. `/btw` is `side_question`.
- Attached files arrive as `file_attachments` on the user message, to be
  fetched with the account's login (`download_file`).
- "Always allow" shows on a card only for a `permission_suggestions` rule
  with a `ruleContent`, which in practice means a command. The card says it
  "lasts for this session", whatever the rule's `destination`.
- `system/informational` is not drawn. A notice has to be an assistant
  message to be seen.
- Answers that take time (`get_workspace_diff`, `file_suggestions`,
  `side_question`) are worked out off the bridge's loop and posted back to
  it: the loop is the only thing that writes to the session.

## Where state lives

- Which session belongs to which thread is kept on the server, as the session
  tag `codex-thread:<thread id>`. A session's title says whether it was named:
  one starting with `Codex: ` follows its thread, any other is kept.
- Each running bridge holds a locked record in
  `~/.local/state/codex-remote-control/` (`$XDG_STATE_HOME`). The lock, not
  the pid in the record, is what says a bridge is alive.
- Every `result` event the bridge writes for a Codex turn carries that turn's
  id as `codex_turn`. A bridge taking a session up again reads the newest one
  to know which turns the transcript lacks. A command's answer is a `result`
  with `num_turns: 0` and no turn id.
- Files attached in the app are saved under `uploads/<thread id>/` in the
  state directory, and removed when the thread is closed.

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
- `uv run` syncs the environment to the lockfile, so it cannot run against an
  unpushed claude-rc-api. For that, `uv pip install -e <checkout>` and run
  `.venv/bin/codex-rc` and `.venv/bin/python -m pytest` directly.
- To see what the app sends, run the bridge with `-v` and drive the session
  in a browser. As a controller, claude-rc-api's `request_control()` and
  `send_message()` exercise most handlers without one.
- A file-write approval in read-only mode usually comes as a command approval.
  To get a `fileChange` one, ask Codex to use its `apply_patch` tool.
