"""What the app asks about the thread's directory: its diff, its files.

Claude Code answers these from the working tree it runs in. The bridge runs on
the same machine as Codex, so it answers them the same way, from the thread's
directory: the app's diff panel, its file mentions and its file viewer then
work on a Codex session as on a Claude one. The answers have the shapes Claude
Code's own have (read from a 2.1.327 worker).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
from pathlib import Path

# How long git gets to answer. Claude Code gives its own diff eight seconds.
_GIT_TIMEOUT_S = 8.0
# A diff past this many lines is cut off: the files beyond it are listed, with
# their counts, and not shown.
_DIFF_LINES = 4000
# An untracked file is shown, as all new, up to this size.
_UNTRACKED_BYTES = 64 * 1024
# How much of a file the app's viewer gets unless it asks for less.
_READ_BYTES = 256 * 1024
# How many files a mention search offers, or a folder lists.
_SUGGESTIONS = 100

_STATUS = {"A": "added", "D": "deleted", "M": "modified", "R": "renamed", "C": "copied", "T": "modified"}
# A GitHub remote, over https or ssh, with or without ".git".
_GITHUB = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
# The tree a repository with no commit yet is compared against.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class ReadDenied(Exception):
    """The file is not one the app may read: outside the workspace, or not a file."""


def _git(cwd: str, *args: str) -> str | None:
    """A git command's output in ``cwd``, or None if it failed or took too long."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "-c", "core.quotePath=false", *args],
            capture_output=True, timeout=_GIT_TIMEOUT_S, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None


def root(cwd: str) -> str | None:
    """The top of the git checkout ``cwd`` is in, or None outside one."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return top.strip() or None if top else None


def repository(cwd: str) -> tuple[str, str | None] | None:
    """The GitHub repository ``cwd`` is a checkout of, as ``owner/name``, and
    the branch it is on. None for a directory that isn't one. A session is
    told this as it is made: the app keeps its diff panel for sessions that
    have a repository."""
    remote = _git(cwd, "remote", "get-url", "origin")
    found = _GITHUB.search(remote.strip()) if remote else None
    if found is None:
        return None
    branch = _git(cwd, "branch", "--show-current")
    return found[1], (branch or "").strip() or None


def parse_patch(patch: str) -> dict[str, list[dict]]:
    """A unified diff (``git diff``) as each file's hunks, in the shape the
    app's diff panel takes: ``{oldStart, oldLines, newStart, newLines, lines}``
    with every line still carrying its ``+``, ``-`` or space."""
    files: dict[str, list[dict]] = {}
    hunks: list[dict] | None = None
    name = ""
    old_left = new_left = 0  # lines the hunk being read still has: a "--- x" inside one is content
    for line in patch.splitlines():
        if old_left > 0 or new_left > 0:
            mark = line[:1]
            if mark == "\\":  # "\ No newline at end of file"
                continue
            old_left -= mark in (" ", "-", "")
            new_left -= mark in (" ", "+", "")
            hunks[-1]["lines"].append(line or " ")
        elif line.startswith("--- "):
            name = line[4:].rstrip("\t").removeprefix("a/")
        elif line.startswith("+++ "):
            if line[4:] != "/dev/null":  # a deleted file keeps the name its "---" line gave
                name = line[4:].rstrip("\t").removeprefix("b/")
            hunks = files.setdefault(name, [])
        elif line.startswith("@@") and hunks is not None:
            old, new = line.split("@@")[1].split()
            old_start, _, old_lines = old[1:].partition(",")
            new_start, _, new_lines = new[1:].partition(",")
            old_left, new_left = int(old_lines or 1), int(new_lines or 1)
            hunks.append({
                "oldStart": int(old_start), "oldLines": old_left,
                "newStart": int(new_start), "newLines": new_left, "lines": [],
            })
        elif line.startswith("diff --git "):
            hunks = None
    return files


def _untracked(top: str, path: str) -> list[str] | None:
    """An untracked file's lines, if it is text and small enough to show."""
    try:
        data = (Path(top) / path).read_bytes()
    except OSError:
        return None
    if len(data) > _UNTRACKED_BYTES or b"\0" in data:
        return None
    return data.decode(errors="replace").splitlines()


