"""Codex's vocabulary restated in Claude Code's."""
import pytest
from claude_rc import Event

from codex_remote_control import translate

SETTINGS = {
    "model": "gpt-x", "effort": "low", "cwd": "/w", "approvalPolicy": "on-request", "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "workspaceWrite"}, "collaborationMode": {"mode": "default"},
}


# -- modes ----------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto", "bypassPermissions", "plan"])
def test_a_mode_set_on_a_thread_is_the_mode_it_then_reports(mode):
    updated = {**SETTINGS, **translate.mode_settings(mode, SETTINGS)}
    assert translate.permission_mode(updated) == mode


def test_plan_mode_leaves_what_may_be_touched_alone():
    assert set(translate.mode_settings("plan", SETTINGS)) == {"collaborationMode"}
    leaving = translate.mode_settings("acceptEdits", {**SETTINGS, "collaborationMode": {"mode": "plan"}})
    assert leaving["collaborationMode"]["mode"] == "default"
    assert leaving["collaborationMode"]["settings"] == {
        "model": "gpt-x", "reasoning_effort": "low", "developer_instructions": None}


def test_codex_settings_with_no_claude_name_get_the_nearest_mode():
    asks_for_everything = {**SETTINGS, "approvalPolicy": "untrusted"}
    assert translate.permission_mode(asks_for_everything) == "default"
    never_asks_in_the_sandbox = {**SETTINGS, "approvalPolicy": "never"}
    assert translate.permission_mode(never_asks_in_the_sandbox) == "acceptEdits"


# -- input ----------------------------------------------------------------------
def test_user_input_takes_text_and_images():
    message = Event(payload={"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "tool_result", "tool_use_id": "t", "content": "ignored"},
    ]}})
    assert translate.user_input(message) == [
        {"type": "text", "text": "look", "text_elements": []},
        {"type": "image", "url": "data:image/png;base64,QUJD"},
    ]
    plain = Event(payload={"type": "user", "message": {"role": "user", "content": "hi"}})
    assert translate.user_input(plain) == [{"type": "text", "text": "hi", "text_elements": []}]


# -- tool calls -------------------------------------------------------------------
def test_shell_script_unwraps_the_shell_codex_runs_commands_in():
    assert translate.shell_script("/run/current-system/sw/bin/bash -lc 'date +%s\nprintf hi > hello.txt'") == (
        "date +%s\nprintf hi > hello.txt")
    assert translate.shell_script("git status --short") == "git status --short"
    assert translate.shell_script("echo 'unbalanced") == "echo 'unbalanced"


def test_a_command_is_a_bash_call_and_its_output():
    item = {"type": "commandExecution", "id": "exec-1", "command": "bash -lc 'ls'", "status": "completed",
            "aggregatedOutput": "a\nb\n", "exitCode": 0}
    assert translate.tool_calls(item) == [
        {"type": "tool_use", "id": "exec-1", "name": "Bash", "input": {"command": "ls"}}]
    assert translate.tool_output(item) == ("a\nb\n", False)
    assert translate.tool_output({**item, "exitCode": 2}) == ("a\nb\n", True)
    assert translate.tool_output({**item, "status": "declined"}) == ("The command was not approved.", True)


def test_long_output_is_cut():
    item = {"type": "commandExecution", "id": "e", "command": "x", "status": "completed",
            "aggregatedOutput": "x" * 30_500, "exitCode": 0}
    output, failed = translate.tool_output(item)
    assert output.endswith("… (500 more characters)") and not failed


def test_a_patch_is_a_write_or_an_edit_per_file():
    item = {"type": "fileChange", "id": "p-1", "status": "completed", "changes": [
        {"path": "/w/new.txt", "kind": {"type": "add"}, "diff": "alpha\nbeta\n"},
        {"path": "/w/old.txt", "kind": {"type": "update", "move_path": None},
         "diff": "@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n@@ -9,1 +9,2 @@\n tail\n+more\n\\ No newline at end of file\n"},
        {"path": "/w/gone.txt", "kind": {"type": "delete"}, "diff": "bye\n"},
    ]}
    write, edit, delete = translate.tool_calls(item)
    assert write == {"type": "tool_use", "id": "p-1", "name": "Write",
                     "input": {"file_path": "/w/new.txt", "content": "alpha\nbeta\n"}}
    assert edit["id"] == "p-1/1" and edit["name"] == "Edit"
    assert edit["input"] == {"file_path": "/w/old.txt",
                             "old_string": "alpha\nbeta\n…\ntail", "new_string": "alpha\ngamma\n…\ntail\nmore"}
    assert delete["input"] == {"file_path": "/w/gone.txt", "old_string": "bye\n", "new_string": ""}
    assert translate.tool_output(item) == ("Applied.", False)
    assert translate.tool_output({**item, "status": "declined"}) == ("The change was not approved.", True)


def test_mcp_calls_and_web_searches_keep_their_names():
    mcp = {"type": "mcpToolCall", "id": "m", "server": "docs", "tool": "search", "arguments": {"q": "x"},
           "status": "completed", "result": {"content": [{"type": "text", "text": "found"}]}, "error": None}
    assert translate.tool_calls(mcp)[0]["name"] == "mcp__docs__search"
    assert translate.tool_output(mcp) == ("found", False)
    assert translate.tool_output({**mcp, "status": "failed", "error": {"message": "down"}}) == ("down", True)
    search = {"type": "webSearch", "id": "s", "query": "websockets"}
    assert translate.tool_calls(search)[0] == {
        "type": "tool_use", "id": "s", "name": "WebSearch", "input": {"query": "websockets"}}
    assert translate.tool_calls({"type": "contextCompaction", "id": "c"}) == []


