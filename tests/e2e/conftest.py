"""Shared by the end-to-end tests: a short throwaway Seeress home, and the client body."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from seeress import bodies, config

from .cdp import Browser


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway Seeress home, in a short path.

    The browser tool's daemon listens on a Unix socket whose path the operating system
    caps, so the usual pytest folder is too deep for it. The user's own Chrome can never be
    looked up from a test that uses this fixture: looking it up fails loudly.
    """
    root = Path(tempfile.mkdtemp(prefix="sr-e2e-", dir="/tmp")).resolve()
    folder = root / "home"
    folder.mkdir()
    monkeypatch.setenv("SEERESS_HOME", str(folder))

    def the_user_s_own_chrome(profile: str) -> dict[str, str]:
        raise AssertionError("this test must not attach to the user's own Chrome")

    monkeypatch.setattr(config, "browser_env", the_user_s_own_chrome)
    yield folder
    body = bodies.body_for("client")
    if isinstance(body, bodies.SeeressBody):
        body.stop()
    for door in ("client", "personal"):
        bodies.reload_daemon(door, None)
    shutil.rmtree(root, ignore_errors=True)


def client_body() -> bodies.SeeressBody:
    body = bodies.body_for("client")
    assert isinstance(body, bodies.SeeressBody)
    return body


@pytest.fixture
def browser(home: Path) -> Iterator[tuple[Browser, str]]:
    """A browser Seeress started, and a connection of the test's own to it."""
    address = client_body().ensure_running(headless=True)
    assert address is not None
    own = Browser(address)
    try:
        yield own, address
    finally:
        own.close()
