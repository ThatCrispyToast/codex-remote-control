"""The bridge between a recorded Codex app-server and a recorded worker seat."""
import threading
import time

import pytest
from claude_rc import APIError, Event

from codex_remote_control import bridge as bridge_module
from codex_remote_control import workspace
from codex_remote_control.appserver import AppServerClosed, AppServerError
from codex_remote_control.bridge import Bridge, Exited, ThreadClosed, session_title

THREAD = "thr-1"
OPENED = {
    "thread": {"id": THREAD, "cwd": "/w", "name": None, "cliVersion": "0.170.0"},
    "model": "gpt-x", "reasoningEffort": "low", "cwd": "/w",
    "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"},
    "collaborationMode": {"mode": "default"},
}
MODELS = {"data": [
    {"id": "gpt-x", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
     "serviceTiers": [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}]},
    {"id": "gpt-y", "supportedReasoningEfforts": [{"reasoningEffort": "low"}], "serviceTiers": []},
]}


class Codex:
    """Records the bridge's requests and answers; `refuse` names methods to fail."""

    remote = None  # the shared daemon

    def __init__(self):
        self.requests, self.answers, self.refuse = [], [], set()
        self.results = {
            "turn/start": {"turn": {"id": "turn-1"}}, "model/list": MODELS, "thread/turns/list": {"data": []},
            "mcpServerStatus/list": {"data": []}, "review/start": {"turn": {"id": "turn-1"}},
        }

    def request(self, method, params=None, **_):
        self.requests.append((method, params))
        if method in self.refuse:
            raise AppServerError(method, {"message": "no"})
        return self.results.get(method, {})

    def respond(self, request_id, result):
        self.answers.append((request_id, result))

    def sent(self, method):
        return [params for m, params in self.requests if m == method]


class Worker:
    """Records what the bridge writes to the session, as (what, ...) tuples."""

    session_id = "cse_1"

    def __init__(self):
        self.log, self.watched, self.client = [], True, self
        self.prompts = 0
        self.held = []  # the session's own events, newest first, as `list_events` answers
        self.files = {}  # what `download_file` has, by file uuid
        self.uploads = []  # how many events each upload carried

    def send(self, *payloads, ephemeral=False):
        if len(payloads) > 100:
            raise APIError(400, "events: must contain no more than 100 item(s)", None)  # as claude.ai answers
        self.uploads.append(len(payloads))
        self.log += [("ephemeral" if ephemeral else "event", p) for p in payloads]
        return {"has_subscribers": self.watched}

    def request_permission(self, tool, tool_input, **kw):
        self.prompts += 1
        self.log.append(("prompt", tool, tool_input, kw))
        return f"prompt-{self.prompts}"

    def rename_session(self, session_id, title):
        self.log.append(("title", title))

    def list_events(self, session_id, **_):
        return self.held

    def download_file(self, file_uuid, **_):
        if file_uuid not in self.files:
            raise APIError(404, "Missing files", None)
        return self.files[file_uuid]

    def __getattr__(self, name):  # begin_turn, end_turn, respond, reject, report_*
        return lambda *args, **kw: self.log.append((name, *args, kw))

    def events(self, kind="event"):
        return [entry[1] for entry in self.log if entry[0] == kind]

    def calls(self, name):
        return [entry[1:] for entry in self.log if entry[0] == name]


@pytest.fixture
def bridge():
    return Bridge(Codex(), Worker(), OPENED)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))  # where attached files are kept


def user(text, uuid="u-1", seq=1, **fields):
    return Event.from_wire({"sequence_num": seq, "event_id": f"ev-{seq}", "source": "client", "payload": {
        "type": "user", "uuid": uuid, "message": {"role": "user", "content": [{"type": "text", "text": text}]}, **fields}})


def settle(bridge):
    """Run what the bridge worked out off its loop, as the loop does next."""
    source, task = bridge._inbox.get(timeout=5)
    assert source == "task"
    task()


def control(subtype, seq=1, **fields):
    return Event.from_wire({"sequence_num": seq, "event_id": f"ev-{seq}", "source": "client", "payload": {
        "type": "control_request", "request_id": f"req-{seq}", "request": {"subtype": subtype, **fields}}})


def answer(request_id, seq=9, **decision):
    return Event.from_wire({"sequence_num": seq, "event_id": f"ev-{seq}", "source": "client", "payload": {
        "type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": decision}}})


def note(method, **params):
    return {"method": method, "params": {"threadId": THREAD, **params}}


def item(event, **fields):
    return note(f"item/{event}", item=fields, turnId="turn-1")


def turn_done(status="completed", **turn):
    return note("turn/completed", turn={"id": "turn-1", "status": status, "error": None, "durationMs": 1200, **turn})


def later(bridge, seconds):
    """Do what the bridge does unprompted once this long has passed with nothing to handle."""
    for when, act in bridge._clock():
        if when <= time.monotonic() + seconds:
            act()


def live(bridge):
    """What the watchers were shown and the transcript does not keep."""
    return [event["event"] for event in bridge.worker.events("ephemeral")]


COMMAND = {"type": "commandExecution", "id": "exec-1", "command": "bash -lc 'ls'", "status": "inProgress"}


# -- a turn from the app ----------------------------------------------------------
def test_a_message_from_the_app_runs_a_codex_turn(bridge):
    message = user("list the files")
    bridge._from_controller(message)
    assert bridge.codex.sent("turn/start") == [{
        "threadId": THREAD, "clientUserMessageId": "u-1",
        "input": [{"type": "text", "text": "list the files", "text_elements": []}]}]
    assert bridge.worker.calls("begin_turn") == [(message, {})]

    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    bridge._from_codex(item("completed", type="userMessage", id="i-0", clientId="u-1", content=[]))
    bridge._from_codex(item("started", **COMMAND))
    bridge._from_codex(item("completed", **{**COMMAND, "status": "completed", "aggregatedOutput": "a\n", "exitCode": 0}))
    bridge._from_codex(note("thread/tokenUsage/updated", tokenUsage={
        "total": {"inputTokens": 90, "cachedInputTokens": 40, "cacheWriteInputTokens": 0, "outputTokens": 7},
        "last": {"totalTokens": 97}, "modelContextWindow": 1000}))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="One file: a"))
    bridge._from_codex(turn_done())

    call, result, said = bridge.worker.events()
    assert call["message"]["content"] == [{"type": "tool_use", "id": "exec-1", "name": "Bash", "input": {"command": "ls"}}]
    assert result["message"]["content"][0]["content"] == "a\n"
    assert said["message"]["content"] == [{"type": "text", "text": "One file: a"}]
    (ended,) = bridge.worker.calls("end_turn")
    assert ended[0] is message
    assert ended[1] == {"result": "One file: a", "is_error": False, "duration_ms": 1200, "codex_turn": "turn-1", "usage": {
        "input_tokens": 50, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0, "output_tokens": 7}}
    assert bridge.worker.calls("report_metadata")[-1] == ({"context_usage": {"used_tokens": 97, "max_tokens": 1000}},)
    assert bridge.turn is None


def test_a_turn_codex_refuses_ends_as_an_error(bridge):
    bridge.codex.refuse.add("turn/start")
    bridge._from_controller(user("hi"))
    assert bridge.worker.events()[0]["message"]["content"][0]["text"] == "turn/start: no"
    (ended,) = bridge.worker.calls("end_turn")
    assert ended[1]["is_error"] is True and bridge.turn is None


def test_a_message_sent_mid_turn_joins_the_turn(bridge):
    first, second = user("one"), user("two", uuid="u-2", seq=2)
    bridge._from_controller(first)
    bridge._from_controller(second)
    assert bridge.codex.sent("turn/steer") == [{
        "threadId": THREAD, "expectedTurnId": "turn-1", "clientUserMessageId": "u-2",
        "input": [{"type": "text", "text": "two", "text_elements": []}]}]
    assert bridge.worker.calls("report_delivery") == [(second, "processing", {})]
    bridge._from_codex(turn_done())
    assert bridge.worker.calls("end_turn")[0][:2] == (first, second)


def test_a_message_the_turn_will_not_take_gets_the_next_turn(bridge):
    first, second = user("one"), user("two", uuid="u-2", seq=2)
    bridge._from_controller(first)
    bridge.codex.refuse.add("turn/steer")
    bridge._from_controller(second)
    assert bridge.worker.calls("report_delivery") == []  # still waiting
    bridge._from_codex(turn_done())
    assert [params["clientUserMessageId"] for params in bridge.codex.sent("turn/start")] == ["u-1", "u-2"]
    assert bridge.worker.calls("begin_turn")[-1] == (second, {})