def test_transcript_of_each_kind_of_item():
    typed = {"type": "userMessage", "id": "u", "clientId": None, "content": [{"type": "text", "text": "hi"}]}
    assert translate.transcript(typed, "gpt-x") == [
        {"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": "hi"}}]
    said = translate.transcript({"type": "agentMessage", "id": "msg_1", "text": "hello"}, "gpt-x")
    assert said[0]["message"]["id"] == "msg_1" and said[0]["message"]["content"] == [{"type": "text", "text": "hello"}]
    assert translate.transcript({"type": "agentMessage", "id": "msg_2", "text": ""}, "gpt-x") == []
    thought = translate.transcript({"type": "reasoning", "id": "r", "summary": ["a", "b"], "content": []}, "gpt-x")
    assert thought[0]["message"]["content"] == [{"type": "thinking", "thinking": "a\n\nb", "signature": ""}]
    assert translate.transcript({"type": "reasoning", "id": "r", "summary": [], "content": []}, "gpt-x") == []
    ran = translate.transcript({"type": "commandExecution", "id": "e", "command": "ls", "status": "completed",
                                "aggregatedOutput": "a", "exitCode": 0}, "gpt-x")
    assert [event["type"] for event in ran] == ["assistant", "user"]
    assert ran[1]["message"]["content"][0] == {
        "type": "tool_result", "tool_use_id": "e", "content": "a", "is_error": False}
    assert translate.transcript({"type": "imageView", "id": "i", "path": "/x.png"}, "gpt-x") == []


def test_a_plan_is_a_todo_list():
    plan = [{"step": "read", "status": "completed"}, {"step": "write", "status": "inProgress"},
            {"step": "test", "status": "pending"}]
    assert [todo["status"] for todo in translate.todos(plan)["todos"]] == ["completed", "in_progress", "pending"]
    assert translate.todos(plan)["todos"][1] == {"content": "write", "activeForm": "write", "status": "in_progress"}


# -- approvals --------------------------------------------------------------------
def test_an_approval_is_accepted_declined_or_cancelled():
    command = {"method": "item/commandExecution/requestApproval", "params": {"availableDecisions": ["accept", "cancel"]}}
    assert translate.approval_answer(command, True) == {"decision": "accept"}
    assert translate.approval_answer(command, False) == {"decision": "cancel"}  # no decline on offer
    patch = {"method": "item/fileChange/requestApproval", "params": {}}
    assert translate.approval_answer(patch, False) == {"decision": "decline"}
    offered = {"method": "item/commandExecution/requestApproval", "params": {"availableDecisions": ["accept", "decline"]}}
    assert translate.approval_answer(offered, False) == {"decision": "decline"}


def test_a_permissions_request_is_granted_as_asked_or_not_at_all():
    request = {"method": "item/permissions/requestApproval",
               "params": {"permissions": {"network": {"enabled": True}, "fileSystem": None}}}
    assert translate.approval_answer(request, True) == {"permissions": {"network": {"enabled": True}}, "scope": "turn"}
    assert translate.approval_answer(request, False) == {"permissions": {}, "scope": "turn"}


def test_questions_go_out_as_ask_user_question_and_come_back_by_id():
    questions = [
        {"id": "q1", "header": "Stack", "question": "Which stack?", "isOther": False, "isSecret": False,
         "options": [{"label": "Go", "description": "fast builds"}, {"label": "Rust", "description": "slow builds"}]},
        {"id": "q2", "header": "Name", "question": "What name?", "isOther": True, "isSecret": False, "options": None},
    ]
    asked = translate.question_prompt(questions)["questions"]
    assert asked[0] == {"question": "Which stack?", "header": "Stack", "multiSelect": False, "options": [
        {"label": "Go", "description": "fast builds"}, {"label": "Rust", "description": "slow builds"}]}
    assert asked[1]["options"] == []
    answered = translate.question_answers(questions, {"Which stack?": "Go", "What name?": ["a", "b"]})
    assert answered == {"answers": {"q1": {"answers": ["Go"]}, "q2": {"answers": ["a", "b"]}}}
    assert translate.question_answers(questions, {}) == {"answers": {"q1": {"answers": []}, "q2": {"answers": []}}}


# -- usage ------------------------------------------------------------------------
def test_a_turns_usage_is_the_growth_of_the_threads_total():
    before = {"total": {"inputTokens": 100, "cachedInputTokens": 60, "cacheWriteInputTokens": 0, "outputTokens": 10}}
    after = {"total": {"inputTokens": 350, "cachedInputTokens": 260, "cacheWriteInputTokens": 5, "outputTokens": 40}}
    assert translate.result_usage(before, after) == {
        "input_tokens": 50, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 5, "output_tokens": 30}
    assert translate.result_usage(None, None)["output_tokens"] == 0


def test_context_usage_has_every_key_the_app_reads():
    usage = {"last": {"totalTokens": 25_000}, "modelContextWindow": 100_000}
    answer = translate.context_usage(usage, "gpt-x")
    assert (answer["totalTokens"], answer["maxTokens"], answer["percentage"], answer["model"]) == (25_000, 100_000, 25, "gpt-x")
    assert [(c["id"], c["tokens"]) for c in answer["categories"]] == [("messages", 25_000), ("free_space", 75_000)]
    # The app's context panel breaks when one of Claude Code's keys is missing.
    assert {"gridRows", "agents", "mcpTools", "memoryFiles", "skills", "slashCommands", "messageBreakdown",
            "apiUsage", "autoCompactThreshold", "isAutoCompactEnabled", "rawMaxTokens"} <= set(answer)
    empty = translate.context_usage(None, "gpt-x")
    assert (empty["totalTokens"], empty["maxTokens"], empty["percentage"]) == (0, 0, 0)
