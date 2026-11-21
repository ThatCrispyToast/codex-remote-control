"""The command: which session a thread gets, and what it is called."""
import pytest

from codex_remote_control import cli

THREAD = {"id": "thr-1", "cwd": "/home/me/project", "name": None}


class Account:
    """The sessions side of claude_rc's client, with one session or none."""

    def __init__(self, title=None):
        self.existing = [] if title is None else [{"id": "cse_old", "title": title}]
        self.calls = []

    def sessions(self, **filters):
        self.calls.append(("sessions", filters))
        return self.existing

    def rename_session(self, session_id, title):
        self.calls.append(("rename", session_id, title))

    def create_session(self, title, **fields):
        self.calls.append(("create", title, fields))
        return {"id": "cse_new"}


def test_a_thread_never_served_gets_a_session_titled_after_it():
    account = Account()
    assert cli.open_session(account, THREAD, None) == ("cse_new", "Codex: project", True, True)
    assert account.calls == [
        ("sessions", {"tags": "codex-thread:thr-1", "statuses": "active"}),
        ("create", "Codex: project", {
            "cwd": "/home/me/project", "tags": ["codex-thread:thr-1"], "repository": None, "branch": None}),
    ]


def test_a_session_is_told_the_repository_its_thread_works_in(monkeypatch):
    """Without one the app keeps its Changes panel shut."""
    monkeypatch.setattr(cli.workspace, "repository", lambda cwd: ("me/project", "main"))
    account = Account()
    cli.open_session(account, THREAD, None)
    assert account.calls[-1][2]["repository"] == "me/project" and account.calls[-1][2]["branch"] == "main"


def test_a_name_given_is_the_title_and_stops_following_the_thread():
    account = Account()
    assert cli.open_session(account, THREAD, "My Project") == ("cse_new", "My Project", True, False)
    assert account.calls[-1][1] == "My Project"


def test_a_thread_served_before_gets_its_session_again_retitled_after_the_thread():
    account = Account("Codex: project")
    named = {**THREAD, "name": "fix the build"}
    assert cli.open_session(account, named, None) == ("cse_old", "Codex: fix the build", False, True)
    assert account.calls[-1] == ("rename", "cse_old", "Codex: fix the build")


def test_a_session_keeps_the_name_it_was_given_until_it_is_given_another():
    account = Account("My Project")
    assert cli.open_session(account, {**THREAD, "name": "fix the build"}, None) == ("cse_old", "My Project", False, False)
    assert [call[0] for call in account.calls] == ["sessions"]  # nothing renamed
    assert cli.open_session(account, THREAD, "Other") == ("cse_old", "Other", False, False)
    assert account.calls[-1] == ("rename", "cse_old", "Other")


class Codex:
    """The app-server, with some threads to read and resume."""

    def __init__(self, *threads):
        self.threads = {thread["id"]: thread for thread in threads}

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, method, params):
        return {"thread": self.threads[params["threadId"]]}


MAIN = {"id": "thr-main", "attachedToThreadId": None}
SIDE = {"id": "thr-side", "attachedToThreadId": "thr-main"}
SIDE_OF_SIDE = {"id": "thr-side-2", "attachedToThreadId": "thr-side"}


def typed_in(thread_id, monkeypatch, *threads):
    """Run `!codex-rc` typed in a thread's session: which thread goes on Remote Control."""
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    monkeypatch.setattr(cli, "AppServer", lambda remote=None: Codex(*threads))
    served = []
    monkeypatch.setattr(cli, "serve_from_background", lambda thread, args: served.append(thread) or 0)
    cli.main([])
    return served[0]


def test_typed_in_the_main_thread_it_serves_that_thread(monkeypatch, capsys):
    assert typed_in("thr-main", monkeypatch, MAIN, SIDE) == "thr-main"
    assert capsys.readouterr().out == ""


def test_typed_in_a_side_conversation_it_serves_the_thread_it_belongs_to(monkeypatch, capsys):
    """Serving the side conversation would serve the thread as it stood when that began."""
    assert typed_in("thr-side", monkeypatch, MAIN, SIDE) == "thr-main"
    assert typed_in("thr-side-2", monkeypatch, MAIN, SIDE, SIDE_OF_SIDE) == "thr-main"
    assert "side conversation" in capsys.readouterr().out


def test_when_codex_cannot_say_the_thread_typed_in_is_served(monkeypatch):
    """Serving it then reports what is wrong, as it always has."""

    class Gone(Codex):
        def __init__(self, remote=None):
            super().__init__()

        def connect(self):
            raise ConnectionRefusedError("no daemon")

    monkeypatch.setenv("CODEX_THREAD_ID", "thr-side")
    monkeypatch.setattr(cli, "AppServer", Gone)
    monkeypatch.setattr(cli, "serve_from_background", lambda thread, args: thread)
    assert cli.main([]) == "thr-side"


