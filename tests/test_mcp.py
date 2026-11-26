"""The tool Codex is given for putting a file in front of the user."""
import io
import json

import pytest
from claude_rc import APIError

from codex_remote_control import background, cli, mcp

THREAD = "thr-1"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)  # Codex starts the server in the thread's directory


@pytest.fixture
def served():
    """A bridge is serving the thread."""
    record = background.claim(THREAD)
    yield
    record.close()


class Store:
    """claude.ai's file store: takes what it is given, and refuses a file named `refused`."""

    def __init__(self):
        self.taken = []

    def upload(self, name, data):
        if name == "refused":
            raise APIError(413, '{"error": {"details": {"max_bytes": 3}}}')
        self.taken.append((name, data))
        kind = "image" if name.endswith(".png") else "blob"
        return {"file_uuid": f"f-{len(self.taken)}", "file_kind": kind, "media_type": "image/png" if kind == "image" else "text/markdown"}


def test_codex_is_offered_one_tool_which_it_may_run_without_asking():
    hello = mcp.answer("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, None)["result"]
    assert hello["protocolVersion"] == "2025-06-18" and hello["capabilities"] == {"tools": {}}
    assert hello["serverInfo"]["name"] == "codex-rc"
    [tool] = mcp.answer("tools/list", {}, None)["result"]["tools"]
    assert tool["name"] == "send_file" and tool["inputSchema"]["required"] == ["files"]
    assert tool["annotations"] == {"readOnlyHint": True}  # what Codex asks about before running, it asks by this
    assert mcp.answer("ping", {}, None) == {"result": {}}


def test_what_the_server_does_not_do_is_an_error_not_silence():
    assert mcp.answer("resources/list", {}, None)["error"]["code"] == -32601  # Codex asks every server
    assert "other" in mcp.answer("tools/call", {"name": "other"}, None)["error"]["message"]


def test_files_are_uploaded_and_reported_as_send_user_file_reports_them(served, tmp_path):
    (tmp_path / "plot.png").write_bytes(b"\x89PNG")
    (tmp_path / "notes.md").write_text("# hi")
    store = Store()
    call = {"name": "send_file", "arguments": {"files": ["plot.png", str(tmp_path / "notes.md")], "caption": "Two."},
            "_meta": {"threadId": THREAD}}
    result = mcp.answer("tools/call", call, store.upload)["result"]
    assert store.taken == [("plot.png", b"\x89PNG"), ("notes.md", b"# hi")]
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": f"Sent to the user: plot.png\nSent to the user: {tmp_path / 'notes.md'}"}]
    assert result["structuredContent"] == {"caption": "Two.", "attachments": [
        {"path": str(tmp_path / "plot.png"), "size": 4, "isImage": True, "media_type": "image/png", "file_uuid": "f-1"},
        {"path": str(tmp_path / "notes.md"), "size": 4, "isImage": False, "media_type": "text/markdown", "file_uuid": "f-2"},
    ]}


def test_what_cannot_be_sent_is_said_and_the_rest_goes(served, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_SEND_BYTES", 8)
    for name, data in (("ok.md", b"fine"), ("long.md", b"123456789"), ("refused", b"x")):
        (tmp_path / name).write_bytes(data)
    (tmp_path / "folder").mkdir()
    store = Store()
    result = mcp.send_file({"files": ["missing.md", "folder", "long.md", "refused", "ok.md"]}, THREAD, store.upload)
    assert result["content"][0]["text"].splitlines() == [
        "Not sent: missing.md (not a file)",
        "Not sent: folder (not a file)",
        "Not sent: long.md (it is over the 0 MiB that can be sent)",
        'Not sent: refused (claude.ai refused it (413): {"error": {"details": {"max_bytes": 3}}})',
        "Sent to the user: ok.md",
    ]
    assert result["isError"] is False and [a["file_uuid"] for a in result["structuredContent"]["attachments"]] == ["f-1"]
    assert "caption" not in result["structuredContent"]
    nothing = mcp.send_file({"files": ["missing.md"]}, THREAD, store.upload)
    assert nothing["isError"] is True and "structuredContent" not in nothing  # Codex ends such a call as failed
    assert mcp.send_file({}, THREAD, store.upload)["content"][0]["text"] == "No file was named."


def test_nothing_is_uploaded_for_a_thread_off_remote_control(tmp_path):
    (tmp_path / "plot.png").write_bytes(b"\x89PNG")
    store = Store()
    for thread in (THREAD, None):  # no bridge on it, or a call that names no thread
        result = mcp.send_file({"files": ["plot.png"]}, thread, store.upload)
        assert result["isError"] is True and "!codex-rc" in result["content"][0]["text"]
    assert store.taken == []


def test_the_server_answers_each_request_on_a_line_and_nothing_else():
    asked = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "not json",
        {"jsonrpc": "2.0", "id": "b", "method": "tools/list"},
    ]
    stdin = io.StringIO("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in asked))
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    answers = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [(a["jsonrpc"], a["id"]) for a in answers] == [("2.0", 0), ("2.0", "b")]
    assert answers[1]["result"]["tools"][0]["name"] == "send_file"


def test_the_command_is_the_server_codex_runs(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)  # it serves no thread, whatever session it is run in
    monkeypatch.setattr(cli.mcp, "serve", lambda: 7)
    assert cli.main(["--mcp"]) == 7
