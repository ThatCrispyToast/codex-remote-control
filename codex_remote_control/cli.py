"""codex-remote-control: serve a Codex thread as a Claude Remote Control session."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from claude_rc import RemoteControlClient, RemoteControlWorker, WorkerEvicted

from . import __version__, background
from .appserver import AppServer, AppServerClosed, AppServerError
from .bridge import Bridge, ThreadClosed, session_title

# The session tag that ties a Remote Control session to its Codex thread, so
# serving a thread again finds its session instead of making another.
THREAD_TAG = "codex-thread:"

_NOT_SHARED = (
    "\nAnother Codex process has this thread to itself. A session started with -c, --enable, "
    "--disable or --search runs without the shared background server, and nothing else can join it."
)


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


def session_url(session_id: str) -> str:
    return f"https://claude.ai/code/{session_id.replace('cse_', 'session_', 1)}"


def serve_here(thread_id: str, args: argparse.Namespace) -> int:
    """Put the Codex session this command was typed in on Remote Control. The
    bridge serves from a background process, so the session gets its prompt back."""
    options = [*(["--name", args.name] if args.name else []), *(["-v"] if args.verbose else [])]
    try:
        bridge = background.start(thread_id, options)
    except background.StartFailed as e:
        sys.exit(str(e))
    print(f"on Remote Control: {bridge['session']}")
    print("take it off with: codex-remote-control --stop")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-remote-control",
        description="Serve a Codex thread as a Claude Remote Control session, to steer it from "
        "claude.ai/code and the Claude apps. Typed in a Codex session (`!codex-remote-control`) it "
        "serves that session from the background; anywhere else it runs until interrupted.",
    )
    parser.add_argument("thread", nargs="?", help="the Codex thread to serve, by id as `codex resume` takes it (default: the session this is typed in, else a new thread)")
    parser.add_argument("--stop", action="store_true", help="stop serving the thread")
    parser.add_argument("--last", action="store_true", help="serve the directory's most recent thread")
    parser.add_argument("-C", "--cd", metavar="DIR", default=os.getcwd(), help="the directory a new thread works in (default: here)")
    parser.add_argument("-m", "--model", help="the model of a new thread (default: Codex's own)")
    parser.add_argument("--name", help="the session's title in claude.ai (default: follows the thread's name)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log what crosses the bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.thread and args.last:
        parser.error("give a thread or --last, not both")
    # Codex sets this for what a session runs, which is how `!codex-remote-control` knows its thread.
    here = None if args.last else os.environ.get("CODEX_THREAD_ID")
    if args.stop:
        if not (args.thread or here):
            parser.error("--stop needs a thread")
        stopped = background.stop(args.thread or here)
        print("off Remote Control" if stopped else "that thread is not on Remote Control")
        return 0
    if here and not args.thread:
        return serve_here(here, args)
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
        sys.exit(f"Codex refused: {e}" + (_NOT_SHARED if "active writer" in str(e) else ""))
    thread = opened["thread"]
    record = background.claim(thread["id"])
    if record is None:
        codex.close()
        other = background.running(thread["id"]) or {}
        sys.exit(f"this thread is already served ({other.get('session', 'starting')}): stop that with --stop")

    try:
        rc = RemoteControlClient()
        session_id, new_session = open_session(rc, thread, args.name)
        record.update(session=session_url(session_id))
        print(f"thread   {thread['id']}  ({opened['cwd']})")
        print(f"session  {session_url(session_id)}")
        print(f"tui      codex resume {thread['id']}", flush=True)
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
        record.close()
        codex.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