def diff(cwd: str) -> dict | None:
    """The working tree's changes since its last commit, as the answer to the
    app's ``get_workspace_diff``. None outside a git checkout, or when git
    does not answer in time.

    Untracked files count as added and are shown whole, which is what a file
    Codex has just written is."""
    top = root(cwd)
    if top is None:
        return None
    base = "HEAD" if _git(top, "rev-parse", "--verify", "--quiet", "HEAD") else _EMPTY_TREE
    numstat = _git(top, "diff", base, "--numstat", "--no-renames")
    names = _git(top, "diff", base, "--name-status", "--no-renames")
    patch = _git(top, "diff", base, "--no-color", "--no-ext-diff", "--no-renames")
    others = _git(top, "ls-files", "--others", "--exclude-standard")
    if None in (numstat, names, patch, others):
        return None
    status = dict(reversed(row.split("\t", 1)) for row in names.splitlines() if "\t" in row)
    stats = []
    for row in numstat.splitlines():
        added, removed, path = row.split("\t", 2)
        binary = added == "-"
        stats.append({
            "path": path, "added": 0 if binary else int(added), "removed": 0 if binary else int(removed),
            "isBinary": binary, "isUntracked": False, "status": _STATUS.get(status.get(path, "M")[:1], "modified"),
        })
    hunks = parse_patch(patch)
    for path in others.splitlines():
        lines = _untracked(top, path)
        if path in status:  # dropped from the index and still on disk: the tracked diff has it
            continue
        stats.append({
            "path": path, "added": len(lines or []), "removed": 0,
            "isBinary": lines is None, "isUntracked": True, "status": "added",
        })
        if lines:
            hunks[path] = [{
                "oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(lines),
                "lines": [f"+{line}" for line in lines],
            }]
    shown, skipped, budget = [], [], _DIFF_LINES
    for path, file_hunks in hunks.items():
        size = sum(len(h["lines"]) for h in file_hunks)
        if size > budget:
            skipped.append(path)
            continue
        budget -= size
        shown.append({"path": path, "hunks": file_hunks})
    return {
        "hunks": shown,
        "perFileStats": stats,
        "stats": {
            "filesCount": len(stats),
            "linesAdded": sum(s["added"] for s in stats),
            "linesRemoved": sum(s["removed"] for s in stats),
        },
        "omitted": {"exact": True, "files": 0},
        "restricted": [],
        "skippedLarge": skipped,
        "rootId": base64.urlsafe_b64encode(hashlib.md5(top.encode()).digest()).decode().rstrip("="),
        "source": {"kind": "working-tree"},
    }


def entries(cwd: str, folder: str = "") -> list[str] | None:
    """What is in ``cwd``, or in a ``folder`` of it, as paths from ``cwd``
    with directories marked by a trailing slash: what the app's file tree and
    its mentions show before anything is searched for. None if ``folder`` is
    not a directory of the workspace."""
    top = Path(cwd).resolve()
    where = (top / folder).resolve()
    if not where.is_dir() or not where.is_relative_to(top):
        return None
    try:
        found = sorted(os.scandir(where), key=lambda entry: entry.name)
    except OSError:
        return None
    prefix = f"{where.relative_to(top)}/" if where != top else ""
    listed = [f"{prefix}{entry.name}{'/' if entry.is_dir() else ''}" for entry in found if entry.name != ".git"]
    return ([prefix] if prefix else []) + listed[:_SUGGESTIONS]


def suggestions(matches: list[dict]) -> list[str]:
    """Codex's fuzzy file search results as paths to offer, best first."""
    paths = [m["path"] for m in matches if not m["path"].startswith(".git/")]
    return list(dict.fromkeys(paths))[:_SUGGESTIONS]


def read(cwd: str, path: str, max_bytes: int | None = None) -> dict:
    """A file of the workspace, as the answer to the app's ``read_file``.
    Raises :class:`ReadDenied` for anything outside the thread's directory and
    its git checkout, and for what isn't a readable file."""
    roots = [Path(r).resolve() for r in (cwd, root(cwd)) if r]
    target = Path(path)
    candidates = [target] if target.is_absolute() else [r / target for r in roots]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and any(resolved.is_relative_to(r) for r in roots):
            break
    else:
        raise ReadDenied(path)
    limit = max_bytes if isinstance(max_bytes, int) and 0 < max_bytes < _READ_BYTES else _READ_BYTES
    try:
        with open(resolved, "rb") as file:
            data = file.read(limit + 1)
    except OSError:
        raise ReadDenied(path) from None
    truncated = len(data) > limit
    text = data[:limit].decode(errors="replace")
    answer = {"absPath": path, "contents": text, "lossy": "�" in text}
    if truncated:
        answer["truncated"] = True
    return answer