# -- a turn from the TUI ------------------------------------------------------------
def test_a_turn_started_at_the_tui_shows_in_the_app(bridge):
    bridge._from_codex(note("turn/started", turn={"id": "turn-9"}))
    assert bridge.worker.calls("report_state") == [("running", {})]
    typed = [{"type": "text", "text": "from the terminal"}]
    bridge._from_codex(item("completed", type="userMessage", id="i-1", clientId=None, content=typed))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="ok"))
    bridge._from_codex(note("turn/completed", turn={"id": "turn-9", "status": "completed"}))
    shown = bridge.worker.events()
    assert shown[0]["message"] == {"role": "user", "content": "from the terminal"}
    assert shown[1]["message"]["content"][0]["text"] == "ok"
    assert bridge.worker.calls("end_turn")[0][-1]["result"] == "ok"  # no app messages to mark done


def test_a_thread_taken_up_mid_turn_can_be_stopped(bridge):
    bridge.codex.results["thread/turns/list"] = {"data": [{"id": "turn-7", "status": "inProgress", "items": []}]}
    bridge._adopt_running_turn()
    assert bridge.worker.calls("report_state") == [("running", {})]
    bridge._from_controller(control("interrupt"))
    assert bridge.codex.sent("turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-7"}]
    bridge.codex.results["thread/turns/list"] = {"data": [{"id": "turn-8", "status": "completed", "items": []}]}
    idle = Bridge(Codex(), Worker(), OPENED)
    idle._adopt_running_turn()
    assert idle.turn is None and idle.worker.log == []


def test_another_threads_traffic_is_ignored(bridge):
    bridge._from_codex({"method": "turn/started", "params": {"threadId": "thr-other", "turn": {"id": "t"}}})
    bridge._from_codex({"method": "item/commandExecution/requestApproval", "id": 4,
                        "params": {"threadId": "thr-other", "itemId": "x", "command": "rm -rf /"}})
    assert bridge.worker.log == [] and bridge.turn is None


# -- interrupting --------------------------------------------------------------------
def test_an_interrupt_stops_the_turn_and_closes_its_open_tool_calls(bridge):
    bridge._from_controller(user("wait"))
    bridge._from_codex(item("started", **COMMAND))
    request = control("interrupt", seq=2)
    bridge._from_controller(request)
    assert bridge.codex.sent("turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-1"}]
    assert bridge.worker.calls("respond") == [(request, {})]

    bridge._from_codex(turn_done("interrupted"))
    closed = bridge.worker.events()[-1]["message"]["content"][0]
    assert closed == {"type": "tool_result", "tool_use_id": "exec-1", "content": "Interrupted.", "is_error": True}
    assert bridge.worker.calls("end_turn")[0][-1]["terminal_reason"] == "aborted_streaming"

    # Codex leaves the command running and reports its end later: said already.
    written = len(bridge.worker.log)
    bridge._from_codex(item("completed", **{**COMMAND, "status": "completed", "aggregatedOutput": "", "exitCode": 0}))
    assert len(bridge.worker.log) == written


# -- approvals -------------------------------------------------------------------------
def approval(request_id=7, **params):
    return {"method": "item/commandExecution/requestApproval", "id": request_id, "params": {
        "threadId": THREAD, "turnId": "turn-1", "itemId": "exec-1", "command": "bash -lc 'rm x'",
        "availableDecisions": ["accept", "cancel"], **params}}


def test_an_approval_is_put_to_the_app_and_its_answer_returned(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval())
    (prompt,) = bridge.worker.calls("prompt")
    assert prompt[:2] == ("Bash", {"command": "rm x"})
    assert (prompt[2]["tool_use_id"], prompt[2]["description"], prompt[2]["raw_command"]) == ("exec-1", "Run: rm x", "rm x")

    bridge._from_controller(answer("prompt-1", behavior="allow"))
    assert bridge.codex.answers == [(7, {"decision": "accept"})]
    assert bridge.worker.calls("report_state")[-1] == ("running", {})
    bridge._from_controller(answer("prompt-1", behavior="allow"))  # the app's echo of the same answer
    assert len(bridge.codex.answers) == 1


def test_a_refusal_declines_where_codex_offers_it(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval())
    bridge._from_controller(answer("prompt-1", behavior="deny", message="no"))
    assert bridge.codex.answers == [(7, {"decision": "cancel"})]


def test_a_patch_approval_shows_the_change(bridge):
    bridge._from_controller(user("write it"))
    change = {"path": "/w/a.txt", "kind": {"type": "add"}, "diff": "hi\n"}
    bridge._from_codex(item("started", type="fileChange", id="p-1", status="inProgress", changes=[change]))
    bridge._from_codex({"method": "item/fileChange/requestApproval", "id": 8,
                        "params": {"threadId": THREAD, "turnId": "turn-1", "itemId": "p-1"}})
    (prompt,) = bridge.worker.calls("prompt")
    assert prompt[:2] == ("Write", {"file_path": "/w/a.txt", "content": "hi\n"})
    assert prompt[2]["description"] == "Change /w/a.txt"


def test_an_approval_answered_at_the_tui_takes_the_prompt_down(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval())
    bridge._from_codex(note("serverRequest/resolved", requestId=7))
    assert bridge.worker.events()[-1] == {"type": "control_cancel_request", "request_id": "prompt-1"}
    assert bridge.prompts == {}
    bridge._from_controller(answer("prompt-1", behavior="allow"))  # too late
    assert bridge.codex.answers == []


def test_an_open_prompt_is_listed_to_a_controller_attaching(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval())
    bridge._from_controller(control("initialize", seq=5))
    (answered,) = bridge.worker.calls("respond")
    (pending,) = answered[-1]["pending_permission_requests"]
    assert pending["request_id"] == "prompt-1" and pending["request"]["input"] == {"command": "rm x"}
    assert answered[1]["current_model"] == "gpt-x" and answered[1]["current_permission_mode"] == "default"


def test_a_question_is_asked_and_answered_by_id(bridge):
    bridge._from_controller(user("pick"))
    questions = [{"id": "q1", "header": "Stack", "question": "Which stack?", "options": [{"label": "Go", "description": ""}]}]
    bridge._from_codex({"method": "item/tool/requestUserInput", "id": 3,
                        "params": {"threadId": THREAD, "turnId": "turn-1", "itemId": "ask-1", "questions": questions}})
    assert bridge.worker.calls("prompt")[0][0] == "AskUserQuestion"
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedInput={"answers": {"Which stack?": "Go"}}))
    assert bridge.codex.answers == [(3, {"answers": {"q1": {"answers": ["Go"]}}})]


ASKED = {"type": "agentMessage", "id": "call_ask1", "text": "Hands clear?\n- Yes", "phase": "final_answer",
         "delivery": "async", "questions": [{"title": "Hands clear?", "options": ["Yes"]}]}
REPLY = (
    '<send_user_message_question_reply>\n'
    '[{"answer":"Yes","question":"Hands clear?","questionItemId":"[\\"request_user_input_async\\",\\"call_ask1\\",0]"}]\n'
    '</send_user_message_question_reply>'
)


def replied(text=REPLY, **fields):
    """Codex's account of a message that answers its question."""
    return item("completed", type="userMessage", id="u-9", clientId=None, content=[{"type": "text", "text": text}], **fields)


def test_a_question_codex_asks_as_it_works_is_answered_in_the_app_by_a_message(bridge):
    bridge._from_controller(user("go"))
    bridge._from_codex(item("completed", **ASKED))
    tool, asked, _ = bridge.worker.calls("prompt")[0]
    assert (tool, asked["questions"][0]["question"]) == ("AskUserQuestion", "Hands clear?")
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedInput={"answers": {"Hands clear?": "Yes"}}))
    assert bridge.codex.sent("turn/steer")[-1] == {
        "threadId": THREAD, "expectedTurnId": "turn-1", "input": [{"type": "text", "text": REPLY, "text_elements": []}]}
    assert bridge.codex.answers == []  # nothing of the app-server's own was waiting
    bridge._from_codex(replied())  # Codex's account of it, which the transcript shows plainly
    assert bridge.worker.events()[-1]["message"]["content"] == "> Hands clear?\n\nYes"


def test_a_question_answered_at_a_tui_or_dismissed_is_left_to_codex(bridge):
    bridge._from_controller(user("go"))
    bridge._from_codex(item("completed", **ASKED))
    bridge._from_codex(replied())  # answered at the TUI: the card comes down
    assert bridge.worker.events()[-2]["type"] == "control_cancel_request" and bridge.prompts == {}
    bridge._from_codex(item("completed", **{**ASKED, "id": "call_ask2"}))
    bridge._from_controller(answer("prompt-2", seq=10, behavior="allow", updatedInput={"answers": {}}))
    assert bridge.codex.sent("turn/steer") == [] and bridge.prompts == {}


