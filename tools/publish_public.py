#!/usr/bin/env python3
"""Publishes the code, and only the code, to the public repository.

This repository is the complete record: the code, its history, and documents that are
not for publication. The public repository, `origin`, receives the code without those
documents, in a history of its own. Its first commit is made here. Every later commit of
this repository's main branch is then carried over with its message and its dates, except
one that changed only documents.

A commit message is carried over as written. Say nothing in one that is not for
publication, and keep a change to documents in a commit of its own.

A plain `git push` from this repository would publish everything, history included, and
could not be recalled. The guard below refuses one, and this tool is the way out.

    uv run python tools/publish_public.py                  # say what would be published
    uv run python tools/publish_public.py --push           # publish it
    uv run python tools/publish_public.py --install-guard  # refuse a direct push, for good

The name and the address the public commits carry are set once. They live in this
repository's configuration, so they are in no published file:

    git config seeress.public-name  "Anthony"
    git config seeress.public-email "12345+name@users.noreply.github.com"

Exit status 0 when done, 1 otherwise.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = "tools/publish_public.py"
BRANCH = "main"
REMOTE_BRANCH = "refs/remotes/origin/main"
# What was last published: the commit of this repository, and the commit it became. Both
# are recorded here and never pushed.
PUBLISHED = "refs/seeress/published"
PUBLISHED_PUBLIC = "seeress.published-public"
PUBLIC_NAME = "seeress.public-name"
PUBLIC_EMAIL = "seeress.public-email"
# What never leaves this machine, by top-level folder.
PRIVATE: tuple[str, ...] = ("docs",)
ZERO = "0" * 40
GIT = shutil.which("git")

FIRST_SUBJECT = "Seeress 0.1.0: an employee-private browser agent"
FIRST_MESSAGE = f"""{FIRST_SUBJECT}

The program and its checks as they stand at the end of the foundations phase: the
agent loop, two doors, flows, the activity logger, and the tests, types and egress
check that hold them.

This history starts here. The planning and decision documents behind it are not
published, and neither is the earlier history.
"""


class PublishError(Exception):
    """Why nothing was published, or why a push was refused."""


@dataclass(frozen=True)
class Plan:
    """What the next publish would do."""

    first: bool
    commits: tuple[str, ...]  # commits of this repository, oldest first
    remote: str | None  # what the public repository holds on its main branch


# --------------------------------------------------------------------- git ---


def git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    message: str | None = None,
    allowed: tuple[int, ...] = (0,),
) -> str:
    """Run git in the repository and return what it wrote. A status outside `allowed` fails."""
    if GIT is None:
        raise PublishError("git is not installed")
    done = subprocess.run(
        [GIT, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        input=message,
        env={**os.environ, **env} if env else None,
        check=False,
    )
    if done.returncode not in allowed:
        raise PublishError(f"git {' '.join(args)} did not succeed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


def _setting(repo: Path, key: str, what: str) -> str:
    value = git(repo, "config", "--get", key, allowed=(0, 1))
    if not value:
        raise PublishError(f"{what} is not set. Set it once: git config {key} <value>")
    return value


def private_address(repo: Path) -> str:
    return _setting(repo, "user.email", "The address this repository commits under")


def public_address(repo: Path) -> str:
    return _setting(repo, PUBLIC_EMAIL, "The address the public commits carry")


def public_name(repo: Path) -> str:
    return _setting(repo, PUBLIC_NAME, "The name the public commits carry")


def _commit(repo: Path, ref: str) -> str | None:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, allowed=(0, 1)) or None


# ------------------------------------------------------------- the public tree ---


def withheld(repo: Path, commit: str) -> list[str]:
    """What a commit holds under the folders that never leave this machine."""
    return _lines(git(repo, "ls-tree", "-r", "--name-only", commit, "--", *PRIVATE))


def public_tree(repo: Path, commit: str) -> str:
    """The commit's tree without what stays here. Nothing on disk is read or written."""
    with tempfile.TemporaryDirectory(prefix="sr-publish-") as folder:
        env = {"GIT_INDEX_FILE": str(Path(folder) / "index")}
        git(repo, "read-tree", f"{commit}^{{tree}}", env=env)
        behind = withheld(repo, commit)
        if behind:
            git(repo, "update-index", "--force-remove", "--", *behind, env=env)
        return git(repo, "write-tree", env=env)


