"""The guard that keeps a test away from the rest of the machine.

One test is for the exemption a test needs to run git: it may start git, and nothing
else. The others hold the guard as it was.
"""

import shutil
import socket
import subprocess
import sys

import pytest

GIT = shutil.which("git")


@pytest.mark.git
def test_a_test_marked_git_may_start_git() -> None:
    assert GIT is not None
    done = subprocess.run([GIT, "--version"], capture_output=True, text=True, check=True)

    assert done.stdout.startswith("git version")


@pytest.mark.git
def test_a_test_marked_git_may_start_nothing_else() -> None:
    with pytest.raises(RuntimeError) as caught:
        subprocess.run([sys.executable, "-c", "pass"], check=False)

    assert "may start git and nothing else" in str(caught.value)


@pytest.mark.git
def test_a_test_marked_git_still_cannot_open_a_connection() -> None:
    with pytest.raises(RuntimeError) as caught:
        socket.socket().connect(("127.0.0.1", 9))

    assert "tried to start a process or open a connection" in str(caught.value)


def test_a_test_with_no_marker_cannot_start_a_process() -> None:
    with pytest.raises(RuntimeError) as caught:
        subprocess.run([sys.executable, "-c", "pass"], check=False)

    assert "tried to start a process or open a connection" in str(caught.value)