def test_an_answer_the_turn_ends_before_starts_the_next_turn(bridge):
    bridge._from_controller(user("go"))
    bridge._from_codex(item("completed", **ASKED))
    bridge.codex.refuse.add("turn/steer")
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedInput={"answers": {"Hands clear?": "Yes"}}))
    assert len(bridge.codex.sent("turn/start")) == 1
    bridge._from_codex(turn_done())
    assert bridge.codex.sent("turn/start")[-1]["input"] == [{"type": "text", "text": REPLY, "text_elements": []}]
    assert bridge.turn == "turn-1"


def test_a_question_in_the_threads_history_is_shown_and_not_asked_again(bridge):
    told = {"type": "userMessage", "id": "u-9", "clientId": None, "content": [{"type": "text", "text": REPLY}]}
    bridge.codex.results["thread/turns/list"] = {"data": [{**turn(1), "items": [*turn(1)["items"], ASKED, told]}]}
    bridge.backfill(new=True)
    assert bridge.worker.calls("prompt") == []
    assert [e["message"]["content"] for e in bridge.worker.events() if e["type"] == "user"] == ["q1", "> Hands clear?\n\nYes"]


def test_always_allow_lasts_for_the_session_and_covers_that_command(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval())
    rule = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "rm x"}], "behavior": "allow", "destination": "session"}]
    assert bridge.worker.calls("prompt")[0][2]["permission_suggestions"] == rule
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedPermissions=rule))
    assert bridge.codex.answers == [(7, {"decision": "accept"})]

    bridge._from_codex(approval(8))  # the same command again: not asked
    assert bridge.codex.answers[-1] == (8, {"decision": "accept"}) and len(bridge.worker.calls("prompt")) == 1
    bridge._from_codex(approval(9, command="bash -lc 'rm y'"))  # another is
    assert len(bridge.worker.calls("prompt")) == 2
    bridge._from_controller(answer("prompt-2", seq=10, behavior="allow"))  # "Allow once"
    bridge._from_codex(approval(10, command="bash -lc 'rm y'"))
    assert len(bridge.worker.calls("prompt")) == 3


def test_always_allow_is_codexs_to_remember_where_it_offers_to(bridge):
    bridge._from_controller(user("clean up"))
    bridge._from_codex(approval(availableDecisions=["accept", "acceptForSession", "decline"]))
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedPermissions=[{"type": "addRules"}]))
    assert bridge.codex.answers == [(7, {"decision": "acceptForSession"})] and bridge.allowed == set()


def elicitation(request_id=5, **params):
    return {"method": "mcpServer/elicitation/request", "id": request_id, "params": {
        "threadId": THREAD, "turnId": "turn-1", "serverName": "tracker", "_meta": None, **params}}


def test_an_mcp_tool_asking_to_run_is_a_permission_card(bridge):
    bridge._from_controller(user("file it"))
    ask = elicitation(mode="form", message="Allow tracker to create an issue?", requestedSchema={"type": "object", "properties": {}},
                      _meta={"persist": ["session", "always"], "tool_params": {"title": "Bug"}})
    bridge._from_codex(ask)
    tool, shown, options = bridge.worker.calls("prompt")[0]
    assert (tool, shown, options["description"]) == ("mcp__tracker", {"title": "Bug"}, "Allow tracker to create an issue?")
    assert options["tool_use_id"] == "request-5" and options["permission_suggestions"] is None
    bridge._from_controller(answer("prompt-1", behavior="allow"))
    bridge._from_codex({**ask, "id": 6})
    bridge._from_controller(answer("prompt-2", seq=10, behavior="deny"))
    assert bridge.codex.answers == [
        (5, {"action": "accept", "content": {}, "_meta": None}),
        (6, {"action": "decline", "content": None, "_meta": None})]


def test_an_mcp_form_of_choices_is_asked_as_questions(bridge):
    bridge._from_controller(user("file it"))
    schema = {"type": "object", "required": ["kind"], "properties": {
        "kind": {"type": "string", "title": "Kind", "enum": ["bug", "task"]},
        "urgent": {"type": "boolean", "description": "Is it urgent?"}}}
    bridge._from_codex(elicitation(mode="form", message="About the issue", requestedSchema=schema))
    tool, asked, _ = bridge.worker.calls("prompt")[0]
    assert tool == "AskUserQuestion"
    assert [(q["question"], [o["label"] for o in q["options"]]) for q in asked["questions"]] == [
        ("Kind", ["bug", "task"]), ("Is it urgent?", ["Yes", "No"])]
    bridge._from_controller(answer("prompt-1", behavior="allow", updatedInput={"answers": {"Kind": "task", "Is it urgent?": "No"}}))
    assert bridge.codex.answers == [(5, {"action": "accept", "content": {"kind": "task", "urgent": False}, "_meta": None})]


def test_an_mcp_form_the_app_cannot_carry_is_left_to_the_tui(bridge):
    bridge._from_controller(user("file it"))
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}  # free text
    bridge._from_codex(elicitation(mode="form", message="Title?", requestedSchema=schema))
    assert bridge.worker.calls("prompt") == [] and bridge.codex.answers == []
    assert "Answer it in the Codex TUI" in bridge.worker.events()[-1]["message"]["content"][0]["text"]


