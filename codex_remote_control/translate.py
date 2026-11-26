"""Codex's vocabulary in Claude Code's.

claude.ai/code renders a Claude Code transcript: `assistant` messages made of
text, thinking and `tool_use` blocks, `user` messages carrying `tool_result`s,
permission prompts, a permission mode. These functions restate what Codex's
app-server reports in those terms. They are pure: the bridge does the I/O.

Tool calls take the names of Claude Code's own tools (`Bash`, `Write`, `Edit`,
`WebSearch`, `TodoWrite`, `SendUserFile`) because the app draws those
properly: a command as a command, an edit as a diff, a plan as a checklist, a
file sent as a card.
"""

from __future__ import annotations

import json
import shlex
import time

from claude_rc import (
    Event,
    worker_assistant,
    worker_compact_boundary,
    worker_status,
    worker_tool_result,
)

from . import mcp

# A tool's output past this many characters is cut: the app is for following
# along, and one event has a size limit.
_OUTPUT_LIMIT = 30_000

# Claude's permission modes as Codex settings: when to ask (the approval
# policy), what may be touched (the sandbox), and who answers (the reviewer).
# Each is one of Codex's own presets, named beside it. Plan mode is a separate
# switch in Codex, its collaboration mode, and leaves the rest alone.
MODES = {
    "default": ("on-request", "readOnly", "user"),             # Read Only: asks before it changes anything
    "acceptEdits": ("on-request", "workspaceWrite", "user"),   # Auto: works freely inside the workspace
    "auto": ("on-request", "workspaceWrite", "auto_review"),   # --approve-for-me: a reviewer agent answers
    "bypassPermissions": ("never", "dangerFullAccess", "user"),  # Full Access
    "plan": None,
}
_SANDBOXES = {
    "readOnly": {"type": "readOnly", "networkAccess": False},
    "workspaceWrite": {
        "type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
        "excludeTmpdirEnvVar": False, "excludeSlashTmp": False,
    },
    "dangerFullAccess": {"type": "dangerFullAccess"},
}


def permission_mode(settings: dict) -> str:
    """The Claude permission mode nearest a thread's settings (as
    `thread/settings/updated` reports them), which the app shows as the
    session's mode."""
    if (settings.get("collaborationMode") or {}).get("mode") == "plan":
        return "plan"
    approval, sandbox = settings["approvalPolicy"], settings["sandboxPolicy"]["type"]
    if sandbox == "dangerFullAccess" and approval == "never":
        return "bypassPermissions"
    if settings.get("approvalsReviewer", "user") != "user":
        return "auto"
    return "default" if sandbox == "readOnly" or approval == "untrusted" else "acceptEdits"


def mode_settings(mode: str, settings: dict) -> dict:
    """The `thread/settings/update` fields that put a thread in a Claude
    permission mode, given its current settings."""
    collaboration = {
        "mode": "plan" if mode == "plan" else "default",
        "settings": {
            "model": settings["model"], "reasoning_effort": settings.get("effort"), "developer_instructions": None,
        },
    }
    if mode == "plan":
        return {"collaborationMode": collaboration}
    approval, sandbox, reviewer = MODES[mode]
    return {
        "approvalPolicy": approval, "sandboxPolicy": _SANDBOXES[sandbox], "approvalsReviewer": reviewer,
        "collaborationMode": collaboration,
    }


