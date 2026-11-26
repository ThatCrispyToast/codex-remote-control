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
- A side conversation (`/side`, `/btw`) is a thread of its own, forked from
  the one it was opened in, and a `!` command typed in it, or in its pane,
  gets its id. Served as it is, it shows the parent as it stood when the side
  conversation began. A saved one (the experimental `tui_workspace` feature)
  names its parent as `attachedToThreadId`. A temporary one is `ephemeral` and
  names none: `thread/read` answers while it is open, `thread/resume` finds
  "no rollout" and `thread/turns/list` refuses it.
- `request_user_input_async` asks without stopping the turn. The question is
  an `agentMessage` with `delivery: "async"` and `questions` (`title`,
  `options` or null), whose text repeats them as lines. The answer is an
  ordinary user message, a steer while the turn runs: compact JSON
  `[{"answer","question","questionItemId"}]` between
  `<send_user_message_question_reply>` markers, the id being
  `["request_user_input_async","<message id>",<index>]`. Codex's TUI drops a
  question when its turn ends and never asks one again from history.
- `thread/settings/update` does not validate a model id, so the bridge checks
  one against `model/list` itself.
- A thread nobody has written to yet is "not materialized": `thread/turns/list`
  refuses it. It has no turns, which is how the bridge reads that refusal.
- **A TUI joins a thread only once it is on disk.** `codex resume <id>`, with
  or without `--remote`, looks for the thread's rollout file, and for a new
  thread nobody has written to it answers "no rollout found for thread id",
  even while another client holds the thread loaded. `thread/name/set` writes
  it, which is why a new thread given a name is named at once.
  `thread/metadata/update` takes no empty patch, so an unnamed new thread
  can be joined only after its first turn.
- **What a session runs, the app-server runs.** `!` commands and the model's
  commands are children of the app-server process: its cgroup, and its
  environment with the login shell's PATH on top, never the TUI's. With the
  shared daemon that is wherever the daemon was first started.
- **An app-server of one's own:** `codex app-server --listen unix://PATH`
  serves threads by itself, beside the shared daemon and from the same
  `~/.codex`. PATH is made a symlink to a socket under
  `/tmp/codex-daemon-<uid>/` (the socket path length limit), and both go on a
  clean exit. `-c` overrides given to it are the defaults of every thread
  started there (`sandbox_mode`, `approval_policy`, `developer_instructions`,
  the model and effort), in a directory Codex does not trust too, where a
  thread would otherwise start read-only.
- A TUI joined to it (`codex --remote unix://PATH`) takes `-c` without going
  embedded, since it has no server of its own. Resuming a thread it kept the
  thread's model, sandbox and approvals, and asked no folder-trust question in
  a directory Codex had not seen.
- **Two TUI prompts take the next Enter:** the update offer at startup, whose
  first choice runs `npm install -g @openai/codex`, and the nudge to switch
  to a cheaper model as the rate limit nears. A TUI that something else types
  into wants `-c check_for_update_on_startup=false -c
  notice.hide_rate_limit_model_nudge=true`.
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
- A thread nobody has written to takes `thread/settings/update` and
  `thread/name/set` like any other. That is how `/clear` makes its new thread
  stand as the old one stood. `thread/unsubscribe` answers `unsubscribed` and
  ends the old thread's notifications.
