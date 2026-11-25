"""Publishing the code, and only the code, to the public repository.

Every test runs git on a throwaway repository whose remote is a bare repository in the
same temporary folder. Nothing here touches the real repository or the real remote.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import publish_public as publish

pytestmark = pytest.mark.git

GIT = shutil.which("git")
PRIVATE_ADDRESS = "someone@private.example"
PUBLIC_ADDRESS = "42+tony@users.noreply.github.com"
PUBLIC_IDENTITY = f"Tony <{PUBLIC_ADDRESS}>"
ZERO = "0" * 40


def git(repo: Path, *args: str) -> str:
    assert GIT is not None
    done = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def remote_holds(repo: Path) -> list[str]:
    return [line for line in git(repo, "ls-remote", "origin").splitlines() if line]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    assert GIT is not None
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    # Its HEAD names main, as GitHub does once the first branch is pushed.
    subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run([GIT, "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in {
        "user.name": "Anthony",
        "user.email": PRIVATE_ADDRESS,
        "seeress.public-name": "Tony",
        "seeress.public-email": PUBLIC_ADDRESS,
    }.items():
        git(work, "config", key, value)
    git(work, "remote", "add", "origin", str(remote))
    (work / "seeress").mkdir()
    (work / "seeress" / "__init__.py").write_text("", encoding="utf-8")
    (work / "README.md").write_text("# Seeress\n", encoding="utf-8")
    (work / "docs" / "plans").mkdir(parents=True)
    (work / "docs" / "plans" / "a-plan.md").write_text("not for publication\n", encoding="utf-8")
    commit(work, "first")
    return work


# ------------------------------------------------------------ the public tree ---


def test_the_public_tree_leaves_the_private_folder_behind(repo: Path) -> None:
    tree = publish.public_tree(repo, "HEAD")

    assert publish.files(repo, tree) == ["README.md", "seeress/__init__.py"]
    assert publish.withheld(repo, "HEAD") == ["docs/plans/a-plan.md"]


def test_building_the_public_tree_touches_neither_the_tree_on_disk_nor_the_index(
    repo: Path,
) -> None:
    git(repo, "rm", "-q", "--cached", "seeress/__init__.py")  # something staged, on purpose

    publish.public_tree(repo, "HEAD")

    assert git(repo, "diff", "--cached", "--name-only") == "seeress/__init__.py"
    assert (repo / "docs" / "plans" / "a-plan.md").read_text() == "not for publication\n"


def test_a_public_file_that_holds_the_private_address_is_named(repo: Path) -> None:
    (repo / "README.md").write_text(f"write to {PRIVATE_ADDRESS}\n", encoding="utf-8")
    commit(repo, "the address slips into a public file")
    tree = publish.public_tree(repo, "HEAD")

    assert publish.holding(repo, tree, PRIVATE_ADDRESS) == ["README.md"]
    assert publish.holding(repo, publish.public_tree(repo, "HEAD~1"), PRIVATE_ADDRESS) == []


# --------------------------------------------------------------- publishing ---


def test_the_first_publish_is_one_commit_under_the_public_address(repo: Path) -> None:
    made = publish.publish(repo)

    assert len(made) == 1
    assert git(repo, "rev-list", "--count", "origin/main") == "1"
    assert git(repo, "log", "-1", "--format=%an <%ae> | %cn <%ce>", "origin/main") == (
        f"{PUBLIC_IDENTITY} | {PUBLIC_IDENTITY}"
    )
    assert git(repo, "log", "-1", "--format=%s", "origin/main") == publish.FIRST_SUBJECT
    assert publish.files(repo, "origin/main") == ["README.md", "seeress/__init__.py"]


def test_a_later_commit_is_carried_over_with_its_message_and_its_dates(repo: Path) -> None:
    publish.publish(repo)
    (repo / "seeress" / "door.py").write_text("", encoding="utf-8")
    commit(repo, "feat: a door")

    publish.publish(repo)

    assert git(repo, "rev-list", "--count", "origin/main") == "2"
    assert git(repo, "log", "-1", "--format=%s", "origin/main") == "feat: a door"
    for field in ("%aI", "%cI"):
        assert git(repo, "log", "-1", f"--format={field}", "origin/main") == git(
            repo, "log", "-1", f"--format={field}", "main"
        )
    assert publish.files(repo, "origin/main") == [
        "README.md",
        "seeress/__init__.py",
        "seeress/door.py",
    ]


def test_a_commit_that_changed_only_private_documents_has_nothing_to_publish(
    repo: Path,
) -> None:
    publish.publish(repo)
    (repo / "docs" / "plans" / "another.md").write_text("still private\n", encoding="utf-8")
    commit(repo, "docs: a plan")

    assert publish.publish(repo) == []
    assert git(repo, "rev-list", "--count", "origin/main") == "1"

    # It is not picked up later either: what is private stays private.
    (repo / "seeress" / "x.py").write_text("", encoding="utf-8")
    commit(repo, "feat: x")
    publish.publish(repo)

    assert git(repo, "log", "--format=%s", "origin/main").splitlines()[0] == "feat: x"
    assert publish.withheld(repo, "origin/main") == []


def test_a_file_that_holds_the_private_address_stops_the_whole_publish(repo: Path) -> None:
    (repo / "README.md").write_text(f"write to {PRIVATE_ADDRESS}\n", encoding="utf-8")
    commit(repo, "the address slips into a public file")

    with pytest.raises(publish.PublishError, match=r"README\.md"):
        publish.publish(repo)

    assert remote_holds(repo) == []


def test_history_rewritten_after_it_was_published_is_refused(repo: Path) -> None:
    publish.publish(repo)
    (repo / "seeress" / "door.py").write_text("", encoding="utf-8")
    commit(repo, "feat: a door")
    publish.publish(repo)
    git(repo, "commit", "-q", "--amend", "-m", "feat: a door, changed afterwards")

    with pytest.raises(publish.PublishError, match="rewritten"):
        publish.publish(repo)

    assert git(repo, "log", "-1", "--format=%s", "origin/main") == "feat: a door"


def test_a_public_repository_pushed_to_by_someone_else_is_left_alone(
    repo: Path, tmp_path: Path
) -> None:
    assert GIT is not None
    publish.publish(repo)
    theirs = tmp_path / "theirs"
    subprocess.run([GIT, "clone", "-q", str(tmp_path / "remote.git"), str(theirs)], check=True)
    (theirs / "README.md").write_text("# Changed somewhere else\n", encoding="utf-8")
    for key, value in {"user.name": "Tony", "user.email": PUBLIC_ADDRESS}.items():
        git(theirs, "config", key, value)
    commit(theirs, "a change made outside the tool")
    git(theirs, "push", "-q", "origin", "main")
    (repo / "seeress" / "door.py").write_text("", encoding="utf-8")
    commit(repo, "feat: a door")

    with pytest.raises(publish.PublishError, match="outside this tool"):
        publish.publish(repo)

    assert git(repo, "log", "-1", "--format=%s", "origin/main") == "a change made outside the tool"


# ------------------------------------------------------------- saying it first ---


def test_saying_what_would_be_published_names_what_stays_and_pushes_nothing(repo: Path) -> None:
    text = publish.describe(repo)

    assert "public repository is empty" in text
    assert "2 file(s) would be published" in text
    assert "stays: docs/plans/a-plan.md" in text
    assert PUBLIC_IDENTITY in text
    assert remote_holds(repo) == []


# ------------------------------------------------------------------- the guard ---


def guard_line(repo: Path, ref: str, remote: str = ZERO) -> str:
    return f"refs/heads/main {git(repo, 'rev-parse', ref)} refs/heads/main {remote}"


def test_the_guard_refuses_a_commit_made_under_the_private_address(repo: Path) -> None:
    reasons = publish.guard(repo, [guard_line(repo, "main")])

    assert len(reasons) == 1
    assert "tools/publish_public.py" in reasons[0]


def test_the_guard_lets_the_public_history_through(repo: Path) -> None:
    made = publish.publish(repo)
    # What git gives the hook for a push of a commit by its hash.
    pushed = f"{made[-1]} {made[-1]} refs/heads/main {ZERO}"

    assert publish.guard(repo, [pushed]) == []


def test_the_guard_refuses_a_commit_under_the_public_address_that_holds_a_private_document(
    repo: Path,
) -> None:
    assert GIT is not None
    made = subprocess.run(
        [GIT, "-C", str(repo), "commit-tree", "HEAD^{tree}", "-m", "a document in it"],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Tony",
            "GIT_AUTHOR_EMAIL": PUBLIC_ADDRESS,
            "GIT_COMMITTER_NAME": "Tony",
            "GIT_COMMITTER_EMAIL": PUBLIC_ADDRESS,
        },
    ).stdout.strip()

    reasons = publish.guard(repo, [f"{made} {made} refs/heads/main {ZERO}"])

    assert len(reasons) == 1
    assert "docs/plans/a-plan.md" in reasons[0]


def test_the_guard_refuses_a_deletion_on_the_public_repository(repo: Path) -> None:
    reasons = publish.guard(repo, [f"refs/heads/main {ZERO} refs/heads/main {ZERO}"])

    assert reasons == ["nothing is deleted on the public repository through this push"]


# ------------------------------------------------------------ the installed hook ---


def test_a_direct_push_is_stopped_by_the_installed_guard(repo: Path) -> None:
    assert GIT is not None
    publish.install_guard(repo)

    done = subprocess.run(
        [GIT, "-C", str(repo), "push", "origin", "main"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert "tools/publish_public.py" in done.stderr
    assert remote_holds(repo) == []


def test_the_tool_s_own_push_passes_the_installed_guard(repo: Path) -> None:
    publish.install_guard(repo)

    publish.publish(repo)

    assert git(repo, "rev-list", "--count", "origin/main") == "1"


def test_another_pre_push_hook_is_never_replaced(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho something else\n", encoding="utf-8")

    with pytest.raises(publish.PublishError, match="already a pre-push hook"):
        publish.install_guard(repo)

    assert hook.read_text() == "#!/bin/sh\necho something else\n"
