"""The guard that keeps a test away from the rest of the machine.

One test is for the exemption a test needs to run git: it may start git, and nothing
else. Two hold the guard to the home: a test runs in one of its own. The others hold the
guard as it was.
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests import conftest

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


def test_a_test_runs_in_a_home_of_its_own() -> None:
    """Asked for or not. A test once rewrote the activity logger's own LaunchAgent file:
    it ran code that built a path from Path.home(), and HOME still named the real home."""
    home = Path(os.environ["HOME"])

    assert Path.home() == home
    assert home.parent.name.startswith("sr-home-")


def test_a_test_with_no_marker_finds_the_seeress_home_inside_its_own() -> None:
    assert Path(os.environ["SEERESS_HOME"]) == Path.home() / ".seeress"


@pytest.mark.git
def test_a_test_marked_git_also_runs_in_a_home_of_its_own() -> None:
    home = Path(os.environ["HOME"])

    assert Path.home() == home
    assert home.parent.name.startswith("sr-home-")


def test_a_test_cannot_connect_through_connect_ex_either() -> None:
    with pytest.raises(RuntimeError, match="tried to start a process or open a connection"):
        socket.socket().connect_ex(("127.0.0.1", 9))


def test_a_test_cannot_run_a_shell_command() -> None:
    with pytest.raises(RuntimeError, match="tried to start a process or open a connection"):
        os.system("true")  # noqa: S605, S607 - refused by the guard before it runs


def test_the_browser_tool_s_library_is_kept_in_a_home_of_its_own() -> None:
    """Two test modules import it, and on import it makes its folders and reads its
    workspace from its home. That is never the home the run started in."""
    home = Path(os.environ["BH_HOME"]).resolve()

    assert home.is_dir()
    assert home.parent.name.startswith("sr-")
    assert not home.is_relative_to(Path(conftest.HOME_THE_RUN_STARTED_IN))
