"""End to end: a browser of Seeress's own has no way out.

Run with:  uv run pytest -m e2e
"""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seeress import bodies
from seeress.errors import ConfigError
from seeress.runner import run_browser_command


def client_body() -> bodies.SeeressBody:
    body = bodies.body_for("client")
    assert isinstance(body, bodies.SeeressBody)
    return body


pytestmark = pytest.mark.e2e

PAGE = b"<!doctype html><title>a local page</title><h1>A local page</h1>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def site() -> Iterator[int]:
    """A page served on this machine. Its port is returned."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def go(port: int, host: str) -> str:
    address = client_body().address()
    assert address is not None
    code = f'print(goto_url("http://{host}:{port}/page"))\nwait_for_load(4)\nprint(js("document.title"))\n'
    return run_browser_command(code, profile="client", ws=address, timeout=60).stdout


def test_a_browser_of_seeress_s_own_reaches_this_machine_and_nothing_else(
    home: Path, site: int
) -> None:
    """The browser is given a proxy where nothing listens, and no site is exempt from it.

    0.0.0.0 is this machine too, but it is not on a browser's exempt list the way
    127.0.0.1 is. So the same page shows both outcomes without anything leaving the machine.
    """
    client_body().ensure_running(headless=True)

    assert "a local page" in go(site, "127.0.0.1")

    blocked = go(site, "0.0.0.0")  # noqa: S104 - an address in a URL, not a bind
    assert "ERR_PROXY_CONNECTION_FAILED" in blocked


def test_a_client_site_that_is_listed_is_reachable_again(home: Path, site: int) -> None:
    """A listed site is exempt from the route. Nothing else is: see the test above."""
    (home / "config.toml").write_text('[routing]\nclient_domains = ["0.0.0.0"]\n', encoding="utf-8")
    client_body().ensure_running(headless=True)

    assert "a local page" in go(site, "0.0.0.0")  # noqa: S104 - an address in a URL, not a bind


def test_a_settings_file_seeress_cannot_read_stops_the_browser_from_starting(home: Path) -> None:
    (home / "config.toml").write_text("[routing]\nclient_domains = [\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid TOML"):
        client_body().ensure_running(headless=True)
