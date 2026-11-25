"""Bridges that serve from a background process.

`!codex-rc`, typed in a Codex session, has to hand the prompt back, so the
bridge it starts runs in a process of its own. Every bridge keeps a
record named for its thread and holds a lock on it for as long as it runs. That
is how a later command finds it, to say the thread is already served or to stop
it. The lock, not the pid, says a bridge is alive, so a record left behind by a
crash or a reboot reads as no bridge.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


# What a background bridge runs: this package's own command, told its thread.
_COMMAND = [sys.executable, "-m", "codex_remote_control.cli", "--thread"]


class StartFailed(Exception):
    """The background bridge did not come up. Carries what it logged."""


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "codex-remote-control"


def _path(thread_id: str) -> Path:
    return state_dir() / f"{thread_id}.json"


def _private(directory: Path) -> None:
    """Make the state directory, for its owner alone: a record holds a session's
    link, and a bridge's log can hold the start of what was typed in the app."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)


class Record:
    """A running bridge's own record. Hold it for the life of the process.
    It says what the bridge serves (`session`, `title`, `cwd`), since when
    (`started`, epoch seconds), and how the session stands (`state`: `idle`,
    `running`, or `requires_action` while a prompt waits on the app)."""

    def __init__(self, path: Path, file) -> None:
        self.path = path
        self._file = file
        self._info = {"pid": os.getpid(), "started": time.time()}
        self.update()

    def update(self, **info) -> None:
        self._info.update(info)
        self._file.seek(0)
        self._file.truncate()
        json.dump(self._info, self._file)
        self._file.flush()

    def move(self, thread_id: str) -> None:
        """Go over to another thread with the bridge (`/clear` starts one): the
        record is found by the thread it is named for. Renamed, the file stays
        the one this process holds locked. A background bridge's log goes too."""
        path = _path(thread_id)
        os.replace(self.path, path)
        log = self.path.with_suffix(".log")
        if log.exists():
            os.replace(log, path.with_suffix(".log"))
        self.path = path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        self._file.close()


def claim(thread_id: str) -> Record | None:
    """Take a thread for this process. None if another bridge is serving it."""
    path = _path(thread_id)
    _private(path.parent)
    file = open(path, "a+")
    for _ in range(20):  # a reader checking for a bridge holds the lock for an instant
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(0.05)
        else:
            return Record(path, file)
    file.close()
    return None


def running(thread_id: str) -> dict | None:
    """What the bridge serving a thread has recorded (`pid`, then `session`), or
    None if no bridge is."""
    try:
        file = open(_path(thread_id))
    except FileNotFoundError:
        return None
    with file:
        try:
            fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:  # held: its bridge is alive
            try:
                return json.loads(file.read())
            except ValueError:  # caught between two writes
                return {}
    return None


def save_upload(thread_id: str, file_uuid: str, name: str, data: bytes) -> Path:
    """Keep a file the app attached to a message where Codex can read it:
    beside the bridges' records, in a directory of the thread's own."""
    folder = state_dir() / "uploads" / thread_id
    _private(state_dir())
    _private(folder)
    path = folder / f"{file_uuid[:8]}-{os.path.basename(name) or 'file'}"
    path.write_bytes(data)
    return path


def drop_uploads(thread_id: str) -> None:
    """Forget the files attached to a thread that is gone."""
    shutil.rmtree(state_dir() / "uploads" / thread_id, ignore_errors=True)


def bridges() -> dict[str, dict]:
    """Every running bridge's record, by the thread it serves."""
    found = {}
    for path in sorted(state_dir().glob("*.json")):
        info = running(path.stem)
        if info is not None:
            found[path.stem] = info
    return found


def start(thread_id: str, options: list[str], *, timeout: float = 60.0) -> dict:
    """Serve a thread from a background process, unless one already does.
    Returns the bridge's record once its session is up."""
    log_path = _path(thread_id).with_suffix(".log")
    bridge = None
    if running(thread_id) is None:
        _private(log_path.parent)
        with open(log_path, "w") as log:
            bridge = subprocess.Popen(
                [*_COMMAND, thread_id, *options],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # out of reach of whatever ends the command that started it
                # Not "typed in a session" itself, or it would start a bridge in turn.
                env={k: v for k, v in os.environ.items() if k != "CODEX_THREAD_ID"},
            )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = running(thread_id)
        if info and info.get("session"):
            return info
        if info is None and (bridge is None or bridge.poll() is not None):
            said = log_path.read_text().strip()
            log_path.unlink()
            raise StartFailed(said or "the bridge stopped before its session was up")
        time.sleep(0.1)
    raise StartFailed(f"the bridge is still starting: see {log_path}")


def stop(thread_id: str, *, timeout: float = 15.0) -> bool:
    """Stop the bridge serving a thread, and wait for it to go. False if there
    was none. Its log goes with it: only a bridge that ended by itself leaves one."""
    info = running(thread_id)
    if not info:
        return False
    os.kill(info["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while running(thread_id) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    _path(thread_id).with_suffix(".log").unlink(missing_ok=True)
    return True
