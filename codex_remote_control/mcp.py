"""The tool Codex is given: `send_file`, which puts a file in front of the user.

A thread on Remote Control is followed from somewhere else, a phone or a
browser, where a path on this machine opens nothing. Claude Code has
`SendUserFile` for that. This is the same for Codex, as an MCP server it runs
(`codex mcp add codex-rc -- codex-rc --mcp`): a tool of its own is what Codex
takes from outside, and its model then finds `send_file` by itself.

The tool uploads the file to the account's store, as Claude Code's does, and
answers with what it sent. Codex keeps that answer with the call, in the
thread, and the bridge writes the call as a `SendUserFile` wherever it meets
one (`translate.sent`). So a file card reaches the transcript by the road
everything else takes, and a thread read again later still has it.

MCP over stdio is JSON-RPC, one message to a line. Checked against Codex
0.172.0, which starts a server for each thread, in the thread's directory and
outside its sandbox, and names the thread in every call's `_meta`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import httpx
from claude_rc import APIError, CredentialsError, RemoteControlClient

from . import __version__, background

# The name Codex has to know the server by, and the tool's: the bridge tells
# a call of it by the two.
SERVER = "codex-rc"
TOOL = "send_file"
# The largest file sent. claude.ai's own limit is not published, and is never
# under this.
_SEND_BYTES = 30 * 1024 * 1024

_NOT_SERVED = (
    "This session is not on Remote Control, so there is nowhere to send a file. "
    "The user puts it there by typing !codex-rc."
)
_TOOLS = [{
    "name": TOOL,
    "title": "Send files to the user",
    "description": (
        "Send files to the user, who may be following this session from the Claude app on a phone or in a "
        "browser, where a path on this machine opens nothing. Use it for a file they would want to see or "
        "keep: a report, a plot, a screenshot, a built artifact. Each file shows there as a card to open or "
        "download. Works while the session is on Remote Control, and says so when it is not."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array", "items": {"type": "string"}, "minItems": 1,
                "description": "The files' paths, absolute or from the working directory.",
            },
            "caption": {"type": "string", "description": "A line shown above the files, where one helps."},
        },
        "required": ["files"],
    },
    # It changes nothing on this machine. Codex runs such a tool without
    # asking, as it reads a file without asking, in every permission mode.
    "annotations": {"readOnlyHint": True},
}]


def _why(error: Exception) -> str:
    if isinstance(error, APIError):
        return f"claude.ai refused it ({error.status}): {str(error.body)[:200]}"
    if isinstance(error, CredentialsError):
        return "Claude Code is not signed in on this machine"
    if isinstance(error, httpx.HTTPError):
        return f"claude.ai could not be reached ({error or type(error).__name__})"
    return getattr(error, "strerror", None) or str(error)


def send_file(arguments: dict, thread_id: str | None, upload: Callable[[str, bytes], dict]) -> dict:
    """What a `send_file` call answers. Each file goes through ``upload``
    (its name and bytes, for claude.ai's answer). The model is told which
    were sent and why any was not. What the bridge needs is the structured
    content: `SendUserFile`'s own result, the caption and each file with the
    id of its upload. A call that sent nothing is an error.

    Nothing is uploaded for a thread no bridge is serving: there would be no
    transcript to show it in."""
    if thread_id is None or background.running(thread_id) is None:
        return {"content": [{"type": "text", "text": _NOT_SERVED}], "isError": True}
    sent, lines = [], []
    for name in arguments.get("files") or []:
        path = Path(name).expanduser().resolve()
        try:
            if not path.is_file():
                raise OSError("not a file")
            size = path.stat().st_size
            if size > _SEND_BYTES:
                raise OSError(f"it is over the {_SEND_BYTES >> 20} MiB that can be sent")
            answer = upload(path.name, path.read_bytes())
        except (OSError, APIError, CredentialsError, httpx.HTTPError) as e:
            lines.append(f"Not sent: {name} ({_why(e)})")
            continue
        sent.append({
            "path": str(path), "size": size, "isImage": answer.get("file_kind") == "image",
            "media_type": answer.get("media_type"), "file_uuid": answer["file_uuid"],
        })
        lines.append(f"Sent to the user: {name}")
    result: dict = {"content": [{"type": "text", "text": "\n".join(lines) or "No file was named."}], "isError": not sent}
    if sent:
        caption = arguments.get("caption")
        result["structuredContent"] = {**({"caption": caption} if caption else {}), "attachments": sent}
    return result


def answer(method: str, params: dict, upload: Callable[[str, bytes], dict]) -> dict:
    """The JSON-RPC answer to one of Codex's requests: a `result`, or an
    `error` for what this server does not do."""
    if method == "initialize":
        return {"result": {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": __version__},
        }}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": _TOOLS}}
    if method == "tools/call" and params.get("name") == TOOL:
        thread_id = (params.get("_meta") or {}).get("threadId")
        return {"result": send_file(params.get("arguments") or {}, thread_id, upload)}
    return {"error": {"code": -32601, "message": f"codex-rc has no {method} {params.get('name') or ''}".rstrip()}}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    """Serve Codex until it closes the pipe."""
    clients: list[RemoteControlClient] = []  # the account's login, read once a file is sent

    def upload(name: str, data: bytes) -> dict:
        if not clients:
            clients.append(RemoteControlClient())
        return clients[0].upload_file(name, data)

    for line in stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message or "method" not in message:
            continue  # a notification: nothing to answer
        reply = answer(message["method"], message.get("params") or {}, upload)
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], **reply}) + "\n")
        stdout.flush()
    return 0
