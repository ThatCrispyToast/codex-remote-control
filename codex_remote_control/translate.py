"""Codex's vocabulary in Claude Code's.

claude.ai/code renders a Claude Code transcript: `assistant` messages made of
text, thinking and `tool_use` blocks, `user` messages carrying `tool_result`s,
permission prompts, a permission mode. These functions restate what Codex's
app-server reports in those terms. They are pure: the bridge does the I/O.

Tool calls take the names of Claude Code's own tools (`Bash`, `Write`, `Edit`,
`WebSearch`, `TodoWrite`) because the app draws those properly: a command as a
command, an edit as a diff, a plan as a checklist.
"""

from __future__ import annotations

import shlex

from claude_rc import Event, worker_assistant, worker_tool_result

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
def user_input(message: Event) -> list[dict]:
    """A controller's `user` message as Codex turn input: its text and images."""
    content = message.payload.get("message", {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    out = []
    for block in blocks:
        if block.get("type") == "text":
            out.append({"type": "text", "text": block["text"], "text_elements": []})
        elif block.get("type") == "image" and block.get("source", {}).get("type") == "base64":
            source = block["source"]
            out.append({"type": "image", "url": f"data:{source['media_type']};base64,{source['data']}"})
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


def tool_calls(item: dict) -> list[dict]:
    """The `tool_use` blocks a thread item amounts to: one for a command, an
    MCP call or a web search, one per file for a patch, none for anything that
    isn't a tool call. The first block's id is the item's id."""
    kind = item["type"]
    if kind == "commandExecution":
        calls = [("Bash", {"command": shell_script(item["command"])})]
    elif kind == "fileChange":
        calls = [_file_change(change) for change in item["changes"]]
    elif kind == "mcpToolCall":
        calls = [(f"mcp__{item['server']}__{item['tool']}", item.get("arguments") or {})]
    elif kind == "webSearch":
        calls = [("WebSearch", {"query": item.get("query") or ""})]
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
    return "", failed


def transcript(item: dict, model: str) -> list[dict]:
    """A finished thread item as the transcript events it amounts to: a
    message, a thought, or a tool call followed by its result. Nothing for the
    kinds of item the app has no way to show."""
    kind = item["type"]
    if kind == "userMessage":
        text = "\n".join(part["text"] for part in item["content"] if part.get("type") == "text")
        return [{"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": text}}]
    if kind == "agentMessage":
        if not item["text"]:
            return []
        return [worker_assistant(item["text"], model=model, message_id=item["id"], stop_reason="end_turn")]
    if kind == "plan":
        return [worker_assistant(item["text"], model=model)]
    if kind == "reasoning":
        summary = "\n\n".join(item.get("summary") or [])
        return [worker_assistant([{"type": "thinking", "thinking": summary, "signature": ""}], model=model)] if summary else []
    calls = tool_calls(item)
    if not calls:
        return []
    output, failed = tool_output(item)
    return [worker_assistant(calls, model=model), *(worker_tool_result(c["id"], output, is_error=failed) for c in calls)]


def todos(plan: list[dict]) -> dict:
    """A Codex plan as `TodoWrite` input, which the app shows as a checklist."""
    status = {"inProgress": "in_progress"}
    return {"todos": [
        {"content": step["step"], "activeForm": step["step"], "status": status.get(step["status"], step["status"])}
        for step in plan
    ]}


# -- approvals as permission prompts ------------------------------------------
def approval_answer(request: dict, allow: bool) -> dict:
    """The result for one of the app-server's approval requests. A refusal is
    `decline` (the turn goes on without it) where Codex offers that, else
    `cancel`."""
    if request["method"] == "item/permissions/requestApproval":
        granted = {k: v for k, v in request["params"]["permissions"].items() if v} if allow else {}
        return {"permissions": granted, "scope": "turn"}
    if allow:
        return {"decision": "accept"}
    offered = request["params"].get("availableDecisions")
    return {"decision": "decline" if offered is None or "decline" in offered else "cancel"}


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