- **Fast mode is a service tier** (0.172.0). `model/list` gives each model
  its `serviceTiers`, and the one with id `priority` is the one Codex calls
  Fast, with what it gives and costs as its `description` ("2x speed,
  increased usage"). A thread's `serviceTier` is `priority` for Fast and
  `default` for standard speed, set with `thread/settings/update` for the
  turns after. `fast` is taken for `priority`. Any other string is taken as
  it is, unchecked, like a model id.
- **A tool of one's own is an MCP server** (0.172.0). A client's own tools,
  `dynamicTools`, are taken by `thread/start` and not by `thread/resume`, so
  a thread joined from a TUI can't be given one. Every thread has the MCP
  servers of Codex's config, which is why `send_file` is one.
- Codex starts **an MCP server for each thread**, as a child of the
  app-server: in the thread's directory, with the app-server's environment
  (no `CODEX_THREAD_ID`), and outside the sandbox whatever the thread's mode,
  with the network and the whole disk. Each `tools/call` names its thread in
  `_meta.threadId`. One the model made also has `callId`, the item's id.
- **Codex asks before an MCP tool runs, unless the tool says it is
  read-only.** One with `annotations.readOnlyHint: true` runs unasked in
  every mode. One with no annotations, or with `readOnlyHint: false`, is
  asked about (`mcpServer/elicitation/request`, `codex_approval_kind:
  "mcp_tool_call"`) in read-only and workspace mode. The reviewer answers
  under `auto_review`, and nothing asks under full access.
- An `mcpToolCall` item's `server` is the name in Codex's config. Its
  `result` keeps the tool's `structuredContent` beside `content`. A result
  with `isError: true` ends the item `failed`, with `content` kept and
  `structuredContent` null.
- Codex asks an MCP server for `resources/list` and
  `resources/templates/list` whatever capabilities it declared. Answered
  with an error, the server is still `connected`.
- `codex mcp add` writes the server to `~/.codex/config.toml`. A running
  app-server gives it to the threads started after that. A thread already
  loaded answers "unknown MCP server" until `config/mcpServer/reload`.
- The TUI's own `/new` and `/clear` move that TUI to a new thread. Nothing in
  the app-server moves a TUI from outside, so a terminal on the thread stays
  on the old one after the bridge's `/clear`.
- `thread/backgroundTerminals/list` names what the model left running
  (`command`, `cwd`, `processId`), and `…/clean` kills them all. A command
  started that way ends its turn with its tool call still open.
- Nothing reports a thread's token usage until its next turn, so the context
  meter reads empty on a bridge that has just attached.
- **Codex says nothing while its model thinks.** A `reasoning` item is
  reported once it is over, started and completed in the same instant, and
  with most models its summary is empty. Between a turn's start or a tool's
  result and the next item there is only silence. The bridge takes a second
  of it for thinking.
- An `agentMessage` has a `phase`: `commentary` for what is said on the way,
  `final_answer` for the answer, null from a model that doesn't say.
- A `contextCompaction` item is in progress for as long as its turn waits on
  the compaction, and the `thread/tokenUsage/updated` for what is left comes
  just before it completes. Read in the source, not seen: a compaction Codex
  prepared in the background is reported started and completed together, as
  it is put in place.
- An item's `completedAtMs` is null until it is finished, in
  `thread/turns/list` too.

## What claude.ai expects

- The session page fails to load ("Something went wrong loading this session")
  if `get_context_usage` is answered with only some of Claude Code's keys.
  Refuse the request or send every key, as `translate.context_usage` does.
- The app's model menu lists Claude models only, so a Codex session reads
  "Unsupported model".
- The app has its own `/` commands. It runs many itself (`/status`, `/usage`,
  `/help`, `/config`, `/diff`), sends `/model <id>` as `set_model`, and passes
  others on as the text of a user message (`/effort`, `/compact`, `/sandbox`,
  `/clear`, `/exit`, and any it does not know). So a message whose first word
  is a `/command` is never a prompt: the bridge runs it or turns it away.
  Leaving one to the model spends a turn on nonsense.
- `/fast` is the app's own name too, but one it runs only for a session
  that lists `fast` among its `slash_commands`. Then it offers `/fast
  [on|off]` and sends what was typed as text. For a session that does not
  list it the app answers "/fast isn't a command here." and sends nothing.
  A Claude Code worker reports the mode as `fast_mode_state` (`on`, `off`,
  `cooldown`) in `system/init`, and so does the bridge. The web app draws
  nothing for it.
- Some of the app's own never reach the bridge as text, so the bridge has no
  command of that name: `/plan` is `set_permission_mode`, `/permissions`
  opens the mode menu, `/rename <title>` retitles the session and sends
  `rename_session`, `/tasks` opens the app's Background tasks panel, and
  `/skills` and `/mcp` open the app's own settings.
- The `/` menu lists the app's commands first and the session's
  `slash_commands` after them, so the session's show once the list is
  scrolled or a letter is typed.
- `/clear` empties the transcript on a `conversation_reset` event
  (`new_conversation_id`, `trigger: "clear"`, `user_message_uuid`). The app
  then shows "No messages yet", and a session opened later shows only what
  came after the event. The session and its title carry on.
- An archived session reads "This session is archived" where the message box
  was, with an Unarchive button. Unarchived by hand it is active and
  disconnected until a bridge serves its thread again.
- The Changes panel needs two things of a session. It must have been created
  with its GitHub repository (`create_session(repository=…)`), else the server
  tags it `config:no-git-repo`. And the worker must report
  `container_cc_version` in its metadata: the app asks for
  `get_workspace_diff` only once it knows that version. A checkout with no
  GitHub remote gets no Changes panel, as with Claude Code.
- `@` mentions and the Files panel are `file_suggestions` (`""`, typed text,
  or `"dir/"`) and `read_file`. `/btw` is `side_question`.
- **A file card** is the result of a `SendUserFile` call. The `user` event
  of its `tool_result` carries `tool_use_result: {caption, attachments:
  [{path, size, isImage, media_type, file_uuid}]}`, each `file_uuid` an
  upload's (`POST /api/oauth/file_upload`, with the account's login). The app
  writes the caption, draws a card for each file and lists them under
  Outputs. A picture opens from the upload. Any other file opens in the file
  viewer, which asks for it by its path (`read_file`): one outside the
  thread's directory reads "Can't read this file" there and still downloads.
  A call that failed reads "Sending file Failed".