def files(repo: Path, tree: str) -> list[str]:
    return _lines(git(repo, "ls-tree", "-r", "--name-only", tree))


def holding(repo: Path, tree: str, text: str) -> list[str]:
    """The files of a tree that hold a piece of text."""
    found = git(repo, "grep", "-l", "-F", "-e", text, tree, allowed=(0, 1))
    return [line.split(":", 1)[1] for line in _lines(found)]


# ---------------------------------------------------------------- publishing ---


def plan(repo: Path) -> Plan:
    """What the next publish would do, or why it must not happen."""
    git(repo, "fetch", "--quiet", "origin")
    remote = _commit(repo, REMOTE_BRANCH)
    tip = git(repo, "rev-parse", "--verify", f"refs/heads/{BRANCH}")
    published = _commit(repo, PUBLISHED)
    if remote is None:
        if published is not None:
            raise PublishError(
                "I published from this repository before, and the public repository is "
                "empty now. Nothing is pushed: look at it first."
            )
        return Plan(first=True, commits=(tip,), remote=None)
    if published is None:
        raise PublishError(
            "The public repository already holds commits, and this repository has no "
            "record of publishing them. Nothing is pushed: look at them first."
        )
    if git(repo, "config", "--get", PUBLISHED_PUBLIC, allowed=(0, 1)) != remote:
        raise PublishError(
            "The public repository was pushed to outside this tool. Nothing is pushed: "
            "look at it first."
        )
    if not _is_ancestor(repo, published, tip):
        raise PublishError(
            "The history of this repository was rewritten after it was published. "
            "Nothing is pushed."
        )
    behind = _lines(git(repo, "rev-list", "--first-parent", "--reverse", f"{published}..{tip}"))
    return Plan(first=False, commits=tuple(behind), remote=remote)


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    done = (
        subprocess.run(
            [GIT, "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
            text=True,
            check=False,
        )
        if GIT
        else None
    )
    if done is None or done.returncode not in (0, 1):
        raise PublishError(f"could not tell whether {older[:7]} is part of the current history")
    return done.returncode == 0


def _carry(
    repo: Path, tree: str, parent: str | None, message: str, dates: tuple[str, str] | None
) -> str:
    name, address = public_name(repo), public_address(repo)
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": address,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": address,
    }
    if dates is not None:
        env |= {"GIT_AUTHOR_DATE": dates[0], "GIT_COMMITTER_DATE": dates[1]}
    return git(
        repo, "commit-tree", tree, *(("-p", parent) if parent else ()), env=env, message=message
    )


def publish(repo: Path) -> list[str]:
    """Carry this repository's commits over to the public repository. Returns what was pushed."""
    planned = plan(repo)
    private = private_address(repo)
    parent, made = planned.remote, []
    for commit in planned.commits:
        tree = public_tree(repo, commit)
        leaking = holding(repo, tree, private)
        if leaking:
            raise PublishError(
                f"Not published: {', '.join(leaking)} holds the address this repository "
                "commits under."
            )
        if parent is not None and tree == git(repo, "rev-parse", f"{parent}^{{tree}}"):
            continue  # nothing that is for publication changed
        if planned.first:
            message, dates = FIRST_MESSAGE, None
        else:
            message = git(repo, "log", "-1", "--format=%B", commit) + "\n"
            dates = (
                git(repo, "log", "-1", "--format=%aI", commit),
                git(repo, "log", "-1", "--format=%cI", commit),
            )
        parent = _carry(repo, tree, parent, message, dates)
        made.append(parent)
    if not made:
        return []
    reasons = guard(repo, [f"{made[-1]} {made[-1]} refs/heads/{BRANCH} {planned.remote or ZERO}"])
    if reasons:
        raise PublishError("The guard refused what I made: " + " ".join(reasons))
    git(repo, "push", "--quiet", "origin", f"{made[-1]}:refs/heads/{BRANCH}")
    git(repo, "update-ref", PUBLISHED, planned.commits[-1])
    git(repo, "config", PUBLISHED_PUBLIC, made[-1])
    return made