# -- settings ---------------------------------------------------------------------------
def test_the_apps_mode_menu_changes_the_threads_settings(bridge):
    request = control("set_permission_mode", mode="acceptEdits")
    bridge._from_controller(request)
    (update,) = bridge.codex.sent("thread/settings/update")
    assert (update["threadId"], update["approvalPolicy"], update["sandboxPolicy"]["type"]) == (THREAD, "on-request", "workspaceWrite")
    assert bridge.worker.calls("respond") == [(request, {})]
    assert bridge.mode == "default"  # until Codex confirms

    bridge._from_codex(note("thread/settings/updated", threadSettings={
        "model": "gpt-x", "effort": "low", "cwd": "/w", "approvalPolicy": "on-request", "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "workspaceWrite"}, "collaborationMode": {"mode": "default"}}))
    assert bridge.mode == "acceptEdits"
    init = bridge.worker.events()[-1]
    assert (init["subtype"], init["permissionMode"], init["model"], init["effort"]) == ("init", "acceptEdits", "gpt-x", "low")
    reported = bridge.worker.calls("report_metadata")[-1][0]
    assert (reported["model"], reported["effort_level"], reported["permission_mode"]) == ("gpt-x", "low", "acceptEdits")
    assert reported["container_cc_version"]  # the app asks for a diff only of a worker whose version it knows


def test_settings_that_did_not_move_are_not_announced_again(bridge):
    bridge._from_codex(note("thread/settings/updated", threadSettings={
        "model": "gpt-x", "effort": "low", "cwd": "/w", "approvalPolicy": "on-request", "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "readOnly"}, "collaborationMode": {"mode": "default"}, "serviceTier": "default"}))
    assert bridge.worker.log == []


def test_a_mode_or_model_codex_does_not_have_is_refused(bridge):
    bridge._from_controller(control("set_permission_mode", mode="yolo"))
    bridge._from_controller(control("set_model", seq=2, model="claude-opus-5-5"))
    refusals = bridge.worker.calls("reject")
    assert refusals[0][1:] == ("no such permission mode: yolo", {"error_code": "invalid_mode"})
    assert "Codex has no model 'claude-opus-5-5'" in refusals[1][1]
    assert bridge.codex.sent("thread/settings/update") == []


def test_model_and_effort_follow_the_apps_controls(bridge):
    bridge._from_controller(control("set_model", model="gpt-y"))
    bridge._from_controller(control("apply_flag_settings", seq=2, settings={"effortLevel": "high"}))
    assert bridge.codex.sent("thread/settings/update") == [
        {"threadId": THREAD, "model": "gpt-y"}, {"threadId": THREAD, "effort": "high"}]


def test_every_control_request_gets_an_answer(bridge):
    bridge._from_controller(control("get_context_usage"))
    bridge._from_controller(control("mcp_set_servers", seq=3, servers={}))
    bridge._from_controller(control("list_directory", seq=4, path=""))
    bridge.codex.refuse.add("thread/name/set")
    bridge._from_controller(control("rename_session", seq=5, title="new"))
    bridge._from_controller(control("fork_conversation", seq=6))
    bridge._from_controller(control("end_session", seq=7, reason="archived"))
    assert [call[1] for call in bridge.worker.calls("respond")][1:] == [
        {"forked": False, "reason": "requires_remote_control_server"}, {}]
    assert bridge.worker.calls("respond")[0][1]["totalTokens"] == 0
    assert [call[1:] for call in bridge.worker.calls("reject")] == [
        ("A Codex session takes its MCP servers from Codex's own configuration.", {"error_code": "unsupported_request"}),
        ("codex-rc does not handle control_request subtype: list_directory", {"error_code": "unsupported_request"}),
        ("Codex refused: thread/name/set: no", {})]


def test_a_claude_model_picked_in_the_app_is_explained_once(bridge):
    bridge._from_controller(control("set_model", model="claude-opus-5-5"))
    bridge._from_controller(control("set_model", seq=2, model="claude-opus-5-5"))
    assert len(bridge.worker.calls("reject")) == 2
    said, done = bridge.worker.events()
    assert "Codex's models: `gpt-x`, `gpt-y`. Switch with `/model <id>`" in said["message"]["content"][0]["text"]
    assert (done["type"], done["num_turns"]) == ("result", 0)


def test_an_effort_the_model_lacks_is_refused(bridge):
    bridge._from_controller(control("apply_flag_settings", settings={"effortLevel": "max"}))
    assert bridge.worker.calls("reject")[0][1] == "`gpt-x` has no effort 'max'. It takes: low, high"
    assert bridge.codex.sent("thread/settings/update") == []


def test_fast_mode_is_the_models_fast_service_tier(bridge):
    """Claude Code's `/fast`, bare or with `on` or `off`. Codex names the tier and what it costs."""
    bridge._from_controller(user("/fast"))
    assert bridge.worker.events()[0]["message"]["content"][0]["text"] == "Fast mode on, from the next turn: 2x speed, increased usage."
    bridge._from_codex(note("thread/settings/updated", threadSettings={**bridge.settings, "serviceTier": "priority"}))
    init = bridge.worker.events()[-1]
    assert (init["subtype"], init["fast_mode_state"]) == ("init", "on")  # as a Claude Code worker reports it
    bridge._from_controller(user("/fast", seq=2))  # bare, it is the other of the two
    bridge._from_controller(user("/fast on", seq=3))
    bridge._from_controller(user("/fast off", seq=4))
    assert [sent["serviceTier"] for sent in bridge.codex.sent("thread/settings/update")] == [
        "priority", "default", "priority", "default"]
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Fast mode off, from the next turn."


def test_fast_mode_is_refused_where_there_is_none(bridge):
    bridge._from_controller(user("/fast sometimes"))
    assert bridge.worker.events()[0]["message"]["content"][0]["text"] == "`/fast` takes `on` or `off`."
    bridge.settings["model"] = "gpt-y"
    bridge._from_controller(user("/fast on", seq=2))
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "`gpt-y` has no Fast mode."
    assert bridge.codex.sent("thread/settings/update") == []


# -- the app's panels: changes, files, side chat --------------------------------------------
def test_the_changes_panel_gets_the_working_trees_diff(bridge, monkeypatch):
    monkeypatch.setattr(workspace, "diff", lambda cwd: {"stats": {"filesCount": 1}, "cwd": cwd})
    request = control("get_workspace_diff")
    bridge._from_controller(request)
    assert bridge.worker.calls("respond") == []  # worked out off the loop
    settle(bridge)
    assert bridge.worker.calls("respond") == [(request, {"diff": {"stats": {"filesCount": 1}, "cwd": "/w"}}, {})]


def test_a_file_mention_searches_and_a_folder_lists(bridge, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    bridge.settings["cwd"] = str(tmp_path)
    bridge.codex.results["fuzzyFileSearch"] = {"files": [{"path": "src/a.py"}, {"path": ".git/HEAD"}]}
    for seq, query in enumerate(["", "src/", "a.p"], 1):
        bridge._from_controller(control("file_suggestions", seq=seq, query=query))
        settle(bridge)
    assert [call[1]["suggestions"] for call in bridge.worker.calls("respond")] == [
        [{"path": "src/"}], [{"path": "src/"}, {"path": "src/a.py"}], [{"path": "src/a.py"}]]
    assert bridge.codex.sent("fuzzyFileSearch") == [{"query": "a.p", "roots": [str(tmp_path)], "cancellationToken": None}]


def test_the_file_viewer_reads_only_the_workspace(bridge, tmp_path):
    (tmp_path / "a.txt").write_text("hi\n")
    bridge.settings["cwd"] = str(tmp_path)
    bridge._from_controller(control("read_file", path="a.txt"))
    bridge._from_controller(control("read_file", seq=2, path="/etc/passwd"))
    assert bridge.worker.calls("respond")[0][1] == {"absPath": "a.txt", "contents": "hi\n", "lossy": False}
    assert bridge.worker.calls("reject")[0][1] == "read denied: /etc/passwd"


def test_a_side_question_is_sampled_from_the_thread(bridge):
    nothing_yet = control("side_question", question="where?")
    bridge._from_controller(nothing_yet)
    assert bridge.worker.calls("respond") == [(nothing_yet, {"response": None, "synthetic": False}, {})]

    bridge.last_turn = "turn-0"
    bridge.codex.results["thread/sample"] = {"text": "/w"}
    asked = control("side_question", seq=2, question="where?", history=[{"question": "who?", "response": "you"}])
    bridge._from_controller(asked)
    settle(bridge)
    (sample,) = bridge.codex.sent("thread/sample")
    assert (sample["expectedTurnId"], sample["allowInProgressTurn"], sample["disableMcp"]) == ("turn-0", True, True)
    assert sample["prompt"].endswith("Q: who?\nA: you\n\nQ: where?")
    assert bridge.worker.calls("respond")[-1] == (asked, {"response": "/w", "synthetic": False}, {})
    assert bridge.codex.sent("turn/start") == []  # beside the conversation: no turn


def test_a_side_question_codex_cannot_answer_is_refused(bridge):
    bridge.last_turn = "turn-0"
    bridge.codex.refuse.add("thread/sample")
    bridge._from_controller(control("side_question", question="where?"))
    settle(bridge)
    assert bridge.worker.calls("reject")[0][1] == "thread/sample: no"


def test_a_queued_message_can_be_withdrawn(bridge):
    first, second = user("one"), user("two", uuid="u-2", seq=2)
    bridge._from_controller(first)
    bridge.codex.refuse.add("turn/steer")
    bridge._from_controller(second)  # waits for the turn to end
    bridge._from_controller(control("cancel_async_message", seq=3, message_uuid="u-2"))
    bridge._from_controller(control("cancel_async_message", seq=4, message_uuid="u-2"))
    assert [call[1] for call in bridge.worker.calls("respond")] == [{"cancelled": True}, {"cancelled": False}]
    bridge._from_codex(turn_done())
    assert len(bridge.codex.sent("turn/start")) == 1


def test_codexs_mcp_servers_show_in_the_apps_status(bridge):
    bridge.codex.results["mcpServerStatus/list"] = {"data": [
        {"name": "docs", "runtimeStatus": "connected"}, {"name": "tracker", "runtimeStatus": "authenticationRequired"}]}
    bridge._from_codex({"method": "mcpServer/startupStatus/updated", "params": {"name": "docs"}})
    servers = [{"name": "docs", "status": "connected"}, {"name": "tracker", "status": "needs-auth"}]
    assert bridge.worker.events()[-1]["mcp_servers"] == servers
    bridge._from_controller(control("mcp_status"))
    assert bridge.worker.calls("respond")[-1][1] == {"mcpServers": servers}


# -- commands answered here -------------------------------------------------------------
def test_slash_commands_are_answered_without_a_turn(bridge):
    bridge._from_controller(user("/model"))
    bridge._from_controller(user("/model gpt-y", seq=2))
    bridge._from_controller(user("/effort ludicrous", seq=3))
    bridge._from_controller(user("/sandbox", seq=4))
    said = [event["message"]["content"][0]["text"] for event in bridge.worker.events()]
    assert said[0] == "Model: `gpt-x`. Codex has: `gpt-x`, `gpt-y`."
    assert said[1] == "Model set to `gpt-y`, from the next turn."
    assert said[2] == "`gpt-x` has no effort `ludicrous`. It takes: low, high."
    assert said[3] == "Sandbox: read-only, network off. Approvals: asks you when it needs more."
    assert bridge.codex.sent("thread/settings/update") == [{"threadId": THREAD, "model": "gpt-y"}]
    assert [call[-1] for call in bridge.worker.calls("end_turn")] == [
        {"result": text, "num_turns": 0} for text in said]
    assert bridge.codex.sent("turn/start") == []


def test_a_command_this_session_lacks_never_reaches_the_model(bridge):
    """The app passes on the commands it doesn't run itself, as text."""
    bridge._from_controller(user("/rewind"))
    bridge._from_controller(user("/Frobnicate now", seq=2))
    said = [event["message"]["content"][0]["text"] for event in bridge.worker.events()]
    assert said[0].startswith("`/rewind` isn't available on a Codex session. This one takes `/clear`, `/codex`, `/compact`")
    assert said[1].startswith("`/frobnicate` isn't available")
    assert bridge.codex.sent("turn/start") == []
    bridge._from_controller(user("/tmp is full, what can go?", seq=3))  # a path, not a command
    bridge._from_controller(user("/tmp/x.log\nis huge", uuid="u-2", seq=4))
    assert len(bridge.codex.sent("turn/start")) + len(bridge.codex.sent("turn/steer")) == 2


NEW = {**OPENED, "thread": {"id": "thr-2", "cwd": "/w", "name": None}, "reasoningEffort": "medium",
       "approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}  # as Codex starts one: on its defaults


def test_clear_starts_a_thread_and_empties_the_transcript(bridge):
    """Claude Code's `/clear`: the session goes on, with nothing of the conversation before."""
    moved = []
    bridge.moved = lambda left, thread: moved.append((left, thread["id"]))
    bridge.codex.results["thread/start"] = NEW
    bridge._from_codex(note("thread/tokenUsage/updated", tokenUsage={"last": {"totalTokens": 1500}}))
    bridge._from_controller(user("/clear"))
    assert bridge.codex.sent("thread/start") == [{"cwd": "/w", "model": "gpt-x"}]
    assert bridge.codex.sent("thread/unsubscribe") == [{"threadId": THREAD}]
    assert (bridge.thread_id, bridge.usage, moved) == ("thr-2", None, [(THREAD, "thr-2")])
    reset, init, said = bridge.worker.events()
    assert (reset["type"], reset["new_conversation_id"], reset["user_message_uuid"]) == ("conversation_reset", "thr-2", "u-1")
    assert said["message"]["content"][0]["text"] == "Started a new thread. The last one is kept: `codex resume thr-1`."
    assert bridge.worker.calls("report_metadata")[0] == ({"context_usage": None},)
    assert bridge.worker.calls("title") == [("Codex: w",)]  # no longer titled after the thread it left
    assert bridge.worker.calls("end_turn")[-1][-1]["num_turns"] == 0
    bridge._from_codex(note("turn/started", turn={"id": "turn-9"}))  # the thread it left: not this session's any more
    assert bridge.turn is None
    bridge._from_controller(user("hello", uuid="u-2", seq=2))
    assert bridge.codex.sent("turn/start")[0]["threadId"] == "thr-2"


def test_the_new_thread_starts_as_the_last_one_stood(bridge):
    """Its mode, sandbox, effort and Fast mode were set in the app, and a `/clear` is no reason to lose them."""
    bridge.settings["serviceTier"] = "priority"
    bridge.codex.results["thread/start"] = NEW
    bridge._from_controller(user("/new"))  # Codex's name for it
    assert bridge.codex.sent("thread/settings/update") == [{
        "threadId": "thr-2", "effort": "low", "serviceTier": "priority", "approvalPolicy": "on-request",
        "approvalsReviewer": "user", "sandboxPolicy": {"type": "readOnly"}, "collaborationMode": {"mode": "default"}}]
    init = bridge.worker.events()[1]
    assert (init["subtype"], init["permissionMode"], init["effort"], init["fast_mode_state"]) == ("init", "default", "low", "on")
    refused = Bridge(Codex(), Worker(), OPENED)
    refused.codex.results["thread/start"] = NEW
    refused.codex.refuse.add("thread/settings/update")
    refused._from_controller(user("/clear"))
    assert refused.worker.events()[1]["permissionMode"] == "bypassPermissions"  # what Codex gave it, said as it is


def test_a_named_sessions_new_thread_takes_the_name(bridge):
    """Which is what writes it to disk, where a terminal can join it."""
    pinned = Bridge(Codex(), Worker(), OPENED, name="Agent")
    pinned.codex.remote = "unix:///run/agent/app.sock"
    pinned.codex.results["thread/start"] = NEW
    pinned._from_controller(user("/clear"))
    assert pinned.codex.sent("thread/name/set") == [{"threadId": "thr-2", "name": "Agent"}]
    assert pinned.worker.calls("title") == []  # the session keeps its name
    assert "`codex --remote unix:///run/agent/app.sock resume thr-1`" in pinned.worker.events()[-1]["message"]["content"][0]["text"]


def test_clear_waits_for_the_running_turn_and_a_thread_codex_will_not_start_changes_nothing(bridge):
    bridge.codex.refuse.add("thread/start")
    bridge._from_controller(user("/clear"))
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Codex refused: thread/start: no"
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    bridge._from_controller(user("/clear", seq=2))
    assert "middle of a turn" in bridge.worker.events()[-1]["message"]["content"][0]["text"]
    assert bridge.thread_id == THREAD and bridge.turn == "turn-1"


def test_exit_says_so_and_ends_the_bridge(bridge):
    with pytest.raises(Exited):
        bridge._from_controller(user("/exit"))
    assert bridge.worker.events()[0]["message"]["content"][0]["text"] == (
        "Off Remote Control. The thread is kept: `codex resume thr-1`.")
    assert bridge.worker.calls("end_turn")[0][-1]["num_turns"] == 0  # answered, so not delivered again
    assert bridge.codex.sent("turn/interrupt") == []


def test_exit_mid_turn_stops_the_turn(bridge):
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    with pytest.raises(Exited):
        bridge._from_controller(user("/quit"))  # Codex's name for it
    assert bridge.codex.sent("turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-1"}]
    bridge._leave()  # as the loop does on its way out
    assert bridge.worker.calls("end_turn")[-1][-1]["codex_turn"] == "turn-1"
    assert bridge.worker.events()[-1]["subtype"] == "worker_shutting_down"


def test_background_terminals_are_listed_and_stopped(bridge):
    """The app's Stop ends the turn, and what Codex left running in the background runs on."""
    bridge.codex.results["thread/backgroundTerminals/list"] = {"data": []}
    bridge._from_controller(user("/ps"))
    bridge._from_controller(user("/stop", seq=3))
    assert [e["message"]["content"][0]["text"] for e in bridge.worker.events()[-2:]] == ["No background terminals are running."] * 2
    assert bridge.codex.sent("thread/backgroundTerminals/clean") == []
    bridge.codex.results["thread/backgroundTerminals/list"] = {"data": [
        {"itemId": "i1", "processId": "p1", "command": "bash -lc 'npm run dev'", "cwd": "/w"}]}
    bridge._from_controller(user("/ps", seq=4))
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"].splitlines() == [
        "**Background terminals** (`/stop` ends them)", "- `npm run dev` in `/w`"]
    bridge._from_controller(user("/stop", seq=5))
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Stopped 1 background terminal."
    assert bridge.codex.sent("thread/backgroundTerminals/clean") == [{"threadId": THREAD}]


def test_the_codex_command_reports_what_the_apps_cards_leave_out(bridge):
    bridge.codex.results["account/rateLimits/read"] = {"rateLimits": {
        "primary": {"usedPercent": 23.4, "windowDurationMins": 300, "resetsAt": None}, "secondary": None, "planType": "plus"}}
    bridge._from_codex(note("thread/tokenUsage/updated", tokenUsage={"last": {"totalTokens": 1500}, "modelContextWindow": 3000}))
    bridge.settings["serviceTier"] = "priority"
    bridge._from_controller(user("/codex"))
    assert bridge.worker.events()[0]["message"]["content"][0]["text"].splitlines() == [
        "**Codex 0.170.0**",
        "- Thread: `thr-1` in `/w`",
        "- Model: `gpt-x`, effort `low`, Fast mode",
        "- Sandbox: read-only, network off. Approvals: asks you when it needs more.",
        "- Context: 1.5k of 3.0k tokens (50%)",
        "- Usage: 23% of the 5-hour limit used",
        "- Plan: plus",
        "- Models, for `/model <id>`: `gpt-x`, `gpt-y`",
        "- In a terminal: `codex resume thr-1`"]


def test_compact_is_a_turn_of_the_command_that_asked(bridge):
    command = user("/compact")
    bridge._from_controller(command)
    assert bridge.worker.events()[0]["message"]["content"][0]["text"] == "Nothing to compact yet."

    bridge.last_turn = "turn-0"
    again = user("/compact", uuid="u-2", seq=2)
    bridge._from_controller(again)
    assert bridge.codex.sent("thread/compact/start") == [{"threadId": THREAD}]
    assert bridge.worker.calls("begin_turn") == [(again, {})]
    bridge._from_codex(note("thread/tokenUsage/updated", tokenUsage={"last": {"totalTokens": 900}}))
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    bridge._from_codex(item("started", type="contextCompaction", id="c-1"))
    bridge._from_codex(note("thread/tokenUsage/updated", tokenUsage={"last": {"totalTokens": 200}}))  # what is left
    bridge._from_codex(item("completed", type="contextCompaction", id="c-1"))
    bridge._from_codex(turn_done())
    assert bridge.worker.events()[1:] == [
        {"type": "system", "subtype": "status", "status": "compacting"},
        {"type": "system", "subtype": "status", "status": None, "compact_result": "success"},
        {"type": "system", "subtype": "compact_boundary",
         "compact_metadata": {"trigger": "manual", "pre_tokens": 900, "post_tokens": 200}}]
    ended = bridge.worker.calls("end_turn")[-1]
    assert ended[0] is again and (ended[-1]["result"], ended[-1]["codex_turn"]) == ("Context compacted.", "turn-1")


def test_a_compaction_of_codexs_own_is_marked_too(bridge):
    bridge._from_controller(user("go on"))
    bridge._from_codex(item("started", type="contextCompaction", id="c-1"))
    bridge._from_codex(item("completed", type="contextCompaction", id="c-1"))
    assert bridge.worker.events()[2]["compact_metadata"] == {"trigger": "auto"}  # Codex gave no sizes


def test_the_app_is_told_again_of_a_compaction_that_goes_on(bridge):
    said = {"type": "system", "subtype": "status", "status": "compacting"}
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))  # at a terminal: the app asked for nothing
    bridge._from_codex(item("started", type="contextCompaction", id="c-1"))
    later(bridge, 20)
    assert bridge.worker.events() == [said]
    later(bridge, 40)  # the app would take it to be over at 75 seconds
    assert bridge.worker.events() == [said, said]
    later(bridge, 20)
    assert bridge.worker.events() == [said, said] and live(bridge) == []  # nor is the model thinking meanwhile
    bridge._from_codex(item("completed", type="contextCompaction", id="c-1"))
    later(bridge, 600)
    assert [event["subtype"] for event in bridge.worker.events()[2:]] == ["status", "compact_boundary"]


def test_a_compaction_its_turn_ends_before_is_marked_as_failed(bridge):
    bridge._from_controller(user("go on"))
    bridge._from_codex(item("started", type="contextCompaction", id="c-1"))
    bridge._from_codex(turn_done("interrupted"))
    assert bridge.worker.events()[1] == {"type": "system", "subtype": "status", "status": None, "compact_result": "failed"}
    later(bridge, 600)
    assert len(bridge.worker.events()) == 2


def test_review_runs_codexs_review_and_writes_its_findings_once(bridge):
    command = user("/review")
    bridge._from_controller(command)
    assert bridge.codex.sent("review/start") == [
        {"threadId": THREAD, "target": {"type": "uncommittedChanges"}, "delivery": "inline"}]
    assert bridge.turn == "turn-1"
    bridge._from_codex(item("completed", type="enteredReviewMode", id="r-1", review="current changes"))
    bridge._from_codex(item("completed", type="userMessage", id="i-1", clientId=None, content=[{"type": "text", "text": "Review the changes"}]))
    bridge._from_codex(item("completed", type="exitedReviewMode", id="r-2", review="One finding."))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="One finding."))  # the same, as a message
    bridge._from_codex(turn_done())
    assert [event["message"]["content"][0]["text"] for event in bridge.worker.events()] == [
        "Reviewing current changes.", "One finding."]
    assert bridge.worker.calls("end_turn")[0][0] is command

    bridge._from_controller(user("/review only the tests", uuid="u-2", seq=2))
    assert bridge.codex.sent("review/start")[-1]["target"] == {"type": "custom", "instructions": "only the tests"}
    bridge._from_codex(item("completed", type="exitedReviewMode", id="r-3", review="Nothing to fix."))
    bridge._from_codex(turn_done())  # no message repeated it: written as the turn ends
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Nothing to fix."
    assert bridge.worker.calls("end_turn")[-1][-1]["result"] == "Nothing to fix."


def test_a_turn_command_waits_for_the_running_turn(bridge):
    bridge._from_controller(user("work"))
    bridge._from_controller(user("/review", uuid="u-2", seq=2))
    assert bridge.codex.sent("review/start") == []
    assert "in the middle of a turn" in bridge.worker.events()[0]["message"]["content"][0]["text"]


def test_a_command_mid_turn_leaves_the_turn_running(bridge):
    bridge._from_controller(user("work"))
    command = user("/effort", seq=2)
    bridge._from_controller(command)
    assert bridge.worker.calls("end_turn") == []
    assert bridge.worker.calls("report_delivery") == [(command, "processing", {}), (command, "processed", {})]
    bridge._from_codex(note("warning", message="Usage is running low."))
    assert [event["message"]["stop_reason"] for event in bridge.worker.events()] == [None, None]  # not the turn's end

    bridge._from_codex(turn_done())
    bridge._from_controller(user("/effort", uuid="u-3", seq=3))
    assert bridge.worker.events()[-1]["message"]["stop_reason"] == "end_turn"


# -- a reply as it is written -------------------------------------------------------------
def delta(text):
    return note("item/agentMessage/delta", turnId="turn-1", itemId="msg_1", delta=text)


def test_a_reply_is_streamed_to_watchers_then_kept_once(bridge):
    bridge._from_controller(user("say hi"))
    bridge._from_codex(delta("Hel"))
    bridge._from_codex(delta("lo"))  # within the flush interval: held
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="Hello"))
    live = [event["event"] for event in bridge.worker.events("ephemeral")]
    assert [event["type"] for event in live] == [
        "message_start", "content_block_start", "content_block_delta",  # the first piece, at once
        "content_block_delta", "content_block_stop",  # the rest, as the message ends
        "message_delta", "message_stop"]
    assert live[0]["message"]["id"] == "msg_1"
    assert [live[2]["delta"]["text"], live[3]["delta"]["text"]] == ["Hel", "lo"]
    (kept,) = bridge.worker.events()
    assert kept["message"]["id"] == "msg_1" and kept["message"]["content"][0]["text"] == "Hello"
    # The stored message goes out between the block's end and the message's.
    order = [entry[0] for entry in bridge.worker.log if entry[0] in ("event", "ephemeral")]
    assert order == ["ephemeral"] * 5 + ["event"] + ["ephemeral"] * 2