- Attached files arrive as `file_attachments` on the user message, to be
  fetched with the account's login (`download_file`).
- "Always allow" shows on a card only for a `permission_suggestions` rule
  with a `ruleContent`, which in practice means a command. The card says it
  "lasts for this session", whatever the rule's `destination`.
- An upload to the session takes at most 100 events, or claude.ai answers
  400 ("events: must contain no more than 100 item(s)"). A Codex turn left to
  work for hours holds a thousand items, so the bridge splits its uploads and
  backfills by items as well as turns.
- An `AskUserQuestion` card always adds an "Other" box, and what is typed
  there comes back in `answers` as the answer's text, as a label would.
- `system/informational` is not drawn. A notice has to be an assistant
  message to be seen.
- Answers that take time (`get_workspace_diff`, `file_suggestions`,
  `side_question`) are worked out off the bridge's loop and posted back to
  it: the loop is the only thing that writes to the session.
- "Thinking" beside a turn's clock is drawn for a `thinking` block begun in
  the stream (`message_start`, then `content_block_start`), empty or not,
  until another block begins or the message ends. A turn that is only
  `running` shows the clock alone. The counts in `system/thinking_tokens`
  are not shown.
- `stop_reason: "end_turn"`, on a stored assistant message or in a stream's
  `message_delta`, is the end of the answer: the app stops drawing the session
  as working until something more arrives. Anything said before the answer
  carries null. A turn whose last message carries null still ends on its
  `result`.
- "Compacting session" is drawn from a `system/status` of `compacting`, and
  by the app itself when `/compact` is sent from it, until a status with a
  `compact_result` (`success`, the `compact_boundary` after it, or `failed`)
  or the turn's `result`. A compaction the app did not ask for is taken to be
  over 75 seconds after the last `compacting`, so Claude Code repeats it
  every 30. A boundary whose `post_tokens` is under its `pre_tokens` reads
  "saved N tokens". The app's code also takes `post_tokens` for the size of
  the context from there on.
- A worker that leaves says goodbye (`connection_status: disconnected`), and
  the app shows the session disconnected at once. Without one it stays
  `connected` until the heartbeats have lapsed, a minute. A systemd stop
  sends SIGTERM twice, the second as soon as the unit's main process has
  gone, so only the first one stops the bridge: the second, taken the same
  way, would land in the goodbye and cut it short.
- A session that is only disconnected stays in the app's list of live
  sessions until someone archives it. Claude Code archives its own as it
  exits, and so does the bridge, after the goodbye: an archived session
  refuses its worker's calls, and one archived first stays listed as
  `connected`.

## Where state lives

- Which session belongs to which thread is kept on the server, as the session
  tag `codex-thread:<thread id>`. A session's title says whether it was named:
  one starting with `Codex: ` follows its thread, any other is kept.
- A bridge archives its session as it stops, whatever stopped it, unless
  another worker took the seat. Serving a thread looks for its session among
  the active ones first (one left by a bridge that was killed), then the
  archived, and unarchives it.
- `/clear` moves all of it to the new thread: the session's tag, the bridge's
  record (renamed, so the lock is kept) and its log. The thread left has no
  session after that, and serving it makes a new one.
