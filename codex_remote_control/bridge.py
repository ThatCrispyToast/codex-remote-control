"""The bridge: one Codex thread served as one Claude Remote Control session.

Two connections meet here. Towards Codex it is an app-server client, like the
TUI: it subscribes to a thread, starts turns on it and answers its approval
requests. Towards Anthropic it is a Remote Control worker, the seat a Claude
Code process normally holds: it receives what claude.ai/code and the Claude
apps send, and writes the transcript they show.

Everything is handled on one thread, in arrival order, off one queue that both
connections feed, so the handlers below share state without locks.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

import httpx
from claude_rc import (
    CLAUDE_CODE_VERSION,
    APIError,
    CredentialsError,
    Event,
    RemoteControlWorker,
    WorkerEvicted,
    cli_control_cancel_request,
    worker_assistant,
    worker_init,
    worker_initialize_response,
    worker_permission_request,
    worker_result,
    worker_status,
    worker_stream_event,
    worker_tool_result,
)

from . import background, translate, workspace
from .appserver import AppServer, AppServerClosed, AppServerError

log = logging.getLogger(__name__)

# How long a dropped app-server connection is retried: the daemon restarts
# itself to update, and is back within seconds.
_RECONNECT_TRIES = 8
# How often a reply that is still being written is passed on to its watchers.
_STREAM_FLUSH_S = 0.3
# What a tool call and a turn still open when the bridge is stopped end as.
_LEFT = "Remote Control was stopped before this finished."
# How much of a thread's history a session is given, to start with or to catch
# up on: its latest turns, up to this many, and only as many items as the
# second number allows. A turn left to work for hours can hold a thousand.
_BACKFILL_TURNS = 30
_BACKFILL_ITEMS = 1000
# The most events claude.ai takes in one upload.
_UPLOAD_EVENTS = 100
# The largest file the app may attach to a message.
_ATTACHMENT_BYTES = 25 * 1024 * 1024
# How far back a session's events are searched for the last turn written to it.
_HELD_EVENTS = 200
# A message whose first word looks like this is a command, as it is in Claude
# Code and in Codex, unless that word is a path that exists.
_COMMAND = re.compile(r"/([A-Za-z][\w:-]*)$")
# The field of a `result` event that names the Codex turn it ends. It is how a
# bridge taking a session up again knows what its transcript already holds.
TURN_FIELD = "codex_turn"
# What the app asks for that a Codex thread has no way to do, and what it is told.
_REFUSALS = {
    "rewind_conversation": "A Codex thread can't be rewound from here.",
    "mcp_set_servers": "A Codex session takes its MCP servers from Codex's own configuration.",
    "set_max_thinking_tokens": "Codex has no thinking budget to set. Use /effort.",
}


# What a session's title starts with unless someone named the session.
TITLE_PREFIX = "Codex: "


def session_title(thread_name: str | None, cwd: str) -> str:
    """What a thread's session is called in the app's list, where it sits
    among Claude Code sessions, unless it was given a name: Codex's name for
    the thread once it has one, its directory until then."""
    return f"{TITLE_PREFIX}{thread_name or os.path.basename(cwd) or cwd}"


def _settings(opened: dict) -> dict:
    """A thread's settings as `thread/start` and `thread/resume` answer them,
    in the shape `thread/settings/updated` reports them in from then on."""
    return {
        "model": opened["model"],
        "effort": opened.get("reasoningEffort"),
        "cwd": opened["cwd"],
        "approvalPolicy": opened["approvalPolicy"],
        "approvalsReviewer": opened.get("approvalsReviewer", "user"),
        "sandboxPolicy": opened["sandbox"],
        "collaborationMode": opened.get("collaborationMode"),
    }


def _latest(turns: list[dict], items: int) -> list[dict]:
    """The newest of ``turns`` (newest first) that hold no more than ``items``
    items between them, and the newest turn whatever its size."""
    kept: list[dict] = []
    for turn in turns:
        items -= len(turn["items"])
        if kept and items < 0:
            break
        kept.append(turn)
    return kept


class ThreadClosed(Exception):
    """The Codex thread was closed or archived: there is nothing left to serve."""


class Bridge:
    def __init__(self, codex: AppServer, worker: RemoteControlWorker, opened: dict, *, follow_name: bool):
        """``opened`` is the result of the `thread/start` or `thread/resume`
        that subscribed ``codex`` to the thread. With ``follow_name`` the
        session is retitled as Codex names the thread."""
        self.codex = codex
        self.worker = worker
        self.thread: dict = opened["thread"]
        self.thread_id: str = self.thread["id"]
        self.follow_name = follow_name
        self.settings = _settings(opened)
        self._models: dict[str, list[str]] | None = None  # see models()
        self.mcp: list[dict] = []  # Codex's MCP servers, as the app lists a session's
        self.repository = workspace.repository(self.settings["cwd"])  # the checkout's GitHub repository and branch
        self.refused_models: set[str] = set()  # models asked for that Codex lacks, each explained once
        self.turn: str | None = None  # the running Codex turn
        self.last_turn: str | None = None  # the latest turn the thread has, running or not
        self.inputs: list[Event] = []  # the controller messages that turn answers
        self.queued: list[Event] = []  # controller messages waiting for it to end
        self.replies: list[str] = []  # ... and answers to Codex's questions, from the app
        self.sent: set[str] = set()  # ids of the user messages this bridge submitted
        self.calls: dict[str, list[dict]] = {}  # running tool items' tool_use blocks, by item id
        self.interrupted: set[str] = set()  # tool items closed with their turn, whose real end is still to come
        self.backfilled: set[str] = set()  # turns written from the thread's history, see backfill()
        self.written: set[str] = set()  # ... and the finished items of the turn running then
        self.compacting_from: dict | None = None  # the usage as a compaction began
        self.asked_to_compact = False  # /compact was typed: the next compaction is not Codex's own
        self.own_prompt = False  # the running turn was started by a command: its prompt is Codex's wording, not the user's
        self.review: str | None = None  # a review's findings, until the message that repeats them arrives
        self.prompts: dict[str, dict] = {}  # open permission prompts: request id -> the app-server request
        self.allowed: set[str] = set()  # commands given "Always allow", which lasts as long as this bridge serves
        self.usage: dict | None = None  # the thread's token usage, as last reported
        self.usage_before: dict | None = None  # ... and as the running turn began
        self.last_text = ""  # the turn's latest agent message: its result
        self.watched = True  # whether a controller has the session open, by the last upload's ack
        self.stream: str | None = None  # the message or thought being written, by item id
        self.stream_kind = "text"  # ... which of the two: "text" or "thinking"
        self.stream_open = False  # ... whether the watchers have its start
        self.stream_text = ""  # ... and its text not yet passed on to them
        self.stream_sent_at = 0.0
        self._inbox: queue.Queue = queue.Queue()

    # -- the loop ------------------------------------------------------------
    def run(self) -> None:
        """Serve until the worker seat is lost (:class:`claude_rc.WorkerEvicted`),
        the thread closes (:class:`ThreadClosed`), or the app-server stays gone
        (:class:`AppServerClosed`)."""
        self.mcp = self._mcp_servers()
        self._announce()
        self._adopt_running_turn()
        self._pump("rc", self.worker.events())
        try:
            while True:
                self._pump("codex", self.codex.messages())
                self._serve()  # returns when the app-server connection drops
                self._reconnect()
        except KeyboardInterrupt:
            self._leave()
            raise

    def _pump(self, source: str, items: Iterable) -> None:
        def pump() -> None:
            try:
                for item in items:
                    self._inbox.put((source, item))
            except Exception as e:  # handed to the loop, which decides
                self._inbox.put((source, e))

        threading.Thread(target=pump, name=f"bridge-{source}", daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                source, item = self._inbox.get(timeout=_STREAM_FLUSH_S if self.stream_text else None)
            except queue.Empty:
                self._stream_flush()
                continue
            if isinstance(item, AppServerClosed):
                return
            if isinstance(item, Exception):
                raise item
            try:
                if source == "task":
                    item()
                elif source == "rc":
                    self._from_controller(item)
                else:
                    self._from_codex(item)
            except AppServerClosed:
                return
            except (WorkerEvicted, ThreadClosed):
                raise
            except Exception:
                # One event that can't be passed on (a refused upload, a shape
                # this version doesn't know) is no reason to drop the session.
                log.exception("could not handle %s", item.get("method") if source == "codex" else item)

    def _later(self, request: Event, work: Callable[[], dict]) -> None:
        """Answer a control request with what ``work`` returns, worked out off
        the loop: it waits on git or on a model, and the session's traffic
        should not wait behind it."""
        def run() -> None:
            try:
                response = work()
            except Exception as e:  # the request gets its answer either way
                refusal = str(e)
                self._inbox.put(("task", lambda: self.worker.reject(request, refusal)))
            else:
                self._inbox.put(("task", lambda: self.worker.respond(request, response)))

        threading.Thread(target=run, name="bridge-task", daemon=True).start()

    def _leave(self) -> None:
        """Stopped mid-turn, the bridge will not see that turn end, and the
        `--stop` typed in a session always is mid-turn: it is a command in
        one. Close what the app shows as open. Codex carries on regardless."""
        if self.turn is None:
            return
        try:
            for request_id in list(self.prompts):
                self._withdraw_prompt(request_id)
            for calls in self.calls.values():
                self._send(*(worker_tool_result(call["id"], _LEFT) for call in calls))
            self.worker.end_turn(*self.inputs, result=_LEFT, **{TURN_FIELD: self.turn})
        except Exception:  # the stop goes ahead either way
            log.debug("could not close the running turn", exc_info=True)

    def _turns(self, limit: int, *, items: bool = False) -> list[dict]:
        """The thread's latest turns, newest first, with their items if asked.
        A thread nobody has written to yet has none, and Codex refuses to list
        them: it keeps no record of a thread before its first message."""
        try:
            return self.codex.request("thread/turns/list", {
                "threadId": self.thread_id, "limit": limit, "sortDirection": "desc",
                **({"itemsView": "full"} if items else {}),
            })["data"]
        except AppServerError as e:
            if "not materialized" in str(e):
                return []
            raise

    def _adopt_running_turn(self) -> None:
        """A thread taken up mid-turn (one a TUI set going): find that turn, so
        the app shows the session working and can steer or stop it."""
        latest = self._turns(1)
        if latest:
            self.last_turn = latest[0]["id"]
        if latest and latest[0]["status"] == "inProgress":
            self._turn_began(latest[0]["id"])
            self.worker.report_state("running")

    def backfill(self, *, new: bool) -> None:
        """Write into the session what its transcript lacks of the thread, so
        it reads the same in the app as in a Codex terminal. A session made
        just now gets the thread's latest turns. One served before gets the
        turns since the last one written to it: what was done in a terminal
        while the thread was off Remote Control.

        A turn that ended while the bridge was attaching is in this history
        and also in the notifications waiting to be read (the command that
        started the bridge from inside its session always is), so what Codex
        still says about a turn written here is dropped."""
        turns = self._turns(_BACKFILL_TURNS, items=True)
        if not new:
            held = self._held_turn()
            if held is None:
                return
            ids = [turn["id"] for turn in turns]
            if held in ids:
                turns = turns[:ids.index(held)]
        for turn in reversed(_latest(turns, _BACKFILL_ITEMS)):
            if turn["status"] == "inProgress":
                self._write_running(turn)
                continue
            events = translate.turn_transcript(turn["items"], self.model)
            if events:
                done = worker_result(duration_ms=turn.get("durationMs") or 0, **{TURN_FIELD: turn["id"]})
                self._send(*events, done)
            self.backfilled.add(turn["id"])

    def _held_turn(self) -> str | None:
        """The newest Codex turn the session's transcript holds: the `result`
        a bridge ends a turn with names it. "" if the transcript holds no
        turn, None if it can't be told which (one written before turns were
        named, or too long ago to find)."""
        try:
            events = self.worker.client.list_events(self.worker.session_id, limit=_HELD_EVENTS, sort_order="desc")
        except (APIError, httpx.HTTPError) as e:  # the session is served all the same, from here on
            log.warning("could not read the session's transcript to catch it up: %s", e)
            return None
        for event in events:
            if event.payload.get("type") == "result" and event.payload.get("num_turns") != 0:  # 0: a command's answer
                return event.payload.get(TURN_FIELD)
        return "" if len(events) < _HELD_EVENTS else None

    def _write_running(self, turn: dict) -> None:
        """Write the running turn as far as it has got: what has finished, and
        the tool calls still open, which their own end then closes. A message
        or a thought at its end may still be being written, and arrives
        finished."""
        items = turn["items"]
        if items and items[-1]["type"] in ("agentMessage", "reasoning", "plan"):
            items = items[:-1]
        events = []
        for item in items:
            if item.get("status") != "inProgress":
                events += translate.transcript(item, self.model)
                self.written.add(item["id"])
            elif calls := translate.tool_calls(item):
                self.calls[item["id"]] = calls
                events.append(worker_assistant(calls, model=self.model))
        if events:
            self._send(*events)

    def _reconnect(self) -> None:
        """The app-server went away, and the running turn with it. Close that
        turn for the controllers, then subscribe to the thread again."""
        log.warning("lost the Codex app-server; reconnecting")
        if self.turn is not None:
            self._turn_completed({"turn": {"status": "failed", "error": {"message": "The Codex app-server restarted mid-turn."}}})
        for attempt in range(_RECONNECT_TRIES):
            time.sleep(min(30, 2**attempt))
            try:
                codex = AppServer()
                codex.connect()
                opened = codex.request("thread/resume", {"threadId": self.thread_id, "excludeTurns": True})
            except (OSError, AppServerError, AppServerClosed, TimeoutError) as e:
                log.warning("reconnect %d/%d failed: %s", attempt + 1, _RECONNECT_TRIES, e)
                continue
            self.codex = codex
            self._settings_updated({"threadSettings": _settings(opened)})
            return
        raise AppServerClosed("the Codex app-server did not come back")

    def _send(self, *payloads: dict) -> None:
        """Write transcript events, in as many uploads as claude.ai needs, and
        note whether anyone is watching."""
        for start in range(0, len(payloads), _UPLOAD_EVENTS):
            self.watched = self.worker.send(*payloads[start:start + _UPLOAD_EVENTS]).get("has_subscribers", True)

    # -- what the session says about itself ----------------------------------
    @property
    def model(self) -> str:
        return self.settings["model"]

    @property
    def mode(self) -> str:
        return translate.permission_mode(self.settings)

    def _announce(self) -> None:
        """Tell the controllers the session's settings: `system/init`, which
        they read the model, mode and effort from."""
        effort = self.settings.get("effort")
        self.worker.send(worker_init(
            model=self.model, cwd=self.settings["cwd"], permission_mode=self.mode,
            slash_commands=sorted(self._COMMANDS), mcp_servers=self.mcp, **({"effort": effort} if effort else {}),
        ))
        self.worker.report_metadata(
            model=self.model, effort_level=effort, permission_mode=self.mode,
            # The Claude Code this worker stands in for: the app asks for the
            # workspace's diff only once it knows the version can give one.
            container_cc_version=CLAUDE_CODE_VERSION,
            **({"current_branches": {self.repository[0]: self.repository[1]}} if self.repository and self.repository[1] else {}),
        )

    def models(self) -> dict[str, list[str]]:
        """Codex's models, each with the reasoning efforts it takes."""
        if self._models is None:
            self._models = {
                m["id"]: [e["reasoningEffort"] for e in m["supportedReasoningEfforts"]]
                for m in self.codex.request("model/list", {})["data"]
            }
        return self._models

    def _mcp_servers(self) -> list[dict]:
        """Codex's MCP servers and whether each is up, for the app's status card."""
        try:
            listed = self.codex.request("mcpServerStatus/list", {"threadId": self.thread_id, "detail": "toolsAndAuthOnly"})
        except (AppServerError, TimeoutError) as e:
            log.debug("could not list Codex's MCP servers: %s", e)
            return self.mcp
        return translate.mcp_servers(listed["data"])

    def _mcp_updated(self, _: dict) -> None:
        servers = self._mcp_servers()
        if servers != self.mcp:
            self.mcp = servers
            self._announce()

    def _note(self, text: str) -> None:
        """Say something in the transcript that no message asked for."""
        self._send(worker_assistant(text, model=self.model, stop_reason="end_turn"))
        if self.turn is None:  # a running turn's own result is still to come
            self._send(worker_result(result=text, num_turns=0))

    def _settings_updated(self, params: dict) -> None:
        """The thread's settings changed, here or at a TUI."""
        was = (self.model, self.mode, self.settings.get("effort"), self.settings["cwd"])
        self.settings = params["threadSettings"]
        if was != (self.model, self.mode, self.settings.get("effort"), self.settings["cwd"]):
            self._announce()

    def _update_settings(self, **fields: Any) -> None:
        """Change the thread's settings for its coming turns. Codex confirms
        with `thread/settings/updated`, which is what moves this session's."""
        self.codex.request("thread/settings/update", {"threadId": self.thread_id, **fields})

    # -- from the controllers ------------------------------------------------
    def _from_controller(self, event: Event) -> None:
        log.debug("claude.ai: %r %.600s", event, event.payload.get("request") or "")
        if event.type == "user":
            self._user_message(event)
        elif event.type == "control_request":
            self._control_request(event)
        elif event.type == "control_response":
            self._prompt_answered(event)
        else:  # a control_cancel_request for something already answered
            self.worker.report_delivery(event, "processed")

    def _user_message(self, message: Event) -> None:
        words = message.text().split(None, 1)
        if words and (command := _COMMAND.match(words[0])) and not os.path.exists(words[0]):
            self._command(message, command[1].lower(), words[1].strip() if len(words) > 1 else "")
            return
        if self.turn is None:
            self._start_turn([message])
            return
        # A message sent mid-turn joins the running turn, as it does at the
        # TUI. If the turn won't take it (it just ended), it gets the next one.
        self.sent.add(message.id)
        try:
            self.codex.request("turn/steer", {
                "threadId": self.thread_id, "expectedTurnId": self.turn,
                "input": self._input(message), "clientUserMessageId": message.id,
            })
        except AppServerError:
            self.queued.append(message)
            return
        self.worker.report_delivery(message, "processing")
        self.inputs.append(message)

    def _input(self, message: Event) -> list[dict]:
        """A message as Codex turn input, with the files the app attached to
        it fetched and saved where Codex can read them."""
        attached = [self._fetch(file) for file in message.payload.get("file_attachments") or []]
        return translate.user_input(message, attached)

    def _fetch(self, attachment: dict) -> dict:
        name = os.path.basename(str(attachment.get("file_name") or "")) or "file"
        try:
            data = self.worker.client.download_file(attachment["file_uuid"], max_bytes=_ATTACHMENT_BYTES)
            path = background.save_upload(self.thread_id, attachment["file_uuid"], name, data)
        except (APIError, CredentialsError, httpx.HTTPError, OSError, KeyError) as e:
            log.warning("could not fetch the attached file %s: %s", name, e)
            return {"name": name}  # Codex is told it is missing, and says so
        return {"name": name, "path": str(path), "image": bool(attachment.get("is_image"))}

    def _start_turn(self, messages: list[Event]) -> None:
        self.sent.update(m.id for m in messages)
        self.inputs = list(messages)
        self.worker.begin_turn(*messages)
        try:
            turn = self.codex.request("turn/start", {
                "threadId": self.thread_id,
                "input": [part for m in messages for part in self._input(m)],
                "clientUserMessageId": messages[0].id,
            })["turn"]
        except AppServerError as e:
            self._turn_completed({"turn": {"status": "failed", "error": {"message": str(e)}}})
            return
        self._turn_began(turn["id"])

    # -- commands: a message that is not for the model -------------------------
    def _command(self, message: Event, name: str, argument: str) -> None:
        """A `/command` typed in the app. The app runs many of its own and
        passes the rest on as text, which Claude Code would run. None of them
        is a prompt, so none reaches the model: one this session has no
        answer to is turned away, as Codex's own terminal would."""
        handler = self._COMMANDS.get(name)
        if handler is None:
            known = ", ".join(f"`/{command}`" for command in sorted(self._COMMANDS))
            self._answer_here(message, f"`/{name}` isn't available on a Codex session. This one takes {known}.")
            return
        try:
            answer = handler(self, argument, message)
        except AppServerError as e:
            answer = f"Codex refused: {e}"
        if answer is not None:
            self._answer_here(message, answer)

    def _answer_here(self, message: Event, answer: str) -> None:
        """Answer a message without a Codex turn, as Claude Code answers a
        slash command."""
        self.worker.report_delivery(message, "processing")
        self._send(worker_assistant(answer, model=self.model, stop_reason="end_turn"))
        if self.turn is None:
            self.worker.end_turn(message, result=answer, num_turns=0)
        else:  # mid-turn: the running turn's result is still to come
            self.worker.report_delivery(message, "processed")

    def _run(self, message: Event, method: str, **params: Any) -> str | None:
        """Start a turn that is not a message to the model (a review, a
        compaction) for the command that asked for it, which then waits on it
        as a message waits on its turn. Returns what to answer instead if it
        can't be started."""
        if self.turn is not None:
            return "Codex is in the middle of a turn. Try again when it is done."
        self.sent.add(message.id)
        self.inputs = [message]
        self.worker.begin_turn(message)
        try:
            started = self.codex.request(method, {"threadId": self.thread_id, **params})
        except AppServerError as e:
            self._turn_completed({"turn": {"status": "failed", "error": {"message": f"Codex refused: {e}"}}})
            return None
        if turn := (started or {}).get("turn"):  # else `turn/started` names it
            self._turn_began(turn["id"])
        self.own_prompt = True
        return None

    def _model_command(self, model: str, _: Event) -> str:
        if not model:
            return f"Model: `{self.model}`. Codex has: {', '.join(f'`{m}`' for m in self.models())}."
        if model not in self.models():
            return f"Codex has no model `{model}`. It has: {', '.join(f'`{m}`' for m in self.models())}."
        self._update_settings(model=model)
        return f"Model set to `{model}`, from the next turn."

    def _effort_command(self, effort: str, _: Event) -> str:
        efforts = self.models().get(self.model, [])
        if not effort:
            return f"Reasoning effort: `{self.settings.get('effort')}`. `{self.model}` takes: {', '.join(efforts)}."
        if effort not in efforts:
            return f"`{self.model}` has no effort `{effort}`. It takes: {', '.join(efforts)}."
        self._update_settings(effort=effort)
        return f"Reasoning effort set to `{effort}`, from the next turn."

    def _compact_command(self, _: str, message: Event) -> str | None:
        if self.last_turn is None:
            return "Nothing to compact yet."
        answer = self._run(message, "thread/compact/start")
        self.asked_to_compact = self.own_prompt  # if it was started
        return answer

    def _review_command(self, instructions: str, message: Event) -> str | None:
        target = {"type": "custom", "instructions": instructions} if instructions else {"type": "uncommittedChanges"}
        return self._run(message, "review/start", target=target, delivery="inline")

    def _sandbox_command(self, _: str, __: Event) -> str:
        return translate.sandbox(self.settings)

    def _codex_command(self, _: str, __: Event) -> str:
        try:
            limits = self.codex.request("account/rateLimits/read", {}).get("rateLimits")
        except (AppServerError, TimeoutError):
            limits = None  # signed in with an API key, or offline: the rest still stands
        return translate.report(
            thread=self.thread, settings=self.settings, usage=self.usage, rate_limits=limits, models=list(self.models()),
        )

    # Typed in the app's message box, these are answered here rather than
    # sent to the model. The app adds them to its own in its `/` menu.
    _COMMANDS: ClassVar[dict[str, Callable[[Bridge, str, Event], str | None]]] = {
        "model": _model_command,
        "effort": _effort_command,
        "compact": _compact_command,
        "review": _review_command,
        "sandbox": _sandbox_command,
        "codex": _codex_command,
    }

    # -- control requests: what the app's menus and panels ask ------------------
    def _control_request(self, request: Event) -> None:
        handler = self._CONTROLS.get(request.control_subtype)
        try:
            if handler is None:  # refused as Claude Code refuses what it doesn't serve
                refusal = _REFUSALS.get(request.control_subtype)
                self.worker.reject(
                    request, refusal or f"codex-rc does not handle control_request subtype: {request.control_subtype}",
                    error_code="unsupported_request",
                )
            else:
                handler(self, request, request.payload["request"])
        except AppServerError as e:  # every request gets an answer: its sender waits for one
            self.worker.reject(request, f"Codex refused: {e}")

    def _initialize(self, request: Event, _: dict) -> None:  # every controller sends it as it attaches
        self.worker.respond(
            request,
            worker_initialize_response(model=self.model, permission_mode=self.mode),
            pending_permission_requests=[self._prompt_payload(rid) for rid in self.prompts],
            pending_user_dialog_requests=[],
        )
        self._announce()

    def _interrupt(self, request: Event, _: dict) -> None:
        if self.turn is not None:
            self.codex.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn})
        self.worker.respond(request)

    def _set_model(self, request: Event, fields: dict) -> None:
        model = fields.get("model")
        if model in self.models():
            self._update_settings(model=model)
            self.worker.respond(request, {"model": model})
            return
        has = ", ".join(f"`{m}`" for m in self.models())
        self.worker.reject(request, f"Codex has no model {model!r}. It has: {has}")
        if model not in self.refused_models:  # the app's own menu offers Claude's models: say why not, once
            self.refused_models.add(model)
            self._note(f"This is a Codex session, so it runs Codex's models: {has}. Switch with `/model <id>`.")

    def _set_permission_mode(self, request: Event, fields: dict) -> None:
        if fields.get("mode") not in translate.MODES:
            self.worker.reject(request, f"no such permission mode: {fields.get('mode')}", error_code="invalid_mode")
            return
        self._update_settings(**translate.mode_settings(fields["mode"], self.settings))
        self.worker.respond(request)

    def _apply_flag_settings(self, request: Event, fields: dict) -> None:
        level = (fields.get("settings") or {}).get("effortLevel")
        efforts = self.models().get(self.model, [])
        if level and level not in efforts:
            self.worker.reject(request, f"`{self.model}` has no effort {level!r}. It takes: {', '.join(efforts)}")
            return
        if level:
            self._update_settings(effort=level)
        self.worker.respond(request)

    def _get_context_usage(self, request: Event, _: dict) -> None:
        self.worker.respond(request, translate.context_usage(self.usage, self.model))

    def _get_workspace_diff(self, request: Event, _: dict) -> None:
        cwd = self.settings["cwd"]
        self._later(request, lambda: {"diff": workspace.diff(cwd)})

    def _file_suggestions(self, request: Event, fields: dict) -> None:
        query, cwd, codex = (fields.get("query") or "").strip(), self.settings["cwd"], self.codex

        def find() -> dict:
            paths = workspace.entries(cwd, query) if not query or query.endswith("/") else None
            if paths is None:  # not a folder to open: something to search for
                found = codex.request("fuzzyFileSearch", {"query": query, "roots": [cwd], "cancellationToken": None})
                paths = workspace.suggestions(found["files"])
            return {"suggestions": [{"path": path} for path in paths]}

        self._later(request, find)

    def _read_file(self, request: Event, fields: dict) -> None:
        try:
            self.worker.respond(request, workspace.read(self.settings["cwd"], fields.get("path") or "", fields.get("max_bytes")))
        except workspace.ReadDenied as e:
            self.worker.reject(request, f"read denied: {e}")

    def _side_question(self, request: Event, fields: dict) -> None:
        """The app's `/btw`: a question answered from the conversation, beside
        it. Codex samples the answer from the thread without adding a turn."""
        turn = self.turn or self.last_turn
        if turn is None:  # nothing said yet to answer from
            self.worker.respond(request, {"response": None, "synthetic": False})
            return
        codex, params = self.codex, {
            "threadId": self.thread_id, "expectedTurnId": turn, "allowInProgressTurn": True, "disableMcp": True,
            "prompt": translate.side_prompt(fields.get("question") or "", fields.get("history")),
        }
        self._later(request, lambda: {
            "response": codex.request("thread/sample", params, timeout=300.0).get("text"), "synthetic": False,
        })

    def _cancel_async_message(self, request: Event, fields: dict) -> None:
        waiting = next((m for m in self.queued if m.id == fields.get("message_uuid")), None)
        if waiting is not None:
            self.queued.remove(waiting)
            self.worker.report_delivery(waiting, "processed")
        self.worker.respond(request, {"cancelled": waiting is not None})

    def _mcp_status(self, request: Event, _: dict) -> None:
        self.mcp = self._mcp_servers()
        self.worker.respond(request, {"mcpServers": self.mcp})

    def _rename_session(self, request: Event, fields: dict) -> None:
        self.codex.request("thread/name/set", {"threadId": self.thread_id, "name": fields["title"]})
        self.worker.respond(request)

    def _fork_conversation(self, request: Event, _: dict) -> None:
        # Claude Code's own answer where it can't fork, which the app knows how to show.
        self.worker.respond(request, {"forked": False, "reason": "requires_remote_control_server"})

    def _end_session(self, request: Event, _: dict) -> None:
        self.worker.respond(request)  # archived in the app: the seat is taken away next

    _CONTROLS: ClassVar[dict[str, Callable[[Bridge, Event, dict], None]]] = {
        "initialize": _initialize,
        "interrupt": _interrupt,
        "set_model": _set_model,
        "set_permission_mode": _set_permission_mode,
        "apply_flag_settings": _apply_flag_settings,
        "get_context_usage": _get_context_usage,
        "get_workspace_diff": _get_workspace_diff,
        "file_suggestions": _file_suggestions,
        "read_file": _read_file,
        "side_question": _side_question,
        "cancel_async_message": _cancel_async_message,
        "mcp_status": _mcp_status,
        "rename_session": _rename_session,
        "fork_conversation": _fork_conversation,
        "end_session": _end_session,
    }

    # -- from Codex ------------------------------------------------------------
    def _from_codex(self, message: dict) -> None:
        params = message.get("params") or {}
        if params.get("threadId", self.thread_id) != self.thread_id:
            return  # the daemon is shared: another thread's traffic
        if (params.get("turnId") or (params.get("turn") or {}).get("id")) in self.backfilled:
            return  # in the transcript already
        if not message["method"].endswith(("elta", "/updated")):
            log.debug("codex: %s %s", message["method"], (params.get("item") or {}).get("type", ""))
        if "id" in message:
            log.debug("codex asks: %.800s", params)
            self._server_request(message)
        elif (handler := self._NOTIFICATIONS.get(message["method"])) is not None:
            handler(self, params)

    def _turn_started(self, params: dict) -> None:
        if params["turn"]["id"] != self.turn:  # started at the TUI, not here
            self._turn_began(params["turn"]["id"])
            self.worker.report_state("running")

    def _turn_began(self, turn_id: str) -> None:
        self.turn = turn_id
        self.usage_before = self.usage
        self.last_text = ""

    def _item_started(self, params: dict) -> None:
        item = params["item"]
        if item["id"] in self.written or item["id"] in self.calls:
            return  # written from the thread's history as the bridge attached
        if item["type"] == "contextCompaction":
            self.compacting_from = self.usage
            self._send(worker_status("compacting"))
            return
        if item["type"] == "webSearch":  # its query is known only once it is done
            return
        if calls := translate.tool_calls(item):
            self.calls[item["id"]] = calls
            self._send(worker_assistant(calls, model=self.model))

    def _item_completed(self, params: dict) -> None:
        item = params["item"]
        kind = item["type"]
        if item["id"] in self.written:
            return  # written from the thread's history as the bridge attached
        if kind == "userMessage" and item.get("clientId") in self.sent:
            return  # sent from the app, where it shows already. One typed at a TUI goes on.
        if kind == "userMessage":
            self._questions_answered(item)
        if item["id"] in self.interrupted:
            self.interrupted.discard(item["id"])  # closed with its turn already
            return
        if kind == "contextCompaction":
            trigger, self.asked_to_compact = "manual" if self.asked_to_compact else "auto", False
            self._send(translate.compact_boundary(trigger, self.compacting_from), worker_status(None))
            self.last_text = "Context compacted."
            return
        if kind == "userMessage" and self.own_prompt:
            return  # what Codex makes of a command typed in the app, which shows there as typed
        if kind == "exitedReviewMode":
            self.review = item.get("review")  # written when the turn ends, unless a message says it first
            return
        events = translate.transcript(item, self.model)
        if self.calls.pop(item["id"], None):
            events = events[1:]  # the call itself went out as it started
        if kind == "agentMessage":
            self.last_text = item["text"] or self.last_text
            if item["text"] == self.review:
                self.review = None
        written_live = kind in ("agentMessage", "reasoning") and self.stream == item["id"]
        if written_live:
            self._stream_flush(end=True)
        if events:
            self._send(*events)
        if written_live:
            self._stream_stop()
        if kind == "agentMessage":
            for question in translate.async_questions(item):
                self._ask(question)

    def _message_delta(self, params: dict) -> None:
        self._stream_add(params["itemId"], "text", params["delta"])

    def _thinking_delta(self, params: dict) -> None:
        self._stream_add(params["itemId"], "thinking", params["delta"])

    def _thinking_part(self, params: dict) -> None:
        if params.get("summaryIndex"):  # a thought's summary comes in parts: a paragraph each
            self._stream_add(params["itemId"], "thinking", "\n\n")

    def _stream_add(self, item_id: str, kind: str, text: str) -> None:
        """A piece of a message or a thought still being written. Pieces are
        passed on a few times a second, and only while someone is watching:
        the finished item is what the transcript keeps."""
        if not self.watched:
            return
        if self.stream != item_id:
            if self.stream is not None:  # one that never finished: close it for its watchers
                self._stream_flush(end=True)
                self._stream_stop()
            self.stream, self.stream_kind = item_id, kind
        self.stream_text += text
        if time.monotonic() - self.stream_sent_at >= _STREAM_FLUSH_S:
            self._stream_flush()

    def _stream_flush(self, *, end: bool = False) -> None:
        """Pass on the text gathered since the last flush, as the Messages
        API's streaming events. ``end`` closes the block."""
        thinking = self.stream_kind == "thinking"
        events = []
        if self.stream_text:
            if not self.stream_open:
                self.stream_open = True
                message = {
                    "id": self.stream, "type": "message", "role": "assistant", "model": self.model, "content": [],
                    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
                }
                block = {"type": "thinking", "thinking": ""} if thinking else {"type": "text", "text": ""}
                events.append({"type": "message_start", "message": message})
                events.append({"type": "content_block_start", "index": 0, "content_block": block})
            delta = (
                {"type": "thinking_delta", "thinking": self.stream_text} if thinking
                else {"type": "text_delta", "text": self.stream_text}
            )
            events.append({"type": "content_block_delta", "index": 0, "delta": delta})
        if end and self.stream_open:
            events.append({"type": "content_block_stop", "index": 0})
        if events:
            self.worker.send(*map(worker_stream_event, events), ephemeral=True)
        self.stream_text = ""
        self.stream_sent_at = time.monotonic()

    def _stream_stop(self) -> None:
        """End the message that was being written, for its watchers."""
        if self.stream_open:
            stop = {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}
            self.worker.send(worker_stream_event(stop), worker_stream_event({"type": "message_stop"}), ephemeral=True)
        self.stream, self.stream_open, self.stream_text = None, False, ""

    def _plan_updated(self, params: dict) -> None:
        call_id = f"plan-{params['turnId']}-{time.monotonic_ns()}"
        todo = {"type": "tool_use", "id": call_id, "name": "TodoWrite", "input": translate.todos(params["plan"])}
        self._send(worker_assistant([todo], model=self.model), worker_tool_result(call_id, "Plan updated."))

    def _usage_updated(self, params: dict) -> None:
        self.usage = params["tokenUsage"]

    def _name_updated(self, params: dict) -> None:
        if self.follow_name and params.get("threadName"):
            title = session_title(params["threadName"], self.settings["cwd"])
            self.worker.client.rename_session(self.worker.session_id, title)

    def _turn_completed(self, params: dict) -> None:
        turn = params["turn"]
        if self.turn is not None and turn.get("id", self.turn) != self.turn:
            return
        status = turn["status"]
        if self.review:  # a review that no message repeated
            self.last_text, self.review = self.review, None
            self._send(worker_assistant(self.last_text, model=self.model, stop_reason="end_turn"))
        if self.stream is not None:  # a message cut off as it was being written
            self._stream_flush(end=True)
            self._stream_stop()
        if status == "failed":
            self.last_text = (turn.get("error") or {}).get("message") or "The turn failed."
            self._send(worker_assistant(self.last_text, model=self.model))
        for request_id in list(self.prompts):  # a prompt can't outlive its turn
            self._withdraw_prompt(request_id)
        for item_id, calls in self.calls.items():
            # Nor can a tool call. Codex leaves an interrupted command running
            # and reports its end whenever that comes, long after the turn.
            self._send(*(worker_tool_result(call["id"], "Interrupted.", is_error=True) for call in calls))
            self.interrupted.add(item_id)
        ended = turn.get("id") or self.turn
        self.worker.end_turn(
            *self.inputs,
            result=self.last_text,
            is_error=status == "failed",
            duration_ms=turn.get("durationMs") or 0,
            usage=translate.result_usage(self.usage_before, self.usage),
            **({"terminal_reason": "aborted_streaming"} if status == "interrupted" else {}),
            **({TURN_FIELD: ended} if ended else {}),
        )
        self.last_turn = ended or self.last_turn
        if self.usage:
            self.worker.report_metadata(context_usage={
                "used_tokens": self.usage["last"]["totalTokens"],
                "max_tokens": self.usage.get("modelContextWindow"),
            })
        self.turn, self.inputs, self.calls = None, [], {}
        self.stream, self.stream_open, self.stream_text = None, False, ""
        self.asked_to_compact = self.own_prompt = False
        replies, self.replies = self.replies, []
        if self.queued:
            queued, self.queued = self.queued, []
            self._start_turn(queued)
        for text in replies:
            self._reply(text)

    def _thread_closed(self, params: dict) -> None:
        raise ThreadClosed(self.thread_id)

    def _warning(self, params: dict) -> None:
        self._note(params["message"])  # about this thread: its limits running low, a setting it can't honor

    def _model_rerouted(self, params: dict) -> None:
        self._note(f"Codex is running this turn on `{params['toModel']}` instead of `{params['fromModel']}`.")

    _NOTIFICATIONS: ClassVar[dict[str, Callable[[Bridge, dict], None]]] = {
        "turn/started": _turn_started,
        "turn/completed": _turn_completed,
        "turn/plan/updated": _plan_updated,
        "item/started": _item_started,
        "item/completed": _item_completed,
        "item/agentMessage/delta": _message_delta,
        "item/reasoning/summaryTextDelta": _thinking_delta,
        "item/reasoning/summaryPartAdded": _thinking_part,
        "thread/tokenUsage/updated": _usage_updated,
        "thread/settings/updated": _settings_updated,
        "thread/name/updated": _name_updated,
        "thread/closed": _thread_closed,
        "thread/archived": _thread_closed,
        "thread/deleted": _thread_closed,
        "mcpServer/startupStatus/updated": _mcp_updated,
        "model/rerouted": _model_rerouted,
        "warning": _warning,
        "serverRequest/resolved": lambda self, params: self._request_resolved(params["requestId"]),
    }

    # -- approvals: Codex asks, a controller answers ---------------------------
    def _server_request(self, request: dict) -> None:
        """One of the app-server's requests, put to the controllers as a
        permission prompt. The daemon sends it to every client of the thread,
        so a TUI may answer it first (:meth:`_request_resolved`)."""
        method, params = request["method"], request["params"]
        if method == "item/commandExecution/requestApproval":
            if translate.approval_key(params) in self.allowed:  # "Always allow", said of this command before
                self.codex.respond(request["id"], {"decision": "accept"})
                return
            command = translate.shell_script(params.get("command") or "")
            self._prompt(request, "Bash", {"command": command}, params.get("reason") or f"Run: {command}", command)
        elif method == "item/fileChange/requestApproval":
            calls = self.calls.get(params["itemId"]) or [{"name": "Edit", "input": {}}]
            paths = ", ".join(call["input"].get("file_path", "?") for call in calls)
            self._prompt(request, calls[0]["name"], calls[0]["input"], params.get("reason") or f"Change {paths}")
        elif method == "item/permissions/requestApproval":
            self._prompt(request, "Permissions", params["permissions"], params.get("reason") or "Grant more permissions")
        elif method == "item/tool/requestUserInput":
            self._prompt(request, "AskUserQuestion", translate.question_prompt(params["questions"]), "Codex has a question")
        elif method == "mcpServer/elicitation/request":
            if (prompt := translate.elicitation_prompt(params)) is not None:
                self._prompt(request, *prompt)
                return
            # A form the app's prompts can't carry. Left unanswered so a TUI
            # on the thread can take it.
            notice = f"Codex is waiting on a form from the MCP server {params['serverName']}. Answer it in the Codex TUI, or interrupt."
            self._send(worker_assistant(notice, model=self.model))

    def _ask(self, question: dict) -> None:
        """Put a question Codex asked as it works (`request_user_input_async`)
        to the controllers, as a card like a prompt's. It is no request of the
        app-server's: the answer goes back as a message, as from Codex's own
        terminal, and as there the question goes when its turn ends."""
        self._prompt({"id": None, "method": "question", "params": {**question, "itemId": question["id"]}},
                     "AskUserQuestion", question["input"], "Codex has a question")

    def _questions_answered(self, item: dict) -> None:
        """A message that answers Codex's questions, sent from anywhere: their
        cards come down. An older client names the message that asked."""
        answered = {
            reply["questionItemId"] for part in item["content"] if part.get("type") == "text"
            for reply in translate.question_replies(part["text"]) or []
        }
        for request_id, request in list(self.prompts.items()):
            if request["method"] == "question" and answered & {request["params"]["id"], request["params"]["message"]}:
                self._withdraw_prompt(request_id)

    def _reply(self, text: str) -> None:
        """Give Codex a question's answer, as its own terminal does: a message
        to the running turn. If that won't take it (it has just ended), the
        answer starts the next turn."""
        message = [{"type": "text", "text": text, "text_elements": []}]
        if self.turn is not None:
            try:
                self.codex.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn, "input": message})
            except AppServerError:
                self.replies.append(text)
            return
        turn = self.codex.request("turn/start", {"threadId": self.thread_id, "input": message})["turn"]
        self._turn_began(turn["id"])
        self.worker.report_state("running")

    def _prompt(self, request: dict, tool: str, tool_input: dict, description: str, command: str | None = None) -> None:
        """Put one of Codex's requests to the controllers. A command's card
        also offers "Always allow"."""
        suggestions = translate.session_rule(command) if command else None
        tool_use_id = request["params"].get("itemId") or f"request-{request['id']}"
        request_id = self.worker.request_permission(
            tool, tool_input, tool_use_id=tool_use_id, description=description, raw_command=command,
            permission_suggestions=suggestions,
        )
        self.prompts[request_id] = {**request, "prompt": (tool, tool_input, tool_use_id, description, suggestions)}

    def _prompt_payload(self, request_id: str) -> dict:
        tool, tool_input, tool_use_id, description, suggestions = self.prompts[request_id]["prompt"]
        return worker_permission_request(
            request_id, tool, tool_input, tool_use_id=tool_use_id, description=description,
            permission_suggestions=suggestions,
        )

    def _prompt_answered(self, answer: Event) -> None:
        self.worker.report_delivery(answer, "processed")
        request = self.prompts.pop(answer.request_id, None)
        if request is None:
            return  # settled already: answered at the TUI, or its turn ended
        decision: dict[str, Any] = (answer.payload.get("response") or {}).get("response") or {}
        log.debug("answered: %.600s", decision)
        allow = decision.get("behavior") == "allow"
        if request["method"] == "question":
            question = request["params"]
            given = ((decision.get("updatedInput") or {}).get("answers") or {}).get(question["question"]) if allow else None
            if isinstance(given, list):
                given = ", ".join(given)
            if given:  # dismissed, it stays Codex's to wait on
                self._reply(translate.question_reply(question["id"], question["question"], given))
            self._prompt_settled()
            return
        if request["prompt"][0] == "AskUserQuestion":
            answers = (decision.get("updatedInput") or {}).get("answers") or {}
            if request["method"] == "item/tool/requestUserInput":
                result = translate.question_answers(request["params"]["questions"], answers if allow else {})
            else:
                result = translate.elicitation_answer(request["params"], answers if allow else None)
        else:
            standing = allow and bool(decision.get("updatedPermissions"))  # "Always allow" sends its rule back
            result = translate.approval_answer(request, allow, standing=standing)
            if standing and result == {"decision": "accept"}:  # Codex won't remember it: the bridge does
                self.allowed.add(translate.approval_key(request["params"]))
        self.codex.respond(request["id"], result)
        self._prompt_settled()

    def _request_resolved(self, server_request_id: Any) -> None:
        """An approval was settled. If its prompt is still open here, it was
        answered somewhere else: take the prompt down."""
        for request_id, request in list(self.prompts.items()):
            if request["id"] == server_request_id:
                self._withdraw_prompt(request_id)

    def _withdraw_prompt(self, request_id: str) -> None:
        del self.prompts[request_id]
        self.worker.send(cli_control_cancel_request(request_id))
        self._prompt_settled()

    def _prompt_settled(self) -> None:
        if not self.prompts and self.turn is not None:
            self.worker.report_state("running")