# -- what the user sends ------------------------------------------------------
def user_input(message: Event, attached: list[dict] | None = None) -> list[dict]:
    """A controller's `user` message as Codex turn input: its text, its
    images, and the files the app attached to it. The app attaches by
    reference and the bridge fetches: ``attached`` has one entry per file,
    ``{name, path, image}`` once saved, ``{name}`` alone if it could not be."""
    content = message.payload.get("message", {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    out = []
    for block in blocks:
        if block.get("type") == "text":
            out.append({"type": "text", "text": block["text"], "text_elements": []})
        elif block.get("type") == "image" and block.get("source", {}).get("type") == "base64":
            source = block["source"]
            out.append({"type": "image", "url": f"data:{source['media_type']};base64,{source['data']}"})
    notes = []
    for file in attached or []:
        if file.get("path") and file.get("image"):
            out.append({"type": "localImage", "path": file["path"]})
        elif file.get("path"):
            notes.append(f"[Attached file: {file['path']}]")
        else:
            notes.append(f"[An attached file, {file['name']}, could not be fetched]")
    if notes:
        out.append({"type": "text", "text": "\n".join(notes), "text_elements": []})
    return out


# -- thread items as tool calls -----------------------------------------------
def shell_script(command: str) -> str:
    """The script inside Codex's `bash -lc '…'` wrapper: what a person would
    call the command."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    return argv[2] if len(argv) == 3 and argv[1] in ("-lc", "-c") else command


def _diff_sides(diff: str) -> tuple[str, str]:
    """The before and after text a unified diff's hunks cover, hunks set apart
    by an ellipsis line. Shown as an `Edit`, it renders as that diff again."""
    old: list[str] = []
    new: list[str] = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            if old or new:
                old.append("…")
                new.append("…")
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
        elif not line.startswith("\\"):  # "\ No newline at end of file"
            old.append(line[1:])
            new.append(line[1:])
    return "\n".join(old), "\n".join(new)


def _file_change(change: dict) -> tuple[str, dict]:
    kind = change["kind"]["type"]
    if kind == "add":  # the "diff" of an added file is its content
        return "Write", {"file_path": change["path"], "content": change["diff"]}
    if kind == "delete":
        return "Edit", {"file_path": change["path"], "old_string": change["diff"], "new_string": ""}
    old, new = _diff_sides(change["diff"])
    return "Edit", {"file_path": change["path"], "old_string": old, "new_string": new}


def sends(item: dict) -> bool:
    """Whether a thread item is a call of codex-rc's own tool, `send_file`,
    which is Claude Code's `SendUserFile` in all but name: it takes the same
    input and answers with the same result (see mcp.py)."""
    return item["type"] == "mcpToolCall" and (item["server"], item["tool"]) == (mcp.SERVER, mcp.TOOL)


def sent(item: dict) -> dict | None:
    """What a `send_file` call delivered, as `SendUserFile` reports it: the
    caption and the files, each with the id of its upload. It goes beside the
    call's result, and the app draws its file cards from it. None for any
    other item, and for a call that sent nothing."""
    delivered = (item.get("result") or {}).get("structuredContent") if sends(item) else None
    return delivered if isinstance(delivered, dict) and delivered.get("attachments") else None


def tool_calls(item: dict) -> list[dict]:
    """The `tool_use` blocks a thread item amounts to: one for a command, an
    MCP call or a web search, one per file for a patch, none for anything that
    isn't a tool call. The first block's id is the item's id."""
    kind = item["type"]
    if kind == "commandExecution":
        calls = [("Bash", {"command": shell_script(item["command"])})]
    elif kind == "fileChange":
        calls = [_file_change(change) for change in item["changes"]]
    elif sends(item):
        calls = [("SendUserFile", item.get("arguments") or {})]
    elif kind == "mcpToolCall":
        calls = [(f"mcp__{item['server']}__{item['tool']}", item.get("arguments") or {})]
    elif kind == "webSearch":
        calls = [("WebSearch", {"query": item.get("query") or ""})]
    elif kind == "dynamicToolCall":
        calls = [("__".join(filter(None, (item.get("namespace"), item["tool"]))), item.get("arguments") or {})]
    elif kind == "imageView":
        calls = [("Read", {"file_path": item["path"]})]
    elif kind == "collabAgentToolCall" and item["tool"] == "spawnAgent":
        prompt = item.get("prompt") or ""
        calls = [("Agent", {"description": prompt.strip().split("\n")[0][:80], "prompt": prompt, "subagent_type": "codex"})]
    else:
        return []
    return [
        {"type": "tool_use", "id": item["id"] if n == 0 else f"{item['id']}/{n}", "name": name, "input": tool_input}
        for n, (name, tool_input) in enumerate(calls)
    ]


def tool_output(item: dict) -> tuple[str, bool]:
    """A finished tool item's result text, and whether it failed."""
    kind = item["type"]
    failed = item.get("status") in ("failed", "declined")
    if kind == "commandExecution":
        if item.get("status") == "declined":
            return "The command was not approved.", True
        output = item.get("aggregatedOutput") or ""
        if len(output) > _OUTPUT_LIMIT:
            output = f"{output[:_OUTPUT_LIMIT]}\n… ({len(output) - _OUTPUT_LIMIT} more characters)"
        return output, failed or item.get("exitCode") not in (0, None)
    if kind == "fileChange":
        return {"completed": "Applied.", "declined": "The change was not approved."}.get(
            item.get("status"), "The change could not be applied."
        ), failed
    if kind == "mcpToolCall":
        if item.get("error"):
            return item["error"]["message"], True
        parts = (item.get("result") or {}).get("content") or []
        return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)), failed
    if kind == "dynamicToolCall":
        parts = item.get("contentItems") or []
        return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)), failed or item.get("success") is False
    if kind == "collabAgentToolCall":
        said = [state.get("message") for state in (item.get("agentsStates") or {}).values()]
        return "\n\n".join(filter(None, said)), failed
    return "", failed