def test_typed_in_a_temporary_side_conversation_it_says_where_to_type_it(monkeypatch):
    """Codex keeps no history of a temporary side conversation, nor which thread it came from."""
    with pytest.raises(SystemExit) as stopped:
        typed_in("thr-temp", monkeypatch, {"id": "thr-temp", "ephemeral": True})
    assert "type !codex-rc in the main thread" in str(stopped.value)


def test_a_thread_id_given_as_a_name_is_caught(monkeypatch, capsys):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["01234567-89ab-7cde-8f01-23456789abcd"])
    assert "use --thread 01234567-89ab-7cde-8f01-23456789abcd" in capsys.readouterr().err


def test_what_stops_the_bridge_is_said_in_a_line():
    from claude_rc import APIError, CredentialsError

    assert "sign in with /login" in cli.explain(CredentialsError("no credentials"))
    assert cli.explain(FileNotFoundError(2, "No such file", "codex")).startswith("The codex command was not found")
    refused = cli.explain(cli.subprocess.CalledProcessError(2, ["codex"], stderr="error: unrecognized subcommand 'daemon'\n"))
    assert "unrecognized subcommand 'daemon'" in refused and "0.170 or later" in refused
    assert cli.explain(APIError(403, '{"error": "nope"}', None)) == 'claude.ai refused (403): {"error": "nope"}'
    assert cli.explain(cli.httpx.ConnectError("no route")) == "Could not reach claude.ai: no route"
    assert cli.explain(ConnectionRefusedError("refused")) == "Could not reach Codex's app-server: refused"
    assert "could not read" in cli.explain(ValueError("not JSON"))


class Starts:
    """The app-server as a new thread meets it: started, then perhaps named."""

    def __init__(self):
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        return {"thread": {"id": "thr-new", "cwd": params.get("cwd"), "name": None}} if method == "thread/start" else {}


def new_thread(*argv):
    codex = Starts()
    args = cli.argparse.Namespace(thread=None, last=False, cd="/home/me/project", model=None, name=None)
    for key, value in argv:
        setattr(args, key, value)
    return codex, cli.open_thread(codex, args)


def test_a_new_thread_given_a_name_takes_it_in_codex_which_lets_a_tui_join_it():
    """Naming writes the thread to disk, and `codex resume <id>` finds "no rollout" until then."""
    codex, opened = new_thread(("name", "Agent"), ("model", "gpt-x"))
    assert codex.requests == [
        ("thread/start", {"cwd": "/home/me/project", "model": "gpt-x"}),
        ("thread/name/set", {"threadId": "thr-new", "name": "Agent"}),
    ]
    assert opened["thread"]["name"] == "Agent"
    codex, opened = new_thread()
    assert [method for method, _ in codex.requests] == ["thread/start"]  # left for Codex to name


def test_the_worker_keeps_the_record_in_step_with_the_state_it_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    reported = []
    monkeypatch.setattr(cli.RemoteControlWorker, "report_state", lambda self, status, **kw: reported.append(status))
    record = cli.background.claim("thr-1")
    try:
        worker = cli.RecordedWorker(record, None, "cse_1")
        assert cli.background.running("thr-1").get("state") is None
        for status in ("idle", "running", "requires_action"):
            worker.report_state(status)
            assert cli.background.running("thr-1")["state"] == status
        assert reported == ["idle", "running", "requires_action"]
        assert cli.background.running("thr-1")["started"] > 0
    finally:
        record.close()


class Serves:
    """The app-server as `codex-rc NAME` meets it from a shell: a new thread, named."""

    def __init__(self, remote=None):
        self.remote, self.requests = remote, []

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "01234567-89ab-7cde-8f01-23456789abcd", "cwd": params["cwd"], "name": None},
                    "cwd": params["cwd"]}
        return {}


def test_the_thread_comes_first_and_before_claude_ai_is_asked(tmp_path, monkeypatch, capsys):
    """A wrapper reads the thread from the first line, so it must be out, flushed,
    even when claude.ai never answers."""
    from claude_rc import CredentialsError

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_RC_REMOTE", raising=False)
    codex = []
    monkeypatch.setattr(cli, "AppServer", lambda remote=None: codex.append(Serves(remote)) or codex[-1])

    def no_login():
        raise CredentialsError("no credentials")

    monkeypatch.setattr(cli, "RemoteControlClient", no_login)
    assert cli.main(["--remote", "unix:///run/agent/app-server.sock", "-C", str(tmp_path), "Agent"]) == 1
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"thread   01234567-89ab-7cde-8f01-23456789abcd  ({tmp_path})"
    assert codex[0].remote == "unix:///run/agent/app-server.sock"
    assert [method for method, _ in codex[0].requests] == ["thread/start", "thread/name/set"]
    assert not list((tmp_path / "codex-remote-control").glob("*.json"))  # the record went with it
