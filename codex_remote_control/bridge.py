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
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from claude_rc import (
    Event,
    RemoteControlWorker,
    WorkerEvicted,
    cli_control_cancel_request,
    worker_assistant,
    worker_init,
    worker_initialize_response,
    worker_permission_request,
    worker_result,
    worker_stream_event,
    worker_tool_result,
)

from . import translate
from .appserver import AppServer, AppServerClosed, AppServerError

log = logging.getLogger(__name__)

# How long a dropped app-server connection is retried: the daemon restarts
# itself to update, and is back within seconds.
_RECONNECT_TRIES = 8
# How often a reply that is still being written is passed on to its watchers.
_STREAM_FLUSH_S = 0.3


def session_title(thread_name: str | None, cwd: str) -> str:
    """What a thread's session is called in the app's list, where it sits
    among Claude Code sessions: Codex's name for the thread once it has one,
    its directory until then."""
    return f"Codex: {thread_name or os.path.basename(cwd) or cwd}"


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


class ThreadClosed(Exception):
    """The Codex thread was closed or archived: there is nothing left to serve."""


class Bridge:
    def __init__(self, codex: AppServer, worker: RemoteControlWorker, opened: dict, *, follow_name: bool):
        """``opened`` is the result of the `thread/start` or `thread/resume`
        that subscribed ``codex`` to the thread. With ``follow_name`` the
        session is retitled as Codex names the thread."""
        self.codex = codex
        self.worker = worker
        self.thread_id: str = opened["thread"]["id"]
        self.follow_name = follow_name
        self.settings = _settings(opened)
        self._models: dict[str, list[str]] | None = None  # see models()
        self.turn: str | None = None  # the running Codex turn
        self.inputs: list[Event] = []  # the controller messages that turn answers
        self.queued: list[Event] = []  # controller messages waiting for it to end
        self.sent: set[str] = set()  # ids of the user messages this bridge submitted
        self.calls: dict[str, list[dict]] = {}  # running tool items' tool_use blocks, by item id
        self.interrupted: set[str] = set()  # tool items closed with their turn, whose real end is still to come
        self.prompts: dict[str, dict] = {}  # open permission prompts: request id -> the app-server request
        self.usage: dict | None = None  # the thread's token usage, as last reported
        self.usage_before: dict | None = None  # ... and as the running turn began
        self.last_text = ""  # the turn's latest agent message: its result
        self.watched = True  # whether a controller has the session open, by the last upload's ack
        self.stream: str | None = None  # the agent message being written, by item id
        self.stream_open = False  # ... whether the watchers have its start
        self.stream_text = ""  # ... and its text not yet passed on to them
        self.stream_sent_at = 0.0
        self._inbox: queue.Queue = queue.Queue()

    # -- the loop ------------------------------------------------------------
    def run(self) -> None:
        """Serve until the worker seat is lost (:class:`claude_rc.WorkerEvicted`),
        the thread closes (:class:`ThreadClosed`), or the app-server stays gone
        (:class:`AppServerClosed`)."""
        self._announce()
        self._adopt_running_turn()
        self._pump("rc", self.worker.events())
        while True:
            self._pump("codex", self.codex.messages())
            self._serve()  # returns when the app-server connection drops
            self._reconnect()

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
                (self._from_controller if source == "rc" else self._from_codex)(item)
            except AppServerClosed:
                return
            except (WorkerEvicted, ThreadClosed):
                raise
            except Exception:
                # One event that can't be passed on (a refused upload, a shape
                # this version doesn't know) is no reason to drop the session.
                log.exception("could not handle %s", item if source == "rc" else item.get("method"))

    def _adopt_running_turn(self) -> None:
        """A thread taken up mid-turn (one a TUI set going): find that turn, so
        the app shows the session working and can steer or stop it."""
        latest = self.codex.request("thread/turns/list", {
            "threadId": self.thread_id, "limit": 1, "sortDirection": "desc",
        })["data"]
        if latest and latest[0]["status"] == "inProgress":
            self._turn_began(latest[0]["id"])
            self.worker.report_state("running")

    def backfill(self, turns: int = 30) -> None:
        """Write the thread's latest turns into the session, for one made
        just now: a thread taken up mid-conversation then reads from its
        start (or near it) in the app, not from this moment."""
        listed = self.codex.request("thread/turns/list", {
            "threadId": self.thread_id, "limit": turns, "sortDirection": "desc", "itemsView": "full",
        })["data"]
        for turn in reversed(listed):
            events = [event for item in turn["items"] for event in translate.transcript(item, self.model)]
            if events and turn["status"] != "inProgress":
                self.worker.send(*events, worker_result(duration_ms=turn.get("durationMs") or 0))

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
        """Write transcript events, and note whether anyone is watching."""
        self.watched = self.worker.send(*payloads).get("has_subscribers", True)

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
            slash_commands=sorted(self._COMMANDS), **({"effort": effort} if effort else {}),
        ))
        self.worker.report_metadata(model=self.model, effort_level=effort, permission_mode=self.mode)

    def models(self) -> dict[str, list[str]]:
        """Codex's models, each with the reasoning efforts it takes."""
        if self._models is None:
            self._models = {
                m["id"]: [e["reasoningEffort"] for e in m["supportedReasoningEfforts"]]
                for m in self.codex.request("model/list", {})["data"]
            }
        return self._models

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
        log.debug("claude.ai: %r %s", event, event.control_subtype or "")
        if event.type == "user":
            self._user_message(event)
        elif event.type == "control_request":
            self._control_request(event)
        elif event.type == "control_response":
            self._prompt_answered(event)
        else:  # a control_cancel_request for something already answered
            self.worker.report_delivery(event, "processed")

    def _user_message(self, message: Event) -> None:
        name, _, argument = message.text().strip().partition(" ")
        if name.startswith("/") and (command := self._COMMANDS.get(name[1:])) is not None:
            try:
                answer = command(self, argument.strip())
            except AppServerError as e:
                answer = f"Codex refused: {e}"
            self._answer_here(message, answer)
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
                "input": translate.user_input(message), "clientUserMessageId": message.id,
            })
        except AppServerError:
            self.queued.append(message)
            return
        self.worker.report_delivery(message, "processing")
        self.inputs.append(message)

    def _start_turn(self, messages: list[Event]) -> None:
        self.sent.update(m.id for m in messages)
        self.inputs = list(messages)
        self.worker.begin_turn(*messages)
        try:
            turn = self.codex.request("turn/start", {
                "threadId": self.thread_id,
                "input": [part for m in messages for part in translate.user_input(m)],
                "clientUserMessageId": messages[0].id,
            })["turn"]
        except AppServerError as e:
            self._turn_completed({"turn": {"status": "failed", "error": {"message": str(e)}}})
            return
        self._turn_began(turn["id"])

    def _answer_here(self, message: Event, answer: str) -> None:
        """Answer a message without a Codex turn, as Claude Code answers a
        slash command."""
        self.worker.report_delivery(message, "processing")
        self._send(worker_assistant(answer, model=self.model, stop_reason="end_turn"))
        if self.turn is None:
            self.worker.end_turn(message, result=answer, num_turns=0)
        else:  # mid-turn: the running turn's result is still to come
            self.worker.report_delivery(message, "processed")

    def _model_command(self, model: str) -> str:
        if not model:
            return f"Model: `{self.model}`. Codex has: {', '.join(f'`{m}`' for m in self.models())}."
        if model not in self.models():
            return f"Codex has no model `{model}`. It has: {', '.join(f'`{m}`' for m in self.models())}."
        self._update_settings(model=model)
        return f"Model set to `{model}`, from the next turn."

    def _effort_command(self, effort: str) -> str:
        efforts = self.models().get(self.model, [])
        if not effort:
            return f"Reasoning effort: `{self.settings.get('effort')}`. `{self.model}` takes: {', '.join(efforts)}."
        if effort not in efforts:
            return f"`{self.model}` has no effort `{effort}`. It takes: {', '.join(efforts)}."
        self._update_settings(effort=effort)
        return f"Reasoning effort set to `{effort}`, from the next turn."

    def _compact_command(self, _: str) -> str:
        self.codex.request("thread/compact/start", {"threadId": self.thread_id})
        return "Compacting the thread's context."

    # Typed in the app's message box, these are answered here rather than
    # sent to the model. The app's own model menu lists only Claude models.
    _COMMANDS: ClassVar[dict[str, Callable[[Bridge, str], str]]] = {
        "model": _model_command,
        "effort": _effort_command,
        "compact": _compact_command,
    }

    def _control_request(self, request: Event) -> None:
        try:
            self._control(request)
        except AppServerError as e:  # every request gets an answer: its sender waits for one
            self.worker.reject(request, f"Codex refused: {e}")

    def _control(self, request: Event) -> None:
        subtype = request.control_subtype
        fields = request.payload["request"]
        if subtype == "initialize":  # every controller sends it as it attaches
            self.worker.respond(
                request,
                worker_initialize_response(model=self.model, permission_mode=self.mode),
                pending_permission_requests=[self._prompt_payload(rid) for rid in self.prompts],
                pending_user_dialog_requests=[],
            )
            self._announce()
        elif subtype == "interrupt":
            if self.turn is not None:
                self.codex.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn})
            self.worker.respond(request)
        elif subtype == "set_model":
            if fields.get("model") not in self.models():
                self.worker.reject(request, f"Codex has no model {fields.get('model')!r}. It has: {', '.join(self.models())}")
                return
            self._update_settings(model=fields["model"])
            self.worker.respond(request, {"model": fields["model"]})
        elif subtype == "set_permission_mode":
            if fields.get("mode") not in translate.MODES:
                self.worker.reject(request, f"no such permission mode: {fields.get('mode')}", error_code="invalid_mode")
                return
            self._update_settings(**translate.mode_settings(fields["mode"], self.settings))
            self.worker.respond(request)
        elif subtype == "apply_flag_settings":
            if level := (fields.get("settings") or {}).get("effortLevel"):
                self._update_settings(effort=level)
            self.worker.respond(request)
        elif subtype == "get_context_usage":
            self.worker.respond(request, translate.context_usage(self.usage, self.model))
        elif subtype == "get_workspace_diff":
            self.worker.respond(request, {"diff": None})
        elif subtype == "rename_session":
            self.codex.request("thread/name/set", {"threadId": self.thread_id, "name": fields["title"]})
            self.worker.respond(request)
        else:
            self.worker.reject(request, f"codex-remote-control does not handle control_request subtype: {subtype}")

    # -- from Codex ------------------------------------------------------------
    def _from_codex(self, message: dict) -> None:
        params = message.get("params") or {}
        if params.get("threadId", self.thread_id) != self.thread_id:
            return  # the daemon is shared: another thread's traffic
        if not message["method"].endswith(("elta", "/updated")):
            log.debug("codex: %s %s", message["method"], (params.get("item") or {}).get("type", ""))
        if "id" in message:
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
        if item["type"] == "webSearch":  # its query is known only once it is done
            return
        if calls := translate.tool_calls(item):
            self.calls[item["id"]] = calls
            self._send(worker_assistant(calls, model=self.model))

    def _item_completed(self, params: dict) -> None:
        item = params["item"]
        kind = item["type"]
        if kind == "userMessage" and item.get("clientId") in self.sent:
            return  # sent from the app, where it shows already. One typed at a TUI goes on.
        if item["id"] in self.interrupted:
            self.interrupted.discard(item["id"])  # closed with its turn already
            return
        events = translate.transcript(item, self.model)
        if self.calls.pop(item["id"], None):
            events = events[1:]  # the call itself went out as it started
        if kind == "agentMessage":
            self.last_text = item["text"] or self.last_text
            self._stream_flush(end=True)
        if events:
            self._send(*events)
        if kind == "agentMessage" and self.stream_open:
            stop = {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}
            self.worker.send(worker_stream_event(stop), worker_stream_event({"type": "message_stop"}), ephemeral=True)
            self.stream, self.stream_open = None, False

    def _message_delta(self, params: dict) -> None:
        """A piece of an agent message still being written. Pieces are passed
        on a few times a second, and only while someone is watching: the
        finished message is what the transcript keeps."""
        if not self.watched:
            return
        self.stream = params["itemId"]
        self.stream_text += params["delta"]
        if time.monotonic() - self.stream_sent_at >= _STREAM_FLUSH_S:
            self._stream_flush()

    def _stream_flush(self, *, end: bool = False) -> None:
        """Pass on the text gathered since the last flush, as the Messages
        API's streaming events. ``end`` closes the text block."""
        events = []
        if self.stream_text:
            if not self.stream_open:
                self.stream_open = True
                message = {
                    "id": self.stream, "type": "message", "role": "assistant", "model": self.model, "content": [],
                    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
                }
                events.append({"type": "message_start", "message": message})
                events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            delta = {"type": "text_delta", "text": self.stream_text}
            events.append({"type": "content_block_delta", "index": 0, "delta": delta})
        if end and self.stream_open:
            events.append({"type": "content_block_stop", "index": 0})
        if events:
            self.worker.send(*map(worker_stream_event, events), ephemeral=True)
        self.stream_text = ""
        self.stream_sent_at = time.monotonic()

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
        self.worker.end_turn(
            *self.inputs,
            result=self.last_text,
            is_error=status == "failed",
            duration_ms=turn.get("durationMs") or 0,
            usage=translate.result_usage(self.usage_before, self.usage),
            **({"terminal_reason": "aborted_streaming"} if status == "interrupted" else {}),
        )
        if self.usage:
            self.worker.report_metadata(context_usage={
                "used_tokens": self.usage["last"]["totalTokens"],
                "max_tokens": self.usage.get("modelContextWindow"),
            })
        self.turn, self.inputs, self.calls = None, [], {}
        self.stream, self.stream_open, self.stream_text = None, False, ""
        if self.queued:
            queued, self.queued = self.queued, []
            self._start_turn(queued)

    def _thread_closed(self, params: dict) -> None:
        raise ThreadClosed(self.thread_id)

    _NOTIFICATIONS: ClassVar[dict[str, Callable[[Bridge, dict], None]]] = {
        "turn/started": _turn_started,
        "turn/completed": _turn_completed,
        "turn/plan/updated": _plan_updated,
        "item/started": _item_started,
        "item/completed": _item_completed,
        "item/agentMessage/delta": _message_delta,
        "thread/tokenUsage/updated": _usage_updated,
        "thread/settings/updated": _settings_updated,
        "thread/name/updated": _name_updated,
        "thread/closed": _thread_closed,
        "thread/archived": _thread_closed,
        "serverRequest/resolved": lambda self, params: self._request_resolved(params["requestId"]),
    }

    # -- approvals: Codex asks, a controller answers ---------------------------
    def _server_request(self, request: dict) -> None:
        """One of the app-server's requests, put to the controllers as a
        permission prompt. The daemon sends it to every client of the thread,
        so a TUI may answer it first (:meth:`_request_resolved`)."""
        method, params = request["method"], request["params"]
        if method == "item/commandExecution/requestApproval":
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
            # Not something the app's prompts can carry. Left unanswered so a
            # TUI on the thread can take it.
            notice = f"Codex is waiting on a form from the MCP server {params['serverName']}. Answer it in the Codex TUI, or interrupt."
            self._send(worker_assistant(notice, model=self.model))

    def _prompt(self, request: dict, tool: str, tool_input: dict, description: str, command: str | None = None) -> None:
        request_id = self.worker.request_permission(
            tool, tool_input, tool_use_id=request["params"]["itemId"], description=description, raw_command=command
        )
        self.prompts[request_id] = {**request, "prompt": (tool, tool_input, description)}

    def _prompt_payload(self, request_id: str) -> dict:
        request = self.prompts[request_id]
        tool, tool_input, description = request["prompt"]
        return worker_permission_request(
            request_id, tool, tool_input, tool_use_id=request["params"]["itemId"], description=description
        )

    def _prompt_answered(self, answer: Event) -> None:
        self.worker.report_delivery(answer, "processed")
        request = self.prompts.pop(answer.request_id, None)
        if request is None:
            return  # settled already: answered at the TUI, or its turn ended
        decision: dict[str, Any] = (answer.payload.get("response") or {}).get("response") or {}
        allow = decision.get("behavior") == "allow"
        if request["method"] == "item/tool/requestUserInput":
            answers = (decision.get("updatedInput") or {}).get("answers") or {}
            result = translate.question_answers(request["params"]["questions"], answers if allow else {})
        else:
            result = translate.approval_answer(request, allow)
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