def test_a_thought_is_streamed_as_thinking(bridge):
    bridge._from_controller(user("think"))
    for index, text in enumerate(["First.", "Then."]):
        bridge._from_codex(note("item/reasoning/summaryPartAdded", turnId="turn-1", itemId="rs_1", summaryIndex=index))
        bridge._from_codex(note("item/reasoning/summaryTextDelta", turnId="turn-1", itemId="rs_1", summaryIndex=index, delta=text))
    bridge._from_codex(item("completed", type="reasoning", id="rs_1", summary=["First.", "Then."], content=[]))
    live = [event["event"] for event in bridge.worker.events("ephemeral")]
    assert live[1]["content_block"] == {"type": "thinking", "thinking": "", "signature": ""}
    assert "".join(e["delta"]["thinking"] for e in live if e["type"] == "content_block_delta") == "First.\n\nThen."
    (kept,) = bridge.worker.events()
    assert kept["message"]["id"] == "rs_1" == live[0]["message"]["id"]
    assert kept["message"]["content"][0]["thinking"] == "First.\n\nThen."


def test_a_message_that_never_finished_is_closed_when_the_next_begins(bridge):
    bridge._from_controller(user("say hi"))
    bridge._from_codex(delta("Hel"))
    bridge._from_codex(note("item/agentMessage/delta", turnId="turn-1", itemId="msg_2", delta="Again"))
    kinds = [event["event"]["type"] for event in bridge.worker.events("ephemeral")]
    assert kinds == ["message_start", "content_block_start", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    assert (bridge.stream, bridge.stream_text) == ("msg_2", "Again")  # held for the next flush


def test_nothing_is_streamed_when_nobody_is_watching(bridge):
    bridge.worker.watched = False
    bridge._from_controller(user("say hi"))
    bridge._from_codex(item("started", **COMMAND))  # its upload's ack says nobody watches
    bridge._from_codex(delta("Hello"))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="Hello"))
    assert bridge.worker.events("ephemeral") == []
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Hello"


