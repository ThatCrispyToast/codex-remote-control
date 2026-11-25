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
record.update(session="https://claude.ai/code/session_1", title=title, options=options, env=os.environ.get("CODEX_THREAD_ID"), state="idle")
signal.signal(signal.SIGTERM, lambda *_: (record.close(), sys.exit(0)))
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(background, "_COMMAND", [sys.executable, "-c", BRIDGE])
    monkeypatch.setattr(cli, "main_thread", lambda thread_id, remote: thread_id)  # no Codex to ask
    yield tmp_path / "codex-remote-control"
    background.stop(THREAD)


def test_a_bridge_holds_its_threads_record_while_it_runs(state):
    assert background.running(THREAD) is None
    record = background.claim(THREAD)
    record.update(session="https://claude.ai/code/session_1")
    assert background.running(THREAD)["session"] == "https://claude.ai/code/session_1"
    assert background.claim(THREAD) is None  # one bridge to a thread
    assert state.stat().st_mode & 0o777 == 0o700  # records and logs are their owner's alone
    assert background.running("thr-2") is None
    record.close()
    assert background.running(THREAD) is None
    assert not list(state.iterdir())


def test_a_record_follows_its_bridge_to_another_thread_with_its_log(state):
    record = background.claim(THREAD)
    (state / f"{THREAD}.log").write_text("started\n")
    record.move("thr-2")
    record.update(state="idle")
    assert background.running(THREAD) is None
    assert background.running("thr-2")["state"] == "idle"
    assert sorted(path.name for path in state.iterdir()) == ["thr-2.json", "thr-2.log"]
    assert list(background.bridges()) == ["thr-2"]
    record.close()
    (state / "thr-2.log").unlink()


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


def test_stop_outside_a_session_takes_the_only_bridge(capsys):
    assert cli.main(["--stop"]) == 0
    assert capsys.readouterr().out == "nothing is on Remote Control\n"
    background.start(THREAD, [])
    assert cli.main(["--stop"]) == 0
    assert capsys.readouterr().out == "off Remote Control\n"
    assert background.running(THREAD) is None


def test_stop_asks_which_when_there_are_several(capsys):
    background.start(THREAD, [])
    background.start("thr-2", [])
    try:
        with pytest.raises(SystemExit):
            cli.main(["--stop"])
        said = capsys.readouterr()
        assert "name one with --thread" in said.err and "thr-2" in said.out
        assert cli.main(["--stop", "--thread", "thr-2"]) == 0
    finally:
        background.stop("thr-2")


def test_list_shows_what_is_on_remote_control(capsys):
    assert cli.main(["--list"]) == 0
    assert capsys.readouterr().out == "nothing is on Remote Control\n"
    background.start(THREAD, ["--", "My Project"])
    assert cli.main(["--list"]) == 0
    assert capsys.readouterr().out == 'thr-1  idle  https://claude.ai/code/session_1  "My Project"\n'


def test_a_bridge_on_an_app_server_of_its_own_starts_its_background_bridge_there(monkeypatch):
    """`!codex-rc` typed in a TUI joined to that app-server finds it in the environment."""
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    monkeypatch.setenv("CODEX_RC_REMOTE", "unix:///run/agent/app-server.sock")
    assert cli.main(["Agent"]) == 0
    assert background.running(THREAD)["options"] == ["--remote", "unix:///run/agent/app-server.sock", "--", "Agent"]


def test_a_remote_codex_rc_cannot_reach_is_refused_at_once(monkeypatch, capsys):
    for remote in ("ws://127.0.0.1:4500", "unix://", "unix://relative.sock"):
        with pytest.raises(SystemExit):
            cli.main(["--remote", remote, "--list"])
        assert "--remote" in capsys.readouterr().err


def test_an_attached_file_is_kept_private_until_its_thread_is_gone(state):
    path = background.save_upload(THREAD, "0123456789abcdef", "../../etc/notes.txt", b"hi")
    assert path == state / "uploads" / THREAD / "01234567-notes.txt" and path.read_bytes() == b"hi"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    background.drop_uploads(THREAD)
    assert not path.parent.exists()