def transcript(item: dict, model: str) -> list[dict]:
    """A finished thread item as the transcript events it amounts to: a
    message, a thought, or a tool call followed by its result. Nothing for the
    kinds of item the app has no way to show."""
    kind = item["type"]
    if kind == "userMessage":
        return [{"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": typed(item["content"])}}]
    if kind == "agentMessage":
        if not item["text"]:
            return []
        stop = "end_turn" if final(item) else None
        return [worker_assistant(item["text"], model=model, message_id=item["id"], stop_reason=stop)]
    if kind == "plan":
        return [worker_assistant(item["text"], model=model)]
    if kind == "reasoning":
        summary = "\n\n".join(item.get("summary") or [])
        thought = [{"type": "thinking", "thinking": summary, "signature": ""}]
        return [worker_assistant(thought, model=model, message_id=item["id"])] if summary else []
    if kind == "enteredReviewMode":
        return [worker_assistant(f"Reviewing {item['review']}.", model=model)] if item.get("review") else []
    if kind == "exitedReviewMode":  # see turn_transcript(): the review is usually a message of the turn too
        return [worker_assistant(item["review"], model=model, stop_reason="end_turn")] if item.get("review") else []
    if kind == "imageGeneration":
        return [worker_assistant(f"Generated an image: `{item['savedPath']}`", model=model)] if item.get("savedPath") else []
    if kind == "contextCompaction":
        return [compact_boundary()]
    calls = tool_calls(item)
    if not calls:
        return []
    output, failed = tool_output(item)
    results = (worker_tool_result(c["id"], output, is_error=failed, tool_use_result=sent(item)) for c in calls)
    return [worker_assistant(calls, model=model), *results]


def final(item: dict) -> bool:
    """Whether an agent message is its turn's answer, and not something said
    on the way to it. The app takes a message that ends the turn as the end
    of the work, and stops showing the session as working. A model that does
    not say which a message is has it taken as said on the way: the turn's
    end follows an answer at once in any case."""
    return item.get("phase") == "final_answer"


def turn_transcript(items: list[dict], model: str) -> list[dict]:
    """A finished turn's items as transcript events. Codex reports a review's
    findings twice, as the item that ends the review and as a message of the
    turn: they are written once."""
    said = {item["text"] for item in items if item["type"] == "agentMessage"}
    return [
        event for item in items
        if not (item["type"] == "exitedReviewMode" and item.get("review") in said)
        for event in transcript(item, model)
    ]


def typed(content: list[dict]) -> str:
    """A Codex user message as the text the transcript shows: what was typed,
    with a line for each thing attached to it."""
    lines = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            replies = question_replies(part["text"])
            lines.append(part["text"] if replies is None else "\n\n".join(f"> {r['question']}\n\n{r['answer']}" for r in replies))
        elif kind in ("image", "localImage"):
            lines.append(f"[image: {part['path']}]" if part.get("path") else "[image]")
        elif kind in ("mention", "skill"):
            lines.append(f"[{part['name']}: {part['path']}]")
    return "\n".join(lines)


def compact_boundary(trigger: str = "auto", before: dict | None = None, after: dict | None = None) -> dict:
    """The mark the app draws where the thread's context was compacted.
    ``before`` and ``after`` are the thread's `tokenUsage` as the compaction
    began and as it ended. With both the app says how much was saved, and
    takes the second for the size of the context from there on."""
    def used(usage: dict | None) -> int | None:
        return ((usage or {}).get("last") or {}).get("totalTokens")

    return worker_compact_boundary(trigger=trigger, pre_tokens=used(before), post_tokens=used(after))


