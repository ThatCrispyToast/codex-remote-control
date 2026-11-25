"""A client for Codex's app-server, the JSON-RPC interface the Codex TUI runs on.

Codex keeps one app-server per user as a background daemon, and every `codex`
TUI on the machine is a client of it. This connects to that same daemon over
its control socket (WebSocket on a Unix socket), so a thread driven from here
is the thread a TUI sees, live, and the other way round. Given a `remote`, it
connects instead to an app-server of its own, the one `codex app-server
--listen unix://PATH` runs and `codex --remote unix://PATH` joins.

The protocol is Codex's own: `codex app-server generate-ts --out DIR` writes
its types. Checked against Codex 0.170.0.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections.abc import Iterator
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, unix_connect

from . import __version__


class AppServerError(RuntimeError):
    """The app-server answered a request with a JSON-RPC error."""

    def __init__(self, method: str, error: dict):
        super().__init__(f"{method}: {error.get('message') or error}")
        self.code = error.get("code")


class AppServerClosed(ConnectionError):
    """The connection to the app-server is gone (the daemon restarts to update itself)."""


def start_daemon() -> str:
    """Make sure Codex's app-server daemon is running; return its socket path.
    `codex app-server daemon start` is a no-op when one already is."""
    out = subprocess.run(
        ["codex", "app-server", "daemon", "start"], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)["socketPath"]


def remote_socket(remote: str) -> str:
    """The socket path of a `unix://PATH` address, the form of `codex --remote`
    that codex-rc takes. ValueError for any other."""
    path = remote.removeprefix("unix://")
    if path == remote or not path.startswith("/"):
        raise ValueError(f"{remote!r} is not unix://PATH with an absolute PATH")
    return path


def resume_command(remote: str | None, thread_id: str) -> str:
    """What opens a thread in a Codex terminal, on the app-server it is served from."""
    return f"codex{f' --remote {remote}' if remote else ''} resume {thread_id}"


class AppServer:
    """One connection to the daemon, or with ``remote`` (``unix://PATH``) to the
    app-server listening there. :meth:`request` may be called from any thread;
    notifications and the server's own requests (approvals) come out of
    :meth:`messages`."""

    def __init__(self, remote: str | None = None) -> None:
        self.remote = remote
        self._ws: ClientConnection | None = None
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._waiters: dict[int, queue.Queue] = {}
        self._inbox: queue.Queue = queue.Queue()

    def connect(self) -> None:
        socket = start_daemon() if self.remote is None else remote_socket(self.remote)
        self._ws = unix_connect(socket, max_size=None)
        threading.Thread(target=self._read, name="codex-app-server", daemon=True).start()
        self.request("initialize", {
            "clientInfo": {"name": "codex-remote-control", "title": "Claude Remote Control", "version": __version__},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        })
        self._send({"method": "initialized"})

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()

    def _send(self, message: dict) -> None:
        with self._send_lock:
            try:
                self._ws.send(json.dumps(message))
            except ConnectionClosed as e:
                raise AppServerClosed(str(e)) from None

    def _read(self) -> None:
        try:
            for raw in self._ws:
                message = json.loads(raw)
                if "method" in message:
                    self._inbox.put(message)
                elif (waiter := self._waiters.get(message.get("id"))) is not None:
                    waiter.put(message)
        except ConnectionClosed:
            pass
        finally:
            self._inbox.put(None)
            for waiter in list(self._waiters.values()):
                waiter.put(None)

    def request(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> Any:
        """Call a method and return its result. Raises :class:`AppServerError`
        on an error answer, :class:`AppServerClosed` if the daemon goes away."""
        waiter: queue.Queue = queue.Queue(1)
        with self._send_lock:
            self._next_id += 1
            request_id = self._next_id
        self._waiters[request_id] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            reply = waiter.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method}: no answer from the app-server in {timeout:g}s") from None
        finally:
            del self._waiters[request_id]
        if reply is None:
            raise AppServerClosed(f"the app-server connection closed during {method}")
        if "error" in reply:
            raise AppServerError(method, reply["error"])
        return reply.get("result")

    def respond(self, request_id: Any, result: dict) -> None:
        """Answer one of the server's requests (an approval, a question)."""
        self._send({"id": request_id, "result": result})

    def messages(self) -> Iterator[dict]:
        """Notifications (`method`, `params`) and server requests (those with an
        `id`, which want :meth:`respond`), in order. Raises
        :class:`AppServerClosed` when the connection ends."""
        while (message := self._inbox.get()) is not None:
            yield message
        raise AppServerClosed("the app-server connection closed")