def describe(repo: Path) -> str:
    """Say what the next publish would do."""
    planned = plan(repo)
    if not planned.commits:
        return "Nothing to publish: main stands where the last publish left it."
    tip = planned.commits[-1]
    tree = public_tree(repo, tip)
    leaking = holding(repo, tree, private_address(repo))
    lines = [
        "The public repository is empty. One first commit will be made."
        if planned.first
        else f"{len(planned.commits)} commit(s) of this repository would be carried over.",
        f"{len(files(repo, tree))} file(s) would be published, as "
        f"{public_name(repo)} <{public_address(repo)}>.",
        *(f"  stays: {path}" for path in withheld(repo, tip)),
    ]
    if leaking:
        lines.append(
            f"It would be stopped: {', '.join(leaking)} holds the address this repository "
            "commits under."
        )
    else:
        lines.append("No file to be published holds the address this repository commits under.")
    return "\n".join(lines)


# ------------------------------------------------------------------- the guard ---


def _reason(repo: Path, commit: str, public: str, private: str) -> str | None:
    """Why one commit must not be published."""
    who = git(repo, "log", "-1", "--format=%ce", commit)
    if who != public:
        return f"it was made under {who}, so it is not made for publication. Publish with `{TOOL}`."
    behind = withheld(repo, commit)
    if behind:
        more = "…" if len(behind) > 5 else ""
        return f"it holds what stays here: {', '.join(behind[:5])}{more}."
    if private and private in git(repo, "log", "-1", "--format=%B", commit):
        return "its message holds the address this repository commits under."
    return None


def guard(repo: Path, lines: Iterable[str]) -> list[str]:
    """Why this push must not happen. Nothing means it may.

    Each line is what git gives a pre-push hook: a local ref, its commit, the remote ref,
    and what the remote holds now. Every commit a push would add is looked at: who made
    it, what it holds, and what it says.
    """
    try:
        public = public_address(repo)
    except PublishError as exc:
        return [f"{exc} Until it is set, nothing is pushed from this repository."]
    private = git(repo, "config", "--get", "user.email", allowed=(0, 1))
    reasons: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            reasons.append(f"a push I could not read was refused: {line[:80]!r}")
            continue
        _, local, target, remote = parts
        if local == ZERO:
            reasons.append("nothing is deleted on the public repository through this push")
            continue
        added = [local] if remote == ZERO else [f"{remote}..{local}"]
        for commit in _lines(git(repo, "rev-list", *added)):
            reason = _reason(repo, commit, public, private)
            if reason is not None:
                reasons.append(f"{target} would receive commit {commit[:7]}, and {reason}")
                break
    return reasons


def install_guard(repo: Path) -> Path:
    """Write the hook that makes a direct push fail. A hook that is already there stays."""
    hook = repo / git(repo, "rev-parse", "--git-path", "hooks") / "pre-push"
    script = (
        "#!/bin/sh\n"
        f"# Installed by {TOOL}. This repository holds what is not for publication.\n"
        f'exec "{sys.executable}" "{Path(__file__).resolve()}" guard\n'
    )
    if hook.exists() and hook.read_text(encoding="utf-8") != script:
        raise PublishError(
            f"there is already a pre-push hook here ({hook}), and it is not mine. "
            "I will not replace it."
        )
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    return hook


# --------------------------------------------------------------------- main ---


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv == ["guard"]:
            reasons = guard(Path.cwd(), sys.stdin.read().splitlines())
            for reason in reasons:
                print(f"refused: {reason}", file=sys.stderr)
            if reasons:
                print(f"Only {TOOL} publishes from this repository.", file=sys.stderr)
            return 1 if reasons else 0
        parser = argparse.ArgumentParser(
            description="Publishes the code, and only the code, to the public repository."
        )
        parser.add_argument(
            "--push", action="store_true", help="publish, instead of saying what would be"
        )
        parser.add_argument(
            "--install-guard",
            action="store_true",
            help="make a direct push from this repository fail",
        )
        args = parser.parse_args(argv)
        if args.install_guard:
            print(f"Installed: {install_guard(REPO)}")
        if args.push:
            made = publish(REPO)
            print(
                "Nothing to publish."
                if not made
                else f"Published {len(made)} commit(s); the public main branch is "
                f"at {made[-1][:7]}."
            )
        if not args.push and not args.install_guard:
            print(describe(REPO))
        return 0
    except PublishError as exc:
        print(f"Not done: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