def compaction_over(done: bool) -> dict:
    """How a compaction ended, in Claude Code's words: what takes down the
    "Compacting session" the app has shown since it was told of one."""
    return {**worker_status(None), "compact_result": "success" if done else "failed"}


# -- a message as it is written ---------------------------------------------------
# The Messages API's streaming events, which Claude Code passes on as they come
# and the app draws from: a message's start, its one block (text or a thought)
# growing, and its end.
def stream_start(message_id: str, model: str, thinking: bool) -> list[dict]:
    """The start of a message and of its block. A thought begun is what the
    app writes "Thinking" for, before there is a word of it."""
    message = {
        "id": message_id, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    block = {"type": "thinking", "thinking": "", "signature": ""} if thinking else {"type": "text", "text": ""}
    return [{"type": "message_start", "message": message}, {"type": "content_block_start", "index": 0, "content_block": block}]


def stream_text(text: str, thinking: bool) -> dict:
    """More of the block's text."""
    delta = {"type": "thinking_delta", "thinking": text} if thinking else {"type": "text_delta", "text": text}
    return {"type": "content_block_delta", "index": 0, "delta": delta}


STREAM_BLOCK_END = {"type": "content_block_stop", "index": 0}


def stream_end(answer: bool = False) -> list[dict]:
    """The end of the message. Only the turn's ``answer`` ends as the end of
    a turn: the app stops showing the session as working when it sees one."""
    stop = {"stop_reason": "end_turn" if answer else None, "stop_sequence": None}
    return [{"type": "message_delta", "delta": stop}, {"type": "message_stop"}]


def todos(plan: list[dict]) -> dict:
    """A Codex plan as `TodoWrite` input, which the app shows as a checklist."""
    status = {"inProgress": "in_progress"}
    return {"todos": [
        {"content": step["step"], "activeForm": step["step"], "status": status.get(step["status"], step["status"])}
        for step in plan
    ]}


# -- approvals as permission prompts ------------------------------------------
def session_rule(command: str) -> list[dict]:
    """What makes the app offer "Always allow" on a command's permission
    card: a rule for that command. The card says it lasts for the session
    and covers that command, and an answer that takes it carries the rule
    back as its `updatedPermissions`."""
    rule = {"toolName": "Bash", "ruleContent": command}
    return [{"type": "addRules", "rules": [rule], "behavior": "allow", "destination": "session"}]


def approval_key(params: dict) -> str:
    """What a command approval asks for, as the key an "Always allow" is
    remembered under: the same command, in the same directory, wanting the
    same extra access."""
    asked = (params.get("command"), params.get("cwd"), params.get("additionalPermissions"), params.get("networkApprovalContext"))
    return json.dumps(asked, sort_keys=True)


def approval_answer(request: dict, allow: bool, *, standing: bool = False) -> dict:
    """The result for one of the app-server's approval requests. A refusal is
    `decline` (the turn goes on without it) where Codex offers that, else
    `cancel`. A ``standing`` yes to a command is `acceptForSession` where
    Codex offers that: it then remembers the answer itself. Its other
    standing answer, a rule it writes down for commands like this one,
    outlasts the session, which is more than the app's card promises."""
    if request["method"] == "item/permissions/requestApproval":
        granted = {k: v for k, v in request["params"]["permissions"].items() if v} if allow else {}
        return {"permissions": granted, "scope": "turn"}
    if request["method"] == "mcpServer/elicitation/request":
        return elicitation_answer(request["params"], {} if allow else None)
    offered = request["params"].get("availableDecisions")
    if allow:
        remembered = standing and request["method"] == "item/commandExecution/requestApproval" and "acceptForSession" in (offered or ())
        return {"decision": "acceptForSession" if remembered else "accept"}
    return {"decision": "decline" if offered is None or "decline" in offered else "cancel"}


# -- MCP servers asking the user ------------------------------------------------
_YES_NO = {"Yes": True, "No": False}


def _choices(schema: dict) -> list[tuple[str, str, object]] | None:
    """One form field's choices as (label, description, value), if it is a
    field the app's question card can ask: a yes/no, or a pick of one from
    up to four."""
    if schema.get("type") == "boolean":
        return [(label, "", value) for label, value in _YES_NO.items()]
    if schema.get("type") != "string":
        return None
    if "oneOf" in schema:
        options = [(o.get("title") or o["const"], "", o["const"]) for o in schema["oneOf"]]
    elif "enum" in schema:
        options = [(value, "", value) for value in schema["enum"]]
    else:
        return None
    return options if 2 <= len(options) <= 4 else None


def elicitation_prompt(params: dict) -> tuple[str, dict, str] | None:
    """An MCP server's request to the user (`mcpServer/elicitation/request`)
    as a prompt the app can show: the tool to name, its input, and what is
    being asked. None for the kinds only a Codex terminal can answer.

    A request with nothing to fill in is a yes or no, which is how Codex asks
    whether an MCP tool may run. A link to open is one too. A form made only
    of choices becomes a question card."""
    server, mode = params["serverName"], params.get("mode")
    if mode == "url":
        return f"mcp__{server}", {"url": params["url"]}, f"{params['message']}\n{params['url']}"
    if mode != "form":
        return None
    fields = (params.get("requestedSchema") or {}).get("properties") or {}
    if not fields:
        shown = (params.get("_meta") or {}).get("tool_params") or {}
        return f"mcp__{server}", shown if isinstance(shown, dict) else {}, params["message"]
    if len(fields) > 4 or any(_choices(schema) is None for schema in fields.values()):
        return None
    questions = [
        {
            "question": schema.get("description") or schema.get("title") or name,
            "header": (schema.get("title") or name)[:12],
            "multiSelect": False,
            "options": [{"label": label, "description": text} for label, text, _ in _choices(schema)],
        }
        for name, schema in fields.items()
    ]
    return "AskUserQuestion", {"questions": questions}, params["message"]


def elicitation_answer(params: dict, answers: dict | None) -> dict:
    """The result for an MCP server's request: declined when ``answers`` is
    None, else accepted with the form filled from the app's answers (question
    text to label). A required field left unanswered declines it."""
    if answers is None:
        return {"action": "decline", "content": None, "_meta": None}
    schema = params.get("requestedSchema") or {}
    content = {}
    for name, field in (schema.get("properties") or {}).items():
        picked = answers.get(field.get("description") or field.get("title") or name)
        picked = picked[0] if isinstance(picked, list) and picked else picked
        value = next((value for label, _, value in _choices(field) or [] if label == picked), None)
        if value is not None:
            content[name] = value
        elif name in (schema.get("required") or []):
            return {"action": "decline", "content": None, "_meta": None}
    return {
        "action": "accept",
        "content": content if params.get("mode") == "form" else None,
        "_meta": None,
    }


def question_prompt(questions: list[dict]) -> dict:
    """Codex's `requestUserInput` questions as `AskUserQuestion` input."""
    return {"questions": [
        {
            "question": q["question"],
            "header": q["header"],
            "multiSelect": False,
            "options": [{"label": o["label"], "description": o["description"]} for o in q.get("options") or []],
        }
        for q in questions
    ]}


def question_answers(questions: list[dict], answers: dict) -> dict:
    """The app's answers (question text to label, or labels) as the result
    `requestUserInput` wants (question id to answers). A question left
    unanswered gets none."""
    out = {}
    for q in questions:
        answer = answers.get(q["question"])
        out[q["id"]] = {"answers": [] if answer is None else answer if isinstance(answer, list) else [answer]}
    return {"answers": out}


# -- Codex asking while it works -------------------------------------------------
# What an answer to one of those questions is wrapped in, as Codex writes it.
_REPLY = ("<send_user_message_question_reply>", "</send_user_message_question_reply>")


def async_questions(item: dict) -> list[dict]:
    """The questions an agent message asks with `request_user_input_async`.
    Codex works on while it waits, and takes the answer as a message. Each
    comes with its id, as an answer names it, and the `AskUserQuestion` input
    that puts it to the app, whose card adds a box for any other answer."""
    return [
        {
            "id": json.dumps(["request_user_input_async", item["id"], index], separators=(",", ":")),
            "message": item["id"],
            "question": q["title"],
            "input": {"questions": [{
                "question": q["title"], "header": "Question", "multiSelect": False,
                "options": [{"label": option, "description": ""} for option in q.get("options") or []],
            }]},
        }
        for index, q in enumerate(item.get("questions") or [])
    ]


def question_reply(question_id: str, question: str, answer: str) -> str:
    """The message that answers one of those questions, as Codex's own
    terminal writes it: the question cut to 512 bytes on one line, then the
    answer, wrapped in the markers Codex knows it by."""
    question = question.encode()[:512].decode(errors="ignore").replace("\n", " ").replace("\r", " ")
    if len(question_id) > 512:  # Codex's own fallback: the plain form
        return f"> {question}\n\n{answer}"
    replies = [{"answer": answer, "question": question, "questionItemId": question_id}]
    return f"{_REPLY[0]}\n{json.dumps(replies, ensure_ascii=False, separators=(',', ':'))}\n{_REPLY[1]}"


def question_replies(text: str) -> list[dict] | None:
    """The answers to Codex's questions a message gives, if that is all it
    is: each one's `answer`, `question` and `questionItemId`."""
    text = text.strip()
    if not (text.startswith(_REPLY[0]) and text.endswith(_REPLY[1])):
        return None
    try:
        replies = json.loads(text[len(_REPLY[0]):-len(_REPLY[1])])
    except ValueError:
        return None
    replies = replies if isinstance(replies, list) else [replies]
    fields = ("answer", "question", "questionItemId")
    if not replies or not all(isinstance(r, dict) and all(isinstance(r.get(f), str) for f in fields) for r in replies):
        return None
    return replies


# -- what the session says about itself ------------------------------------------
_MCP_STATUS = {
    "connected": "connected", "authenticationRequired": "needs-auth", "failed": "failed",
    "disabled": "disabled", "cancelled": "failed",
}


def mcp_servers(statuses: list[dict]) -> list[dict]:
    """Codex's MCP servers (`mcpServerStatus/list`) as Claude Code lists its
    own: a name and a status the app knows."""
    return [
        {"name": server["name"], "status": _MCP_STATUS.get(server.get("runtimeStatus"), "pending")}
        for server in statuses
    ]


def side_prompt(question: str, history: list[dict] | None) -> str:
    """A side question as the prompt Codex samples an answer to: the question,
    after what was asked and answered beside the conversation before it."""
    earlier = [
        f"Q: {entry['question']}\nA: {entry['response']}"
        for entry in history or [] if entry.get("question") and entry.get("response")
    ]
    asked = (
        "Answer this side question from the conversation so far, briefly, without using tools. "
        "It is not part of the task and changes nothing about it."
    )
    return "\n\n".join([asked, *earlier, f"Q: {question}"])


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _window(minutes: int | None) -> str:
    if not minutes:
        return "current"
    if minutes % 10080 == 0:
        return "weekly" if minutes == 10080 else f"{minutes // 10080}-week"
    if minutes % 1440 == 0:
        return "daily" if minutes == 1440 else f"{minutes // 1440}-day"
    return f"{minutes // 60}-hour" if minutes % 60 == 0 else f"{minutes}-minute"


def limits(rate_limits: dict | None) -> list[str]:
    """Codex's rate limits (`account/rateLimits/read`) as lines of text."""
    lines = []
    for window in ((rate_limits or {}).get("primary"), (rate_limits or {}).get("secondary")):
        if not window:
            continue
        line = f"{round(window['usedPercent'])}% of the {_window(window.get('windowDurationMins'))} limit used"
        if window.get("resetsAt"):
            line += time.strftime(", resets %b %-d at %H:%M", time.localtime(window["resetsAt"]))
        lines.append(line)
    return lines


def sandbox(settings: dict) -> str:
    """A thread's sandbox and approval settings in a sentence."""
    policy = settings["sandboxPolicy"]
    access = {
        "readOnly": "read-only",
        "workspaceWrite": "can write in the workspace",
        "dangerFullAccess": "full access",
    }.get(policy["type"], policy["type"])
    network = "" if policy["type"] == "dangerFullAccess" else f", network {'on' if policy.get('networkAccess') else 'off'}"
    reviewer = "you" if settings.get("approvalsReviewer", "user") == "user" else "the Codex reviewer"
    asks = {"never": "never asks", "on-request": f"asks {reviewer} when it needs more", "untrusted": f"asks {reviewer} before anything untrusted"}
    return f"Sandbox: {access}{network}. Approvals: {asks.get(settings['approvalPolicy'], settings['approvalPolicy'])}."


def terminals(running: list[dict]) -> str:
    """The background terminals of a thread (`thread/backgroundTerminals/list`)
    as `/ps` answers: each command still running, and where."""
    if not running:
        return "No background terminals are running."
    lines = [f"- `{shell_script(terminal['command'])}` in `{terminal['cwd']}`" for terminal in running]
    return "\n".join(["**Background terminals** (`/stop` ends them)", *lines])


def report(
    *, thread: dict, settings: dict, fast: bool, usage: dict | None, rate_limits: dict | None, models: list[str],
    resume: str,
) -> str:
    """What `/codex` answers: the Codex side of the session, which the app's
    own status and usage cards (they describe the Claude account) leave out."""
    effort = settings.get("effort")
    lines = [
        f"**Codex {thread.get('cliVersion') or ''}**".replace(" **", "**"),
        f"- Thread: `{thread['id']}` in `{settings['cwd']}`",
        f"- Model: `{settings['model']}`" + (f", effort `{effort}`" if effort else "") + (", Fast mode" if fast else ""),
        f"- {sandbox(settings)}",
    ]
    used = ((usage or {}).get("last") or {}).get("totalTokens")
    if used is not None:
        window = (usage or {}).get("modelContextWindow")
        lines.append(f"- Context: {_tokens(used)}" + (f" of {_tokens(window)} tokens ({round(100 * used / window)}%)" if window else " tokens"))
    lines += [f"- Usage: {line}" for line in limits(rate_limits)]
    if plan := (rate_limits or {}).get("planType"):
        lines.append(f"- Plan: {plan}")
    lines.append(f"- Models, for `/model <id>`: {', '.join(f'`{m}`' for m in models)}")
    lines.append(f"- In a terminal: `{resume}`")
    return "\n".join(lines)


# -- usage ----------------------------------------------------------------------
def result_usage(before: dict | None, after: dict | None) -> dict:
    """A turn's token usage in the Messages API's terms, from the thread's
    `tokenUsage` as the turn began and as it ended: the running total's growth."""
    def spent(key: str) -> int:
        return ((after or {}).get("total") or {}).get(key, 0) - ((before or {}).get("total") or {}).get(key, 0)

    cached = spent("cachedInputTokens")
    return {
        "input_tokens": spent("inputTokens") - cached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": spent("cacheWriteInputTokens"),
        "output_tokens": spent("outputTokens"),
    }


def context_usage(usage: dict | None, model: str) -> dict:
    """The answer to the app's `get_context_usage`, from a thread's
    `tokenUsage`. Codex reports how full the context window is, not what fills
    it, so everything used is one category. The app expects every key of
    Claude Code's own answer, so the ones Codex has nothing for are empty."""
    used = ((usage or {}).get("last") or {}).get("totalTokens", 0)
    window = (usage or {}).get("modelContextWindow") or 0
    categories = [{"id": "messages", "name": "Messages", "kind": "used", "color": "claude", "tokens": used}]
    if window:
        free = max(window - used, 0)
        categories.append({"id": "free_space", "name": "Free space", "kind": "free", "color": "promptBorder", "tokens": free})
    return {
        "totalTokens": used,
        "maxTokens": window,
        "rawMaxTokens": window,
        "percentage": round(100 * used / window) if window else 0,
        "model": model,
        "categories": categories,
        "gridRows": [],
        "agents": [],
        "mcpTools": [],
        "memoryFiles": [],
        "skills": {"includedSkills": 0, "totalSkills": 0, "tokens": 0, "skillFrontmatter": []},
        "slashCommands": {"includedCommands": 0, "totalCommands": 0, "tokens": 0},
        "messageBreakdown": {
            "userMessageTokens": 0, "assistantMessageTokens": 0, "toolCallTokens": 0, "toolResultTokens": 0,
            "attachmentTokens": 0, "redirectedContextTokens": 0, "unattributedTokens": used,
            "toolCallsByType": [], "attachmentsByType": [],
        },
        "apiUsage": None,
        "autoCompactThreshold": window,
        "isAutoCompactEnabled": True,
    }
