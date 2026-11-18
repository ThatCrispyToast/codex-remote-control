"""The bridge between a recorded Codex app-server and a recorded worker seat."""
import pytest
from claude_rc import Event

from codex_remote_control.appserver import AppServerError
from codex_remote_control.bridge import Bridge, ThreadClosed, session_title

THREAD = "thr-1"
OPENED = {
    "thread": {"id": THREAD, "cwd": "/w", "name": None},
    "model": "gpt-x", "reasoningEffort": "low", "cwd": "/w",
    "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"},
    "collaborationMode": {"mode": "default"},
}
MODELS = {"data": [
    {"id": "gpt-x", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
    {"id": "gpt-y", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
]}


class Codex:
    """Records the bridge's requests and answers; `refuse` names methods to fail."""

    def __init__(self):
        self.requests, self.answers, self.refuse = [], [], set()
        self.results = {"turn/start": {"turn": {"id": "turn-1"}}, "model/list": MODELS, "thread/turns/list": {"data": []}}

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

    def send(self, *payloads, ephemeral=False):
        self.log += [("ephemeral" if ephemeral else "event", p) for p in payloads]
        return {"has_subscribers": self.watched}

    def request_permission(self, tool, tool_input, **kw):
        self.prompts += 1
        self.log.append(("prompt", tool, tool_input, kw))
        return f"prompt-{self.prompts}"

    def rename_session(self, session_id, title):
        self.log.append(("title", title))

    def __getattr__(self, name):  # begin_turn, end_turn, respond, reject, report_*
        return lambda *args, **kw: self.log.append((name, *args, kw))

    def events(self, kind="event"):
        return [entry[1] for entry in self.log if entry[0] == kind]

    def calls(self, name):
        return [entry[1:] for entry in self.log if entry[0] == name]


@pytest.fixture
def bridge():
    return Bridge(Codex(), Worker(), OPENED, follow_name=True)


def user(text, uuid="u-1", seq=1):
    return Event.from_wire({"sequence_num": seq, "event_id": f"ev-{seq}", "source": "client", "payload": {
        "type": "user", "uuid": uuid, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}})


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
    assert ended[1] == {"result": "One file: a", "is_error": False, "duration_ms": 1200, "usage": {
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
    idle = Bridge(Codex(), Worker(), OPENED, follow_name=True)
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
    assert prompt[2] == {"tool_use_id": "exec-1", "description": "Run: rm x", "raw_command": "rm x"}

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
    assert bridge.worker.calls("report_metadata")[-1] == (
        {"model": "gpt-x", "effort_level": "low", "permission_mode": "acceptEdits"},)


def test_settings_that_did_not_move_are_not_announced_again(bridge):
    bridge._from_codex(note("thread/settings/updated", threadSettings={
        "model": "gpt-x", "effort": "low", "cwd": "/w", "approvalPolicy": "on-request", "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "readOnly"}, "collaborationMode": {"mode": "default"}, "serviceTier": "fast"}))
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
    bridge._from_controller(control("get_workspace_diff", seq=2))
    bridge._from_controller(control("mcp_set_servers", seq=3, servers={}))
    bridge.codex.refuse.add("thread/name/set")
    bridge._from_controller(control("rename_session", seq=4, title="new"))
    assert [call[1]["totalTokens"] if "totalTokens" in call[1] else call[1] for call in bridge.worker.calls("respond")] == [
        0, {"diff": None}]
    assert [call[1] for call in bridge.worker.calls("reject")] == [
        "codex-remote-control does not handle control_request subtype: mcp_set_servers",
        "Codex refused: thread/name/set: no"]


# -- commands answered here -------------------------------------------------------------
def test_slash_commands_are_answered_without_a_turn(bridge):
    bridge._from_controller(user("/model"))
    bridge._from_controller(user("/model gpt-y", seq=2))
    bridge._from_controller(user("/effort ludicrous", seq=3))
    bridge._from_controller(user("/modelling is fun", seq=4))  # not a command: goes to Codex
    said = [event["message"]["content"][0]["text"] for event in bridge.worker.events()]
    assert said[0] == "Model: `gpt-x`. Codex has: `gpt-x`, `gpt-y`."
    assert said[1] == "Model set to `gpt-y`, from the next turn."
    assert said[2] == "`gpt-x` has no effort `ludicrous`. It takes: low, high."
    assert bridge.codex.sent("thread/settings/update") == [{"threadId": THREAD, "model": "gpt-y"}]
    assert [call[-1] for call in bridge.worker.calls("end_turn")] == [
        {"result": text, "num_turns": 0} for text in said]
    assert len(bridge.codex.sent("turn/start")) == 1


def test_a_command_mid_turn_leaves_the_turn_running(bridge):
    bridge._from_controller(user("work"))
    command = user("/effort", seq=2)
    bridge._from_controller(command)
    assert bridge.worker.calls("end_turn") == []
    assert bridge.worker.calls("report_delivery") == [(command, "processing", {}), (command, "processed", {})]


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


def test_nothing_is_streamed_when_nobody_is_watching(bridge):
    bridge.worker.watched = False
    bridge._from_controller(user("say hi"))
    bridge._from_codex(item("started", **COMMAND))  # its upload's ack says nobody watches
    bridge._from_codex(delta("Hello"))
    bridge._from_codex(item("completed", type="agentMessage", id="msg_1", text="Hello"))
    assert bridge.worker.events("ephemeral") == []
    assert bridge.worker.events()[-1]["message"]["content"][0]["text"] == "Hello"


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
    pinned = Bridge(Codex(), Worker(), OPENED, follow_name=False)
    pinned._from_codex(note("thread/name/updated", threadName="Fix the parser"))
    assert pinned.worker.log == []


def test_a_closed_thread_ends_the_bridge(bridge):
    with pytest.raises(ThreadClosed):
        bridge._from_codex(note("thread/closed"))


def test_backfill_writes_earlier_turns_oldest_first(bridge):
    def turn(n, status="completed"):
        return {"id": f"t{n}", "status": status, "durationMs": 5, "items": [
            {"type": "userMessage", "id": f"u{n}", "clientId": None, "content": [{"type": "text", "text": f"q{n}"}]},
            {"type": "agentMessage", "id": f"msg_{n}", "text": f"a{n}"}]}

    bridge.codex.results["thread/turns/list"] = {"data": [turn(3, "inProgress"), turn(2), turn(1)]}  # newest first
    bridge.backfill()
    assert bridge.codex.sent("thread/turns/list") == [
        {"threadId": THREAD, "limit": 30, "sortDirection": "desc", "itemsView": "full"}]
    written = bridge.worker.events()
    assert [event["type"] for event in written] == ["user", "assistant", "result"] * 2
    assert [event["message"]["content"] for event in written if event["type"] == "user"] == ["q1", "q2"]
