"""The guard of the unit tests: no process, no connection, and no path into the home the run
started in. Each test tries one way out and must meet the guard. A review found that nothing
held the refusal of connect_ex or of os.system: with either line of the guard gone, a unit
test could open a connection or run a command, and the suite stayed green."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seeress import watcher
from tests import conftest
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


def test_the_paths_the_logger_bound_at_import_name_the_home_of_the_test() -> None:
    """seeress.watcher binds its two paths when it is imported, from the home there was
    then. A test that used them without rebinding them itself reached into the home the
    run started in: with one fixture line of a test module removed, six tests did. The
    guard binds them for every test, and no test has to remember to.

    The module is imported at the top of this file, so that its paths are bound when the
    file is read, before any test has a home of its own. Imported in the test, they would
    be bound to the home of the test, and the test would pass with no guard at all."""
    assert Path(os.environ["SEERESS_HOME"]) / "watch" == watcher.WATCH_DIR
    assert (
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
        == watcher.PORT_FILE
    )


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no interval timer on this system")
def test_a_unit_test_that_does_not_end_fails_and_says_which_it_is() -> None:
    """No test that needs no browser takes more than a few seconds. One that is still running
    after its limit waits for something that does not come: it fails, says which it is, and
    the run goes on. Before, six rules of the capture, each broken alone, failed a test by
    name and then left the run going for good. This test's own limit is made shorter here."""
    assert signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL, "no limit is set"
    left = signal.setitimer(signal.ITIMER_REAL, 0)  # read, and cleared: the test sets its own
    assert 0 < left[0] <= conftest.LIMIT_OF_A_UNIT_TEST, "the limit is not armed for a unit test"

    signal.setitimer(signal.ITIMER_REAL, 0.2)
    with pytest.raises(BaseException, match="is still running after") as caught:
        time.sleep(5)

    assert "test_a_unit_test_that_does_not_end_fails" in str(caught.value)


@pytest.mark.git
def test_a_git_test_may_start_git_and_nothing_else() -> None:
    subprocess.run(["git", "--version"], check=True, capture_output=True)  # noqa: S607 - git itself

    with pytest.raises(OutsideWorldError, match="may start git and nothing else"):
        subprocess.Popen(["true"])  # noqa: S607 - as above

    with socket.socket() as sock, pytest.raises(OutsideWorldError, match="open a connection"):
        sock.connect(NOWHERE)


def inside_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """Run the tests in tmp_path inside this run, under the guard's limit and nothing else of
    this suite, with the limit of a unit test at three tenths of a second. Returns the exit
    of the run and what it printed."""
    monkeypatch.setattr(conftest, "LIMIT_OF_A_UNIT_TEST", 0.3)

    # The class itself is the plugin, and not an instance of it: pytest reads a plugin's
    # fixtures off its type, and on an instance the fixture would be a method, asked for its
    # request by the class itself.
    class Inner:
        pytest_runtest_setup = staticmethod(conftest.pytest_runtest_setup)
        pytest_runtest_call = staticmethod(conftest.pytest_runtest_call)
        pytest_runtest_teardown = staticmethod(conftest.pytest_runtest_teardown)
        pytest_runtest_protocol = staticmethod(conftest.pytest_runtest_protocol)
        pytest_runtest_makereport = staticmethod(conftest.pytest_runtest_makereport)

    before = set(sys.modules)
    said = pytest.main(
        [str(tmp_path), "-p", "no:cacheprovider", "-v", "--no-header", "--rootdir", str(tmp_path)],
        plugins=[Inner],
    )
    # The run shares the modules of this process: another run inside it would meet its
    # test modules on import, and refuse its own files as not the ones imported.
    for name in set(sys.modules) - before:
        module = sys.modules[name]
        if Path(getattr(module, "__file__", "") or ".").is_relative_to(tmp_path):
            del sys.modules[name]
    return int(said), capsys.readouterr().out


def test_no_test_is_started_after_one_did_not_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run inside the run, in this process: one test that does not end, and one after it.
    The first fails and is named; the second is not started. With only a limit on each
    test, every test after a broken one that waits pays the limit too, and a run can take
    longer than the thing the limit was meant to save."""
    (tmp_path / "test_first.py").write_text(
        "import time\n\n\ndef test_does_not_end() -> None:\n    time.sleep(5)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_second.py").write_text(
        "def test_comes_after() -> None:\n    pass\n", encoding="utf-8"
    )

    said, out = inside_the_run(tmp_path, monkeypatch, capsys)

    assert said == 1, out  # a failure is a failure
    assert "test_does_not_end" in out and "is still running after 0.3 s" in out
    assert "test_comes_after PASSED" not in out


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no interval timer on this system")
@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_the_setup_and_the_teardown_of_a_test_are_held_to_the_limit_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
) -> None:
    """The limit is armed for each phase of a test on its own: a fixture that does not end in
    its setup, or in its teardown, fails the test by name, and the run stops there. Armed
    once for a whole test, the alarm went off while the runner was writing the report of
    another failure."""
    waiting = "    time.sleep(5)\n"
    if phase == "setup":
        fixture = f"def waiting() -> None:\n{waiting}    yield\n"
    else:
        fixture = f"def waiting() -> None:\n    yield\n{waiting}"
    (tmp_path / "test_first.py").write_text(
        f"import time\n\nimport pytest\n\n\n@pytest.fixture\n{fixture}\n\n"
        f"def test_waits_in_its_{phase}(waiting) -> None:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "test_second.py").write_text(
        "def test_comes_after() -> None:\n    pass\n", encoding="utf-8"
    )

    said, out = inside_the_run(tmp_path, monkeypatch, capsys)

    assert said == 1, out
    assert f"test_waits_in_its_{phase}" in out and "is still running after 0.3 s" in out
    assert "test_comes_after PASSED" not in out