def test_only_the_answer_ends_as_the_end_of_the_turn(bridge):
    bridge._from_controller(user("fix it"))
    for phase in ("commentary", "final_answer"):
        bridge._from_codex(delta(phase))
        bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text=phase, phase=phase))
    on_the_way, answer = bridge.worker.events()
    assert (on_the_way["message"]["stop_reason"], answer["message"]["stop_reason"]) == (None, "end_turn")
    ends = [event["delta"]["stop_reason"] for event in live(bridge) if event["type"] == "message_delta"]
    assert ends == [None, "end_turn"]


# -- what Codex does not say: that its model is thinking -------------------------------------
THOUGHT_ENDS = ["content_block_stop", "message_delta", "message_stop"]


def test_a_turn_gone_quiet_shows_the_model_thinking(bridge):
    bridge._from_controller(user("work it out"))
    later(bridge, 0.5)
    assert live(bridge) == []  # an answer that comes at once needs no word of it
    later(bridge, 2)
    begun, block = live(bridge)
    assert begun["type"] == "message_start"
    assert block == {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "thinking", "thinking": "", "signature": ""}}
    later(bridge, 60)
    assert len(live(bridge)) == 2  # said once

    # Codex reports the thought as it ends, with nothing to show: the model is still at it.
    bridge._from_codex(item("started", type="reasoning", id="rs_1", summary=[], content=[]))
    bridge._from_codex(item("completed", type="reasoning", id="rs_1", summary=[], content=[]))
    assert len(live(bridge)) == 2 and bridge.worker.events() == []

    bridge._from_codex(item("started", **COMMAND))  # something to show: the thought ends first
    assert [event["type"] for event in live(bridge)[2:]] == THOUGHT_ENDS
    assert live(bridge)[3]["delta"]["stop_reason"] is None
    assert [entry[0] for entry in bridge.worker.log if entry[0] in ("event", "ephemeral")] == ["ephemeral"] * 5 + ["event"]
    later(bridge, 60)
    assert len(live(bridge)) == 5  # a command is running: the model waits on it

    bridge._from_codex(item("completed", **{**COMMAND, "status": "completed", "aggregatedOutput": "", "exitCode": 0}))
    later(bridge, 2)
    again = live(bridge)[5]
    assert again["type"] == "message_start" and again["message"]["id"] != begun["message"]["id"]