- Each running bridge holds a locked record in
  `~/.local/state/codex-remote-control/` (`$XDG_STATE_HOME`). The lock, not
  the pid in the record, is what says a bridge is alive. The record also says
  when the bridge started and the session's `state` as the worker last
  reported it to claude.ai (`idle`, `running`, `requires_action`), which is
  how `--list`, or anything counting agents at work, tells a busy bridge from
  an idle one. It is rewritten in place, so a reader can catch it empty.
- Every `result` event the bridge writes for a Codex turn carries that turn's
  id as `codex_turn`. A bridge taking a session up again reads the newest one
  to know which turns the transcript lacks. A command's answer is a `result`
  with `num_turns: 0` and no turn id.
- Files attached in the app are saved under `uploads/<thread id>/` in the
  state directory, and removed when the thread is closed.
- A file Codex sent is kept by claude.ai, in the account's file store, and
  named in the thread: the `send_file` item's result holds the upload's id.
  The bridge keeps nothing of it, and writes the card again from the item
  wherever it reads the thread.
- What a wrapper reads is on stdout: `thread <id>  (<directory>)` first and
  again after each `/clear`, and `exit     typed in the app` last when `/exit`
  ended the bridge. The exit status does not tell `/exit` from a stop: both
  are 0, so that a supervisor restarts neither.

## Testing against the real services

The offline suite covers the logic. A change to how the bridge talks to Codex
or to claude.ai also needs a run against both, and that run touches a real
Codex plan and a real Claude account:

- **Test turns spend the account's Codex quota.** Run them on the cheapest
  model at low effort, set explicitly from the first turn (`gpt-6.2-sol` at
  `low` when this was written). In a TUI that is `codex -m <model>` and then
  `/model <model> --reasoning=low`, because `-c model_reasoning_effort=low`
  would put the session in embedded mode. Most checks need no turn at all:
  `!` commands are free. An app-server of your own started with
  `-c model=… -c model_reasoning_effort=low` makes every thread on it cheap
  from the start, and a `--remote` TUI and `codex-rc --remote` can both join it. `codex debug
  prompt-input` (taking the same `-c` overrides) prints what the model would be
  sent, instructions included, without a turn.
- **Sessions cannot be deleted, only archived.** Every test leaves an archived
  session on the account. Use one thread for a whole run, since serving it
  again finds its session again, then archive the test sessions and the Codex
  test threads.
- A directory Codex has not seen makes the TUI ask for folder trust, and the
  answer is saved to the user's Codex config. Run a test TUI in a directory
  that is already trusted.
- A TUI started with `--enable tui_workspace` offers to restart the shared
  daemon with that feature, which then holds for every client. Pick "Run
  without daemon this time". That TUI is embedded and can't be joined, so test
  a saved side conversation with `CODEX_THREAD_ID=<its id> codex-rc` once the
  TUI has quit.
- `send_file` can be tested without touching Codex's config. An app-server
  of your own takes the server as `-c
  'mcp_servers.codex-rc.command="<checkout>/.venv/bin/codex-rc"' -c
  'mcp_servers.codex-rc.args=["--mcp"]'`, and `mcpServer/tool/call` runs a
  tool on a thread without a turn. `codex mcp add` under an empty
  `CODEX_HOME` writes there, and an app-server on it starts threads without a
  login. Every file sent stays on the account and counts against its
  allowance of uploads.
- Leave nothing running: stop the bridges a test started (`codex-rc --stop`)
  and check the state directory is empty.
- `uv run` syncs the environment to the lockfile, so it cannot run against an
  unpushed claude-rc-api. For that, `uv pip install -e <checkout>` and run
  `.venv/bin/codex-rc` and `.venv/bin/python -m pytest` directly.
- To see what the app sends, run the bridge with `-v` and drive the session
  in a browser. As a controller, claude-rc-api's `request_control()` and
  `send_message()` exercise most handlers without one, commands included:
  `send_message(sid, "/clear")` is what the app sends. Typing commands into
  the app from a script is slow and misfires, since the `/` menu takes the
  first Enter.
- To see what the app draws for a sequence of events, no Codex is needed:
  serve a scratch session from a script built on claude-rc-api's
  `examples/echo_worker.py`, write the events by hand and watch the session
  in a browser. It spends no quota, and leaves an archived session like any
  other test.
- A file-write approval in read-only mode usually comes as a command approval.
  To get a `fileChange` one, ask Codex to use its `apply_patch` tool.
