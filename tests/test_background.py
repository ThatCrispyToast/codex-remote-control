"""Background bridges: the record each one holds, and starting and stopping one."""
import sys

import pytest

from codex_remote_control import background, cli

THREAD = "thr-1"

# Stands in for the bridge: claims its thread, reports a session, and waits to be stopped.
BRIDGE = """
import os, signal, sys, time
from codex_remote_control import background
thread, options = sys.argv[1], sys.argv[2:]
record = background.claim(thread)
title = options[options.index("--") + 1] if "--" in options else "Codex: w"
record.update(session="https://claude.ai/code/session_1", title=title, options=options, env=os.environ.get("CODEX_THREAD_ID"))
signal.signal(signal.SIGTERM, lambda *_: (record.close(), sys.exit(0)))
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(background, "_COMMAND", [sys.executable, "-c", BRIDGE])
    yield tmp_path / "codex-remote-control"
    background.stop(THREAD)


def test_a_bridge_holds_its_threads_record_while_it_runs(state):
    assert background.running(THREAD) is None
    record = background.claim(THREAD)
    record.update(session="https://claude.ai/code/session_1")
    assert background.running(THREAD)["session"] == "https://claude.ai/code/session_1"
    assert background.claim(THREAD) is None  # one bridge to a thread
    assert background.running("thr-2") is None
    record.close()
    assert background.running(THREAD) is None
    assert not list(state.iterdir())


def test_a_record_left_behind_is_no_bridge(state):
    state.mkdir()
    (state / f"{THREAD}.json").write_text('{"pid": 1, "session": "https://claude.ai/code/session_0"}')
    assert background.running(THREAD) is None
    assert background.stop(THREAD) is False  # and pid 1 is left alone
    record = background.claim(THREAD)
    assert background.running(THREAD)["pid"] != 1
    record.close()


def test_start_returns_once_the_session_is_up_and_stop_ends_it(state):
    bridge = background.start(THREAD, ["--name", "t"])
    assert bridge["session"] == "https://claude.ai/code/session_1"
    assert bridge["options"] == ["--name", "t"]
    assert background.start(THREAD, [])["pid"] == bridge["pid"]  # already served: the same bridge
    assert background.stop(THREAD) is True
    assert background.running(THREAD) is None
    assert not list(state.iterdir())  # neither record nor log is left
    assert background.stop(THREAD) is False


def test_a_bridge_that_does_not_come_up_says_why(state, monkeypatch):
    monkeypatch.setattr(background, "_COMMAND", [sys.executable, "-c", "import sys; sys.exit('Codex refused: no such thread')"])
    with pytest.raises(background.StartFailed, match="no such thread"):
        background.start(THREAD, [])
    assert not list(state.iterdir())


# -- the command, typed in a Codex session ----------------------------------------
def test_typed_in_a_session_it_serves_that_session_from_the_background(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.startswith('on Remote Control as "Codex: w": https://claude.ai/code/session_1\n')
    bridge = background.running(THREAD)
    assert bridge["env"] is None  # the bridge is not itself "typed in a session"
    assert cli.main([]) == 0  # again: the same bridge, and its link
    assert background.running(THREAD)["pid"] == bridge["pid"]
    assert cli.main(["--stop"]) == 0
    assert capsys.readouterr().out.endswith("off Remote Control\n")
    assert background.running(THREAD) is None
    assert cli.main(["--stop"]) == 0
    assert "not on Remote Control" in capsys.readouterr().out


def test_a_name_is_the_rest_of_the_line_and_renames_a_session_already_on(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    cli.main([])
    first = background.running(THREAD)
    assert cli.main(["My", "Project"]) == 0  # `!codex-rc My Project`
    assert 'on Remote Control as "My Project"' in capsys.readouterr().out
    renamed = background.running(THREAD)
    assert renamed["options"] == ["--", "My Project"]
    assert renamed["pid"] != first["pid"]  # the bridge was started again, to take the name


def test_stop_needs_a_thread_outside_a_session(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--stop"])
    assert "--stop needs --thread" in capsys.readouterr().err