def test_a_thought_codex_does_report_takes_the_place_of_the_empty_one(bridge):
    bridge._from_controller(user("think"))
    later(bridge, 2)
    bridge._from_codex(note("item/reasoning/summaryTextDelta", turnId="turn-1", itemId="rs_1", summaryIndex=0, delta="First."))
    kinds = [event["type"] for event in live(bridge)]
    assert kinds == ["message_start", "content_block_start", *THOUGHT_ENDS,
                     "message_start", "content_block_start", "content_block_delta"]
    assert live(bridge)[5]["message"]["id"] == "rs_1"
    later(bridge, 60)
    assert len(live(bridge)) == 8  # being written: nothing to add


def test_the_model_is_not_thinking_while_someone_is_asked_or_once_it_has_answered(bridge):
    bridge._from_controller(user("clean up"))
    later(bridge, 2)
    bridge._from_codex(approval())
    assert [event["type"] for event in live(bridge)[2:]] == THOUGHT_ENDS
    later(bridge, 60)
    assert len(live(bridge)) == 5  # the turn waits on the answer

    bridge._from_controller(answer("prompt-1", behavior="allow"))
    later(bridge, 2)
    assert len(live(bridge)) == 7

    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="Done.", phase="final_answer"))
    assert [event["type"] for event in live(bridge)[7:]] == THOUGHT_ENDS
    later(bridge, 60)
    assert len(live(bridge)) == 10  # what is left of the turn is Codex's own

    bridge._from_codex(turn_done())
    later(bridge, 60)
    assert len(live(bridge)) == 10 and bridge.thought is None


def test_a_turn_that_ends_takes_the_thought_with_it(bridge):
    bridge._from_controller(user("work"))
    later(bridge, 2)
    bridge._from_codex(turn_done("interrupted"))
    assert [event["type"] for event in live(bridge)[2:]] == THOUGHT_ENDS
    assert [entry[0] for entry in bridge.worker.log if entry[0] in ("ephemeral", "end_turn")] == ["ephemeral"] * 5 + ["end_turn"]


def test_nobody_watching_is_shown_no_thought_and_a_controller_attaching_is(bridge):
    bridge.worker.watched = False
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    bridge._from_codex(item("completed", type="userMessage", id="i-1", clientId=None, content=[]))  # its upload's ack
    later(bridge, 60)
    assert live(bridge) == []

    bridge._from_controller(control("initialize"))
    later(bridge, 2)
    assert [event["type"] for event in live(bridge)] == ["message_start", "content_block_start"]

    bridge._from_controller(control("initialize", seq=2))  # another: it missed the thought's start
    later(bridge, 2)
    assert [event["type"] for event in live(bridge)[2:]] == [*THOUGHT_ENDS, "message_start", "content_block_start"]


def test_a_turn_taken_up_as_a_command_runs_is_not_the_model_thinking(bridge):
    running = {"id": "turn-1", "status": "inProgress", "items": [COMMAND]}
    bridge.codex.results["thread/turns/list"] = {"data": [running]}
    bridge.backfill(new=True)
    bridge._adopt_running_turn()
    later(bridge, 60)
    assert live(bridge) == []
    bridge._from_codex(item("completed", **{**COMMAND, "status": "completed", "aggregatedOutput": "", "exitCode": 0}))
    later(bridge, 2)
    assert [event["type"] for event in live(bridge)] == ["message_start", "content_block_start"]


def test_the_loop_acts_on_the_clock_between_what_it_is_sent(bridge, monkeypatch):
    monkeypatch.setattr(bridge_module, "_THINK_AFTER_S", 0.05)
    bridge._inbox.put(("codex", note("turn/started", turn={"id": "turn-1"})))
    threading.Timer(0.5, bridge._inbox.put, [("codex", AppServerClosed("gone"))]).start()
    bridge._serve()  # until the connection drops
    assert [event["type"] for event in live(bridge)] == ["message_start", "content_block_start"]  # once, not in a spin


# -- the rest -----------------------------------------------------------------------------
def test_a_plan_shows_as_a_checklist(bridge):
    bridge._from_codex(note("turn/plan/updated", turnId="turn-1", plan=[{"step": "read", "status": "inProgress"}]))
    todo, done = bridge.worker.events()
    block = todo["message"]["content"][0]
    assert block["name"] == "TodoWrite" and block["input"]["todos"][0]["status"] == "in_progress"
    assert done["message"]["content"][0]["tool_use_id"] == block["id"]


def test_the_session_takes_the_threads_name(bridge):
    bridge._from_codex(note("thread/name/updated", threadName="Fix the parser"))
    assert bridge.worker.calls("title") == [("Codex: Fix the parser",)]
    assert session_title(None, "/home/me/project") == "Codex: project"
    pinned = Bridge(Codex(), Worker(), OPENED, name="Pinned")
    pinned._from_codex(note("thread/name/updated", threadName="Fix the parser"))
    assert pinned.worker.log == []


def test_a_closed_thread_ends_the_bridge(bridge):
    with pytest.raises(ThreadClosed):
        bridge._from_codex(note("thread/closed"))


def test_stopping_mid_turn_closes_what_the_app_shows_as_open(bridge):
    bridge._leave()
    left = {"type": "system", "subtype": "worker_shutting_down", "reason": "host_exit"}
    assert bridge.worker.log == [("event", left)]  # idle: nothing to close, only to say it is leaving
    bridge._from_codex(note("turn/started", turn={"id": "turn-1"}))
    bridge._from_codex(item("started", **COMMAND))  # `codex-rc --stop`, say
    bridge._leave()
    call = bridge.worker.events()[1]["message"]["content"][0]
    closed = bridge.worker.events()[2]["message"]["content"][0]
    assert (closed["type"], closed["tool_use_id"]) == ("tool_result", call["id"])
    assert "stopped" in closed["content"]
    assert bridge.worker.calls("end_turn")[-1][-1]["result"] == closed["content"]
    assert bridge.worker.events()[-1] == left


def turn(n, status="completed"):
    return {"id": f"t{n}", "status": status, "durationMs": 5, "items": [
        {"type": "userMessage", "id": f"u{n}", "clientId": None, "content": [{"type": "text", "text": f"q{n}"}]},
        {"type": "agentMessage", "id": f"msg_{n}", "text": f"a{n}"}]}


def long_turn(n, said, status="completed"):
    """A turn left to work: one prompt, and ``said`` messages from Codex."""
    return {**turn(n, status), "items": turn(n)["items"][:1] + [
        {"type": "agentMessage", "id": f"msg_{n}_{i}", "text": f"a{n}.{i}"} for i in range(said)]}


