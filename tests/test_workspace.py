"""The thread's directory as the app's panels ask about it."""
import subprocess

import pytest

from codex_remote_control import workspace


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "kept.txt").write_text("one\ntwo\n")
    (tmp_path / "gone.txt").write_text("bye\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "first")
    return tmp_path


def test_the_diff_is_the_working_tree_against_its_last_commit(repo):
    (repo / "kept.txt").write_text("one\n2\n")
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_text("fresh\n")  # untracked: what a file Codex just wrote is
    (repo / "blob.bin").write_bytes(b"\0\1")
    diff = workspace.diff(str(repo))
    assert {(s["path"], s["status"], s["added"], s["removed"], s["isUntracked"], s["isBinary"]) for s in diff["perFileStats"]} == {
        ("kept.txt", "modified", 1, 1, False, False), ("gone.txt", "deleted", 0, 1, False, False),
        ("new.txt", "added", 1, 0, True, False), ("blob.bin", "added", 0, 0, True, True)}
    assert diff["stats"] == {"filesCount": 4, "linesAdded": 2, "linesRemoved": 2}
    hunks = {file["path"]: file["hunks"] for file in diff["hunks"]}
    assert hunks["kept.txt"] == [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2, "lines": [" one", "-two", "+2"]}]
    assert hunks["new.txt"] == [{"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": 1, "lines": ["+fresh"]}]
    assert "blob.bin" not in hunks and diff["source"] == {"kind": "working-tree"}
    assert {"omitted", "restricted", "skippedLarge", "rootId"} <= set(diff)  # every key Claude Code's answer has


def test_a_checkout_with_no_commit_yet_and_a_plain_directory(tmp_path):
    assert workspace.diff(str(tmp_path)) is None  # not a checkout: the app is told there is no repository
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("hi\n")
    git(tmp_path, "add", "a.txt")
    assert workspace.diff(str(tmp_path))["perFileStats"][0]["status"] == "added"


def test_a_line_that_looks_like_a_header_stays_in_its_hunk():
    patch = "\n".join([
        "diff --git a/q.sql b/q.sql", "--- a/q.sql", "+++ b/q.sql", "@@ -1,2 +1 @@", "--- a comment", "-x = 1", "+x = 2",
        "\\ No newline at end of file",
        "diff --git a/old name.txt b/old name.txt", "deleted file mode 100644", "--- a/old name.txt\t", "+++ /dev/null", "@@ -1 +0,0 @@", "-bye"])
    assert workspace.parse_patch(patch) == {
        "q.sql": [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 1, "lines": ["--- a comment", "-x = 1", "+x = 2"]}],
        "old name.txt": [{"oldStart": 1, "oldLines": 1, "newStart": 0, "newLines": 0, "lines": ["-bye"]}]}


def test_a_long_diff_lists_the_files_it_does_not_show(repo, monkeypatch):
    monkeypatch.setattr(workspace, "_DIFF_LINES", 5)
    (repo / "kept.txt").write_text("1\n2\n")
    (repo / "big.txt").write_text("\n".join(map(str, range(10))) + "\n")
    diff = workspace.diff(str(repo))
    assert diff["skippedLarge"] == ["big.txt"] and [file["path"] for file in diff["hunks"]] == ["kept.txt"]
    assert diff["stats"]["filesCount"] == 2


def test_the_repository_is_the_github_one_origin_names(repo):
    assert workspace.repository(str(repo)) is None
    git(repo, "remote", "add", "origin", "git@github.com:someone/project.git")
    assert workspace.repository(str(repo)) == ("someone/project", "main")
    git(repo, "remote", "set-url", "origin", "https://gitlab.example/someone/project.git")
    assert workspace.repository(str(repo)) is None


def test_folders_list_from_the_workspace_and_no_further(repo):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("")
    assert workspace.entries(str(repo)) == ["gone.txt", "kept.txt", "src/"]  # no .git
    assert workspace.entries(str(repo), "src/") == ["src/", "src/a.py"]
    assert workspace.entries(str(repo), "../") is None and workspace.entries(str(repo), "kept.txt/") is None
    assert workspace.suggestions([{"path": "src/a.py"}, {"path": ".git/config"}, {"path": "src/a.py"}]) == ["src/a.py"]


def test_reading_is_kept_to_the_workspace(repo, tmp_path_factory):
    assert workspace.read(str(repo), "kept.txt") == {"absPath": "kept.txt", "contents": "one\ntwo\n", "lossy": False}
    assert workspace.read(str(repo), str(repo / "kept.txt"), 3) == {
        "absPath": str(repo / "kept.txt"), "contents": "one", "lossy": False, "truncated": True}
    (repo / "blob.bin").write_bytes(b"\xff\xfe")
    assert workspace.read(str(repo), "blob.bin")["lossy"] is True
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text("no")
    (repo / "link.txt").symlink_to(outside)
    for path in (str(outside), "../secret.txt", "link.txt", "missing.txt", "src"):
        with pytest.raises(workspace.ReadDenied):
            workspace.read(str(repo), path)
