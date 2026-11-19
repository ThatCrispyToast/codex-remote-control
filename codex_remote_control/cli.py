"""codex-rc: serve a Codex thread as a Claude Remote Control session."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys

import httpx
from claude_rc import (
    APIError,
    CredentialsError,
    RemoteControlClient,
    RemoteControlWorker,
    WorkerEvicted,
)

from . import __version__, background, workspace
from .appserver import AppServer, AppServerClosed, AppServerError
from .bridge import TITLE_PREFIX, Bridge, ThreadClosed, session_title

# The session tag that ties a Remote Control session to its Codex thread, so
# serving a thread again finds its session instead of making another.
THREAD_TAG = "codex-thread:"

_THREAD_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)

_NOT_SHARED = (
    "\nAnother Codex process has this thread to itself. A session started with -c, --enable, "
    "--disable or --search runs without the shared background server, and nothing else can join it."
)


def main_thread(thread_id: str) -> str:
    """The thread a Codex session typed in belongs to. A side conversation is
    a thread of its own, and a `!` command typed in it is given its id, but it
    is a fork: serving it would serve the thread as it stood when the side
    conversation began. A saved one names the thread it is attached to. A
    temporary one names none, and Codex keeps no history of it to serve. If
    Codex can't say, the id stands, and serving it reports what is wrong."""
    codex = AppServer()
    try:
        codex.connect()
        while True:
            thread = codex.request("thread/read", {"threadId": thread_id})["thread"]
            if thread.get("ephemeral"):
                sys.exit("a temporary side conversation can't go on Remote Control: type !codex-rc in the main thread")
            if not (parent := thread.get("attachedToThreadId")):
                return thread_id
            thread_id = parent
    except (AppServerError, OSError, subprocess.CalledProcessError, ValueError, KeyError):
        return thread_id
    finally:
        codex.close()


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


def open_session(rc: RemoteControlClient, thread: dict, name: str | None) -> tuple[str, str, bool, bool]:
    """The Remote Control session for a thread: its id, its title, whether it
    is new, and whether the title follows the thread's name.

    A thread served before gets the same session again, if that is still
    active. A session keeps the name it was given, here or in the app, until
    it is given another; one never named is titled after its thread."""
    tag = THREAD_TAG + thread["id"]
    after_thread = session_title(thread.get("name"), thread["cwd"])
    for session in rc.sessions(tags=tag, statuses="active"):
        current = session.get("title") or ""
        named = name is not None or (current != "" and not current.startswith(TITLE_PREFIX))
        title = name or (current if named else after_thread)
        if title != current:
            rc.rename_session(session["id"], title)
        return session["id"], title, False, not named
    title = name or after_thread
    repository, branch = workspace.repository(thread["cwd"]) or (None, None)
    session = rc.create_session(title, cwd=thread["cwd"], tags=[tag], repository=repository, branch=branch)
    return session["id"], title, True, name is None


def session_url(session_id: str) -> str:
    return f"https://claude.ai/code/{session_id.replace('cse_', 'session_', 1)}"


def explain(error: Exception) -> str:
    """What stopped the bridge, in a line its user can act on: the command is
    typed at a Codex prompt, where a traceback helps nobody."""
    if isinstance(error, CredentialsError):
        return f"Claude Code is not signed in on this machine. Run `claude`, sign in with /login, then try again. ({error})"
    if isinstance(error, FileNotFoundError) and error.filename == "codex":
        return "The codex command was not found. Install the Codex CLI, or put it on PATH."
    if isinstance(error, subprocess.CalledProcessError):
        said = (error.stderr or "").strip().splitlines()
        return f"Codex could not start its app-server: {said[-1] if said else error}. codex-rc needs Codex 0.170 or later."
    if isinstance(error, (ValueError, KeyError)):
        return "Codex's app-server gave an answer codex-rc could not read. codex-rc needs Codex 0.170 or later."
    if isinstance(error, APIError):
        return f"claude.ai refused ({error.status}): {str(error.body)[:300]}"
    if isinstance(error, httpx.HTTPError):
        return f"Could not reach claude.ai: {error or type(error).__name__}"
    return f"Could not reach Codex's app-server: {error}"


def list_bridges() -> int:
    """Print the threads on Remote Control from this machine."""
    serving = background.bridges()
    if not serving:
        print("nothing is on Remote Control")
    for thread_id, bridge in serving.items():
        where = f"  {bridge['cwd']}" if bridge.get("cwd") else ""
        print(f'{thread_id}  {bridge.get("session", "starting")}  "{bridge.get("title", "")}"{where}')
    return 0


