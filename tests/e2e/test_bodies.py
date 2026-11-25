"""End to end, with a real browser: the client body and its marker.

These start a real Chromium, headless, on a throwaway profile inside a throwaway Seeress
home. They never touch the user's Chrome: looking up its address fails loudly in here, and
a test that needs the personal door points it at the throwaway body. Each test stops the
browser and the daemon it started.

Run them with:  uv run pytest -m e2e
"""

import time
from pathlib import Path

import pytest

from seeress import bodies, config
from seeress.errors import IsolationError

from .conftest import client_body

pytestmark = pytest.mark.e2e


def test_a_fresh_body_has_no_marker_and_is_accepted_once_it_has_one(home: Path) -> None:
    body = client_body()

    address = body.ensure_running(headless=True)
    fresh = bodies.attempt("client", address)

    assert address is not None
    assert fresh.readable, fresh
    assert fresh.probe is not None
    # a key that was never set is reported as absent, not as an error
    assert fresh.probe.marker is None
    assert (fresh.probe.daemon, fresh.probe.kind) == ("seeress-client", "cdp")

    bodies.ensure("client", headless=True)

    after = bodies.attempt("client", address)
    assert after.readable, after
    assert after.probe is not None
    assert after.probe.marker == body.nonce()


def test_the_marker_is_kept_when_the_browser_is_restarted(home: Path) -> None:
    body = client_body()
    bodies.ensure("client", headless=True)

    assert body.stop() is True
    deadline = time.monotonic() + 15
    while body.address() is not None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert body.address() is None

    address = body.ensure_running(headless=True)
    again = bodies.attempt("client", address)

    assert again.readable, again
    assert again.probe is not None
    assert again.probe.marker == body.nonce()


def test_the_personal_door_refuses_the_client_body(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The personal door may use a cloud model. It must not read the client browser."""
    bodies.ensure("client", headless=True)
    body = client_body()
    address = body.address()
    assert address is not None
    # stand the client body where the user's Chrome would be
    monkeypatch.setattr(config, "browser_env", lambda profile: {"BU_CDP_WS": address})

    with pytest.raises(IsolationError, match="client browser"):
        bodies.ensure("personal")


def test_the_marker_origin_is_not_a_real_site() -> None:
    """Stated where it is checked: the marker's origin can never resolve."""
    assert bodies.MARKER_URL == "http://seeress.invalid/"
