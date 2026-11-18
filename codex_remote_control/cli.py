"""codex-remote-control: serve a Codex thread as a Claude Remote Control session."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from claude_rc import RemoteControlClient, RemoteControlWorker, WorkerEvicted

from . import __version__
from .appserver import AppServer, AppServerClosed, AppServerError
from .bridge import Bridge, ThreadClosed, session_title

# The session tag that ties a Remote Control session to its Codex thread, so
# serving a thread again finds its session instead of making another.
THREAD_TAG = "codex-thread:"


def open_thread(codex: AppServer, args: argparse.Namespace) -> dict:
    """Subscribe to the thread to serve: the one named, the directory's most
    recent with `--last`, or a new one. Returns the app-server's answer, which
    carries the thread and its settings."""
    thread_id = args.thread
    if args.last:
        recent = codex.request("thread/list", {"cwd": args.cd, "limit": 1})["data"]
        if not recent:
            sys.exit(f"no Codex thread in {args.cd} yet")
        thread_id = recent[0]["id"]
    if thread_id:
        return codex.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    return codex.request("thread/start", {"cwd": args.cd, **({"model": args.model} if args.model else {})})


def open_session(rc: RemoteControlClient, thread: dict, name: str | None) -> tuple[str, bool]:
    """The Remote Control session for a thread, and whether it is new: the one
    made for the thread before, if that is still active, else a fresh one."""
    tag = THREAD_TAG + thread["id"]
    title = name or session_title(thread.get("name"), thread["cwd"])
    for session in rc.sessions(tags=tag, statuses="active"):
        if session.get("title") != title:
            rc.rename_session(session["id"], title)
        return session["id"], False
    return rc.create_session(title, cwd=thread["cwd"], tags=[tag])["id"], True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-remote-control",
        description="Serve a Codex thread as a Claude Remote Control session, to steer it from "
        "claude.ai/code and the Claude apps. Runs until interrupted.",
    )
    parser.add_argument("thread", nargs="?", help="the Codex thread to serve, by id as `codex resume` takes it (default: a new thread)")
    parser.add_argument("--last", action="store_true", help="serve the directory's most recent thread")
    parser.add_argument("-C", "--cd", metavar="DIR", default=os.getcwd(), help="the directory a new thread works in (default: here)")
    parser.add_argument("-m", "--model", help="the model of a new thread (default: Codex's own)")
    parser.add_argument("--name", help="the session's title in claude.ai (default: follows the thread's name)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log what crosses the bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.thread and args.last:
        parser.error("give a thread or --last, not both")
    args.cd = os.path.abspath(args.cd)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Stop on SIGTERM as on Ctrl-C, so the session says goodbye either way.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    codex = AppServer()
    try:
        codex.connect()
        opened = open_thread(codex, args)
    except AppServerError as e:
        sys.exit(f"Codex refused: {e}")
    thread = opened["thread"]
    rc = RemoteControlClient()
    session_id, new_session = open_session(rc, thread, args.name)
    print(f"thread   {thread['id']}  ({opened['cwd']})")
    print(f"session  https://claude.ai/code/{session_id.replace('cse_', 'session_', 1)}")
    print(f"tui      codex resume {thread['id']}", flush=True)

    try:
        with RemoteControlWorker(rc, session_id) as worker:
            bridge = Bridge(codex, worker, opened, follow_name=args.name is None)
            if new_session:
                bridge.backfill()
            bridge.run()
    except KeyboardInterrupt:
        return 0
    except ThreadClosed:
        print("the Codex thread was closed", file=sys.stderr)
        return 0
    except WorkerEvicted as e:
        print(f"the session was taken away: {e}", file=sys.stderr)
        return 0 if e.reason == "session_not_active" else 1
    except AppServerClosed as e:
        print(f"lost Codex: {e}", file=sys.stderr)
        return 1
    finally:
        codex.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