def stop_bridge(thread_id: str | None, parser: argparse.ArgumentParser) -> int:
    """Take a thread off Remote Control: the one named, else the only one on it."""
    if thread_id is None:
        serving = background.bridges()
        if len(serving) > 1:
            list_bridges()
            parser.error("more than one thread is on Remote Control: name one with --thread ID")
        thread_id = next(iter(serving), None)
    stopped = thread_id is not None and background.stop(thread_id)
    print("off Remote Control" if stopped else "that thread is not on Remote Control" if thread_id else "nothing is on Remote Control")
    return 0


def serve_from_background(thread_id: str, args: argparse.Namespace) -> int:
    """Typed in a Codex session: serve from a background process, so the
    session gets its prompt back."""
    if args.name and background.running(thread_id):
        background.stop(thread_id)  # a bridge takes its session's name as it starts
    options = [*(["-v"] if args.verbose else []), *(["--", args.name] if args.name else [])]
    try:
        bridge = background.start(thread_id, options)
    except background.StartFailed as e:
        sys.exit(str(e))
    print(f'on Remote Control as "{bridge["title"]}": {bridge["session"]}')
    print("take it off with: codex-rc --stop")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-rc",
        description="Serve a Codex thread as a Claude Remote Control session, to steer it from "
        "claude.ai/code and the Claude apps. Typed in a Codex session (`!codex-rc`) it serves that "
        "session from the background; anywhere else it runs until interrupted.",
    )
    parser.add_argument("name", nargs="*", help='the session\'s name in claude.ai (default: "Codex: " and the thread\'s name, or its directory)')
    parser.add_argument("--stop", action="store_true", help="take the thread off Remote Control (outside a Codex session: the only one on it, or the one --thread names)")
    parser.add_argument("--list", action="store_true", help="list the threads on Remote Control from this machine")
    parser.add_argument("--thread", metavar="ID", help="the thread to serve, by the id `codex resume` takes (default: the session this is typed in, else a new thread)")
    parser.add_argument("--last", action="store_true", help="serve the directory's most recent thread")
    parser.add_argument("-C", "--cd", metavar="DIR", default=os.getcwd(), help="the directory a new thread works in (default: here)")
    parser.add_argument("-m", "--model", help="the model of a new thread (default: Codex's own)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log what crosses the bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    args.name = " ".join(args.name).strip() or None
    if args.thread and args.last:
        parser.error("give --thread or --last, not both")
    if args.name and _THREAD_ID.fullmatch(args.name):
        parser.error(f"that is a thread id, not a name: use --thread {args.name}")
    # Codex sets this for what a session runs, which is how `!codex-rc` knows its thread.
    here = os.environ.get("CODEX_THREAD_ID")
    args.thread = args.thread or (None if args.last else here)
    if args.list:
        return list_bridges()
    side = False
    if here and args.thread == here:
        args.thread = main_thread(here)
        side = args.thread != here
    if args.stop:
        return stop_bridge(args.thread, parser)
    if here and args.thread:
        if side:
            print("this is a side conversation: serving the thread it belongs to")
        return serve_from_background(args.thread, args)
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
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as e:
        sys.exit(explain(e))
    thread = opened["thread"]
    record = background.claim(thread["id"])
    if record is None:
        codex.close()
        other = background.running(thread["id"]) or {}
        sys.exit(f"this thread is already on Remote Control ({other.get('session', 'starting')}): take it off with --stop")

    try:
        rc = RemoteControlClient()
        session_id, title, new_session, follow_name = open_session(rc, thread, args.name)
        record.update(session=session_url(session_id), title=title, cwd=opened["cwd"])
        print(f"thread   {thread['id']}  ({opened['cwd']})")
        print(f"session  {session_url(session_id)}  ({title})")
        print(f"tui      codex resume {thread['id']}", flush=True)
        with RemoteControlWorker(rc, session_id) as worker:
            bridge = Bridge(codex, worker, opened, follow_name=follow_name)
            bridge.backfill(new=new_session)
            bridge.run()
    except KeyboardInterrupt:
        return 0
    except ThreadClosed:
        print("the Codex thread was closed", file=sys.stderr)
        background.drop_uploads(thread["id"])
        return 0
    except WorkerEvicted as e:
        print(f"the session was taken away: {e}", file=sys.stderr)
        return 0 if e.reason == "session_not_active" else 1
    except AppServerClosed as e:
        print(f"lost Codex: {e}", file=sys.stderr)
        return 1
    except (CredentialsError, APIError, httpx.HTTPError) as e:
        print(explain(e), file=sys.stderr)
        return 1
    finally:
        record.close()
        codex.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
