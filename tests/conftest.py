"""Shared fixtures.

Every test runs in a throwaway home directory of its own, without cloud keys, and (unless it
is marked `e2e` or `git`) without the ability to start a process or open a connection. A unit
test that tries either fails at once and names itself, instead of reaching the real browser
tool. A test marked `git` may start git and nothing else.

The browser tool's own library is given a home of its own before any test module is
imported, because two import it, and on import it reads and writes where it lives.
"""

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

CLOUD_KEYS = ("ZAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BROWSER_USE_API_KEY")
# Set by the egress check for the processes it starts. In a test it would send files to a
# folder the test cannot see.
UNSET = ("SEERESS_EGRESS_LOG", "SEERESS_PROFILE")

Patcher = Callable[[ModuleType, str, object], None]

# Where throwaway homes go. /tmp is the shortest private place on macOS and Linux; its
# resolved form is used because the operating system counts that in the socket's length.
SHORT_TMP = str(Path(tempfile.gettempdir()).resolve()) if sys.platform == "win32" else "/tmp"  # noqa: S108 - mkdtemp makes each folder inside private; /tmp is the shortest path

# The home the run started in, read before any test changes it.
HOME_THE_RUN_STARTED_IN = os.environ.get("HOME", "")


def pytest_configure(config: pytest.Config) -> None:
    """Before any test module is imported, the browser tool's library gets a home of its own.

    Two test modules import that library, and on import it makes its folders and reads the
    files of its workspace. It finds its home in BH_HOME and, when that is not set, under the
    real home. The fixture below comes too late for that: it runs per test, after the import.
    """
    folder = Path(tempfile.mkdtemp(prefix="sr-bh-", dir=SHORT_TMP)).resolve() / "browser-harness"
    os.environ["BH_HOME"] = str(folder)
    config.add_cleanup(functools.partial(shutil.rmtree, folder.parent, ignore_errors=True))


class OutsideWorldError(RuntimeError):
    """Raised when a unit test tries to start a process or open a connection."""


@pytest.fixture(autouse=True)
def seeress_home_of_its_own(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No test ever writes into the real home, asked for or not.

    Every test gets a short throwaway home of its own, and SEERESS_HOME, HOME and
    Path.home() all name it. The path is short because the browser tool's daemon listens on
    a Unix socket under the Seeress home, and the operating system caps that socket's length.

    Two tests once reached the real home because this was asked for. One left an empty
    folder in the real Seeress home. The other rewrote the LaunchAgent file of the activity
    logger: it ran `watcher.start()`, which built the path from Path.home(). A test marked
    `e2e` was exempt until 2026-11-25: the seven tests on a real browser were then run with
    HOME on an empty folder, and passed, so none is exempt now.

    seeress.watcher binds two of its paths when it is imported, from the home there was
    then, and no change of home moves them. They are bound again here for every test:
    before, one fixture line in one test module stood between six tests and the home the
    run started in.
    """
    root = Path(tempfile.mkdtemp(prefix="sr-home-", dir=SHORT_TMP)).resolve()
    fake = root / "home"
    fake.mkdir()
    monkeypatch.setenv("SEERESS_HOME", str(fake / ".seeress"))
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setattr(Path, "home", lambda: fake)
    from seeress import config, watcher  # here and not above: after the home is the test's

    monkeypatch.setattr(watcher, "WATCH_DIR", config.seeress_home() / "watch")
    monkeypatch.setattr(watcher, "PORT_FILE", fake / config.CHROME_PORT_FILE)
    yield fake
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def home(seeress_home_of_its_own: Path) -> Path:
    """The throwaway home of the test, for a test that says it runs code reading the home."""
    return seeress_home_of_its_own


@pytest.fixture
def patch(monkeypatch: pytest.MonkeyPatch) -> Patcher:
    """Replace `name` in `module` for the duration of the test.

    The module is typed as ModuleType so the checker does not require the name to be an
    exported attribute: tests routinely replace a name that a module imported from another.
    """

    def apply(module: ModuleType, name: str, value: object) -> None:
        monkeypatch.setattr(module, name, value)

    return apply


@pytest.fixture(autouse=True)
def no_cloud_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*CLOUD_KEYS, *UNSET):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def nothing_leaves_the_test_process(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse real processes and real connections.

    Tests marked `e2e` are exempt. A test marked `git` may start git, because it is
    testing git on a throwaway repository. It may start nothing else, and it still
    cannot open a connection.
    """
    node = request.node
    if node.get_closest_marker("e2e") is not None:
        return

    def refuse(*args: object, **kwargs: object) -> None:
        raise OutsideWorldError(
            f"{node.nodeid} tried to start a process or open a connection. "
            "Use a fake, or mark the test `e2e` or `git`."
        )

    if node.get_closest_marker("git") is not None:
        real = subprocess.Popen

        def git_only(args: Any, *rest: Any, **kwargs: Any) -> Any:
            first = args[0] if isinstance(args, (list, tuple)) and args else None
            if isinstance(first, str) and os.path.basename(first) == "git":
                return real(args, *rest, **kwargs)
            raise OutsideWorldError(
                f"{node.nodeid} is marked `git`: it may start git and nothing else."
            )

        monkeypatch.setattr(subprocess, "Popen", git_only)
    else:
        monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("os.system", refuse)

    import socket

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
