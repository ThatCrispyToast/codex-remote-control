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
        ("create", "Codex: project", {"cwd": "/home/me/project", "tags": ["codex-thread:thr-1"]}),
    ]


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


def test_a_thread_id_given_as_a_name_is_caught(monkeypatch, capsys):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["01234567-89ab-7cde-8f01-23456789abcd"])
    assert "use --thread 01234567-89ab-7cde-8f01-23456789abcd" in capsys.readouterr().err