def result(turn_id=None, **fields):
    return Event.from_wire({"sequence_num": 1, "event_id": "ev", "source": "worker", "payload": {
        "type": "result", "num_turns": 1, **({"codex_turn": turn_id} if turn_id else {}), **fields}})


def test_a_new_session_is_given_the_threads_latest_turns(bridge):
    bridge.codex.results["thread/turns/list"] = {"data": [turn(2), turn(1)]}  # newest first
    bridge.backfill(new=True)
    assert bridge.codex.sent("thread/turns/list") == [
        {"threadId": THREAD, "limit": 30, "sortDirection": "desc", "itemsView": "full"}]
    written = bridge.worker.events()
    assert [event["type"] for event in written] == ["user", "assistant", "result"] * 2
    assert [event["message"]["content"] for event in written if event["type"] == "user"] == ["q1", "q2"]
    assert [event["codex_turn"] for event in written if event["type"] == "result"] == ["t1", "t2"]


def test_a_session_served_before_catches_up_on_the_turns_since(bridge):
    """What was done in a terminal while the thread was off Remote Control."""
    bridge.codex.results["thread/turns/list"] = {"data": [turn(3), turn(2), turn(1)]}
    bridge.worker.held = [result(None, num_turns=0), result("t1")]  # a command's answer, then the last turn written
    bridge.backfill(new=False)
    assert [event["codex_turn"] for event in bridge.worker.events() if event["type"] == "result"] == ["t2", "t3"]


def test_a_long_turn_is_written_in_uploads_claude_ai_takes(bridge):
    """claude.ai takes at most 100 events in one upload, and turns can hold a thousand items."""
    bridge.codex.results["thread/turns/list"] = {"data": [long_turn(2, 230, status="inProgress"), long_turn(1, 150)]}
    bridge.backfill(new=True)
    assert max(bridge.worker.uploads) == 100
    assert len(bridge.worker.events()) == (1 + 150 + 1) + (1 + 229)  # the running turn's last message is still to come


def test_a_long_history_is_cut_to_the_latest_turns_it_has_room_for(bridge, monkeypatch):
    monkeypatch.setattr(bridge_module, "_BACKFILL_ITEMS", 10)
    bridge.codex.results["thread/turns/list"] = {"data": [long_turn(4, 3), long_turn(3, 5), long_turn(2, 1), turn(1)]}
    bridge.backfill(new=True)
    assert [event["codex_turn"] for event in bridge.worker.events() if event["type"] == "result"] == ["t3", "t4"]

    # The latest turn is written whatever its size.
    monkeypatch.setattr(bridge_module, "_BACKFILL_ITEMS", 2)
    fresh = Bridge(Codex(), Worker(), OPENED)
    fresh.codex.results["thread/turns/list"] = {"data": [long_turn(2, 40), turn(1)]}
    fresh.backfill(new=True)
    assert [event["codex_turn"] for event in fresh.worker.events() if event["type"] == "result"] == ["t2"]


def test_a_transcript_that_does_not_name_its_turns_is_left_alone(bridge):
    bridge.codex.results["thread/turns/list"] = {"data": [turn(1)]}
    bridge.worker.held = [result(None)]  # written by a bridge from before turns were named
    bridge.backfill(new=False)
    assert bridge.worker.log == []
    bridge.worker.held = []  # a session that never saw a turn holds none of them
    bridge.backfill(new=False)
    assert [event["type"] for event in bridge.worker.events()] == ["user", "assistant", "result"]


def test_a_turn_running_as_the_bridge_attaches_is_written_as_far_as_it_got(bridge):
    running = {"id": "t2", "status": "inProgress", "items": [
        {"type": "userMessage", "id": "u2", "clientId": None, "content": [{"type": "text", "text": "q2"}]},
        {**COMMAND, "id": "exec-0", "status": "completed", "aggregatedOutput": "a", "exitCode": 0},
        COMMAND,  # still running
        {"type": "agentMessage", "id": "msg_2", "text": "Loo"}]}  # may still be being written
    bridge.codex.results["thread/turns/list"] = {"data": [running]}
    bridge.backfill(new=True)
    typed, _, output, open_call = bridge.worker.events()
    assert typed["message"]["content"] == "q2" and output["message"]["content"][0]["content"] == "a"
    assert open_call["message"]["content"][0]["id"] == "exec-1"

    # What Codex still reports of the items written is dropped; the open call's end closes it.
    bridge._from_codex(item("started", **COMMAND))
    bridge._from_codex(item("completed", **{**COMMAND, "id": "exec-0", "status": "completed"}))
    assert len(bridge.worker.events()) == 4
    bridge._from_codex(item("completed", **{**COMMAND, "status": "completed", "aggregatedOutput": "b", "exitCode": 0}))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_2", text="Look: b"))
    closed, said = bridge.worker.events()[4:]
    assert closed["message"]["content"][0] == {"type": "tool_result", "tool_use_id": "exec-1", "content": "b", "is_error": False}
    assert said["message"]["content"][0]["text"] == "Look: b"


def test_a_running_turns_last_message_is_written_if_it_is_finished(bridge):
    said = {"type": "agentMessage", "id": "msg_2", "text": "I'll run the tests.", "completedAtMs": 1795200000000}
    bridge.codex.results["thread/turns/list"] = {"data": [{"id": "t2", "status": "inProgress", "items": [said]}]}
    bridge.backfill(new=True)  # the model is thinking, or writing out a call: no word of this message is to come
    (kept,) = bridge.worker.events()
    assert kept["message"]["content"][0]["text"] == "I'll run the tests."
    bridge._from_codex(item("completed", **said))  # unless it finished as the bridge attached
    assert len(bridge.worker.events()) == 1


def test_a_thread_nobody_has_written_to_has_no_history(bridge):
    bridge.codex.request = lambda method, params=None, **_: (_ for _ in ()).throw(
        AppServerError(method, {"message": "thread thr-1 is not materialized yet"}))
    bridge.backfill(new=True)
    bridge._adopt_running_turn()
    assert bridge.worker.log == [] and bridge.turn is None


def test_a_turn_written_from_history_is_not_written_again(bridge):
    """A turn that ends as the bridge attaches is in the history it backfills
    and in the notifications it reads next."""
    done = {"id": "turn-1", "status": "completed", "durationMs": 5, "items": [{**COMMAND, "status": "completed", "aggregatedOutput": "a.txt"}]}
    bridge.codex.results["thread/turns/list"] = {"data": [done]}
    bridge.backfill(new=True)
    written = len(bridge.worker.log)
    bridge._from_codex(item("completed", **done["items"][0]))
    bridge._from_codex(turn_done())
    assert len(bridge.worker.log) == written
    bridge._from_codex(note("turn/started", turn={"id": "turn-2"}))  # the next turn is passed on as ever
    assert bridge.turn == "turn-2"


# -- files attached in the app ---------------------------------------------------------------
def test_an_attached_file_is_fetched_for_codex(bridge, tmp_path):
    bridge.worker.files = {"11112222-aaaa": b"notes", "33334444-bbbb": b"png"}
    bridge._from_controller(user("read these", file_attachments=[
        {"file_uuid": "11112222-aaaa", "file_name": "../notes.txt", "is_image": False},
        {"file_uuid": "33334444-bbbb", "file_name": "shot.png", "is_image": True},
        {"file_uuid": "gone", "file_name": "lost.pdf", "is_image": False}]))
    uploads = tmp_path / "codex-remote-control" / "uploads" / THREAD
    assert (uploads / "11112222-notes.txt").read_bytes() == b"notes"
    assert bridge.codex.sent("turn/start")[0]["input"] == [
        {"type": "text", "text": "read these", "text_elements": []},
        {"type": "localImage", "path": str(uploads / "33334444-shot.png")},
        {"type": "text", "text_elements": [], "text": (
            f"[Attached file: {uploads / '11112222-notes.txt'}]\n[An attached file, lost.pdf, could not be fetched]")}]


# -- what Codex says beside a turn -------------------------------------------------------------
def test_a_warning_about_the_thread_is_said_in_the_transcript(bridge):
    bridge._from_controller(user("go"))
    bridge._from_codex(note("warning", message="You have 5% of your weekly limit left."))
    bridge._from_codex(note("model/rerouted", turnId="turn-1", fromModel="gpt-x", toModel="gpt-y", reason="capacity"))
    bridge._from_codex({"method": "warning", "params": {"threadId": None, "message": "not about this thread"}})
    assert [event["message"]["content"][0]["text"] for event in bridge.worker.events()] == [
        "You have 5% of your weekly limit left.", "Codex is running this turn on `gpt-y` instead of `gpt-x`."]
