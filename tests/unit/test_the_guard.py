"""The guard of the unit tests: no process, no connection. Each test tries one way out and
must meet the guard. A review found that nothing held the refusal of connect_ex or of
os.system: with either line of the guard gone, a unit test could open a connection or run a
command, and the suite stayed green."""

import os
import socket
import subprocess

import pytest

from tests.conftest import OutsideWorldError

NOWHERE = ("127.0.0.1", 9)  # a loopback port where nothing listens


def test_a_unit_test_cannot_start_a_process() -> None:
    with pytest.raises(OutsideWorldError, match="tried to start a process"):
        subprocess.Popen(["true"])  # noqa: S607 - the way out is the point, and the guard holds


def test_a_unit_test_cannot_run_a_shell_command() -> None:
    with pytest.raises(OutsideWorldError, match="tried to start a process"):
        os.system("true")  # noqa: S605, S607 - the way out is the point, and the guard holds


def test_a_unit_test_cannot_open_a_connection() -> None:
    with socket.socket() as sock, pytest.raises(OutsideWorldError, match="open a connection"):
        sock.connect(NOWHERE)


def test_a_unit_test_cannot_ask_whether_a_connection_opens() -> None:
    """connect_ex answers with a code instead of raising, and slips past a guard that
    refuses connect alone."""
    with socket.socket() as sock, pytest.raises(OutsideWorldError, match="open a connection"):
        sock.connect_ex(NOWHERE)


@pytest.mark.git
def test_a_git_test_may_start_git_and_nothing_else() -> None:
    subprocess.run(["git", "--version"], check=True, capture_output=True)  # noqa: S607 - git itself

    with pytest.raises(OutsideWorldError, match="may start git and nothing else"):
        subprocess.Popen(["true"])  # noqa: S607 - as above

    with socket.socket() as sock, pytest.raises(OutsideWorldError, match="open a connection"):
        sock.connect(NOWHERE)
