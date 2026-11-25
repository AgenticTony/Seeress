"""End to end, with a real browser: the capture.

These start a real Chromium, headless, on a throwaway profile inside a throwaway Seeress
home, and the capture on a connection of its own to it. Each test stops the browser. They
never touch the user's Chrome: looking up its address fails loudly in the fixture.

Run them with:  .venv/bin/python -m pytest -m e2e
"""

import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seeress import activity, capture

from .cdp import Browser, until
from .conftest import client_body

pytestmark = pytest.mark.e2e

TITLE = "A page whose title must never be written"
QUERY = "a-query-that-must-never-be-written"
TYPED = "Qx7-violet-orchid-9Z"

PAGE = f"""<!doctype html>
<html><head><title>{TITLE}</title><meta charset="utf-8"></head>
<body>
<h1>A page for the capture</h1>
<form id="signup" onsubmit="return false">
  <input name="query" type="text">
  <input name="secret" type="password">
  <input name="code" autocomplete="one-time-code">
  <input name="card" autocomplete="cc-number">
  <input name="many" autocomplete="section-pay cc-csc">
  <input name="field-4711">
  <select name="country"><option value="se">Sweden</option>
  <option value="dk">Denmark</option></select>
  <button type="button" data-testid="save">Save</button>
  <button type="submit">Send</button>
</form>
<div id="box"><span id="item-4711">x</span><button id="btn-9">Plain</button></div>
<my-widget><span>inside a custom element</span></my-widget>
<iframe src="/frame" title="a frame"></iframe>
<script>
  window.whatThePageSees = () =>
    typeof window.seeressEvent + " and " + typeof window.__seeressListening;
</script>
</body></html>"""

FRAME = """<!doctype html><title>A frame</title>
<button data-testid="in-frame">In a frame</button>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        page = FRAME.encode("utf-8") if self.path.startswith("/frame") else PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def site() -> Iterator[str]:
    """Pages served on this machine. The origin is returned."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def written_into(home: Path) -> Callable[[activity.Event], None]:
    """What the capture gives its events to: the record under this home."""

    def emit(event: activity.Event) -> None:
        activity.write_event(home, event)

    return emit


class Recording:
    """The capture on a browser Seeress started, writing into the Seeress home of the test."""

    def __init__(self, address: str, home: Path) -> None:
        self.home = home
        self.recorder = capture.Recorder(address, written_into(home))

    def start(self) -> None:
        self.recorder.start()

    def events(self) -> list[activity.Event]:
        return activity.read_events(self.home)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events()]

    def of_kind(self, kind: str) -> list[activity.Event]:
        return [event for event in self.events() if event.kind == kind]

    def written(self) -> str:
        """All that the capture wrote, as text."""
        return "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((self.home / "watch").glob("activity-*.jsonl"))
        )

    def stop(self) -> None:
        self.recorder.stop()


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


def record_on(home: Path, address: str) -> Recording:
    """The capture started on the browser, once its first tab is listened to."""
    recording = Recording(address, home)
    recording.start()
    return recording


def parts_of(text: str, least: int = 4) -> list[str]:
    """Every run of least characters or more in text."""
    return [
        text[start:end] for start in range(len(text)) for end in range(start + least, len(text) + 1)
    ]


def nothing_of(typed: str, written: str) -> None:
    """No part of what was typed, four characters or more, is in what was written."""
    assert typed not in written
    leaks = [part for part in parts_of(typed) if part in written]
    assert leaks == [], f"{leaks[:3]} of what was typed is in what was written"


def waited_a_moment() -> None:
    """Long enough for an event that was sent to arrive. Nothing else."""
    time.sleep(0.6)


# ------------------------------------------------------------------- the plan ---


def test_a_click_in_a_page_is_an_event_with_the_site_and_the_path(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/one/two?token={QUERY}#top")
    try:
        tab.wait_loaded()
        tab.click('[data-testid="save"]')
        (event,) = until(lambda: recording.of_kind("click"), "a click event", 10)

        assert (event.site, event.path) == ("127.0.0.1", "/one/two")
        assert event.target == tab.id[:8]
        assert event.action == activity.Action(
            "click", "button", 'button[data-testid="save"]', None
        )
        written = recording.written()
        nothing_of(QUERY, written)
        assert TITLE not in written
        assert [path.name for path in (home / "watch").iterdir()] == [
            f"activity-{time.strftime('%Y%m%d')}.jsonl"
        ]
    finally:
        tab.close()
        recording.stop()


def test_what_a_person_types_is_in_no_event(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.type_text(TYPED, 'input[name="query"]')
        (event,) = until(lambda: recording.of_kind("input"), "an input event", 10)

        assert event.action is not None and event.action.field == "text"
        nothing_of(TYPED, recording.written())
    finally:
        tab.close()
        recording.stop()


def test_a_page_in_a_second_context_of_the_browser_is_not_captured(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    context = own.private_context()
    private = own.tab(f"{site}/private", context=context)
    open_page = own.tab(f"{site}/open")
    try:
        private.wait_loaded()
        private.click('[data-testid="save"]')
        waited_a_moment()

        open_page.wait_loaded()
        open_page.click('[data-testid="save"]')
        until(
            lambda: len(recording.of_kind("click")) == 1,
            "the click in the page of the default context",
            10,
        )

        assert [event.path for event in recording.events()] == ["/open"]
    finally:
        open_page.close()
        private.close()
        own.call("Target.disposeBrowserContext", {"browserContextId": context})
        recording.stop()


# ------------------------------------------------------- what the listener does ---


def test_a_click_inside_a_frame_is_not_the_person_s_click(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        in_frame = (
            'document.querySelector("iframe").contentDocument'
            ".querySelector('[data-testid=\"in-frame\"]')"
        )
        until(lambda: tab.run(f"!!{in_frame}"), "the frame to have loaded", 10)
        tab.run(f"{in_frame}.click()")
        waited_a_moment()

        tab.click('[data-testid="save"]')
        until(lambda: len(recording.of_kind("click")) == 1, "the click in the page", 10)
        waited_a_moment()

        assert [event.action and event.action.fingerprint for event in recording.events()] == [
            'button[data-testid="save"]'
        ]
    finally:
        tab.close()
        recording.stop()


@pytest.mark.parametrize(
    "selector, field",
    [
        pytest.param('input[name="secret"]', "password", id="a password"),
        pytest.param('input[name="code"]', "one-time-code", id="a one-time code"),
        pytest.param('input[name="card"]', "card", id="a card number"),
        pytest.param('input[name="many"]', "card", id="a card code, named after another token"),
        pytest.param('input[name="query"]', "text", id="text"),
    ],
)
def test_the_kind_of_a_field_is_what_the_page_says_it_is_and_its_content_is_never_read(
    home: Path, browser: tuple[Browser, str], site: str, selector: str, field: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.type_text(TYPED, selector)
        (event,) = until(lambda: recording.of_kind("input"), f"an input event for {selector}", 10)

        assert event.action is not None and event.action.field == field
        nothing_of(TYPED, recording.written())
    finally:
        tab.close()
        recording.stop()


def test_the_digits_of_a_name_are_replaced_and_an_id_with_digits_is_no_name(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.type_text(TYPED, 'input[name="field-4711"]')
        tab.click("#btn-9")
        until(lambda: recording.kinds() == ["input", "click"], "an input and a click", 10)

        named, placed = (event.action for event in recording.events())
        assert named is not None and named.fingerprint == 'input[name="field-####"]'
        assert placed is not None
        assert placed.fingerprint == (
            "html:nth-child(1)>body:nth-child(2)>div:nth-child(3)>button:nth-child(2)"
        )
    finally:
        tab.close()
        recording.stop()


def test_a_click_on_a_custom_element_is_a_click_on_an_element(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.click("my-widget")
        (event,) = until(lambda: recording.of_kind("click"), "a click event", 10)

        assert event.action is not None
        assert event.action.tag == "my-widget"
        assert (
            event.action.fingerprint == "html:nth-child(1)>body:nth-child(2)>my-widget:nth-child(4)"
        )
    finally:
        tab.close()
        recording.stop()


def test_an_input_is_reported_once_per_visit_of_a_field(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.type_text("first", 'input[name="query"]')
        until(lambda: len(recording.of_kind("input")) == 1, "the first input", 10)

        tab.type_text("second", 'input[name="query"]')
        waited_a_moment()
        assert len(recording.of_kind("input")) == 1

        tab.leave('input[name="query"]')
        tab.type_text("third", 'input[name="query"]')
        until(
            lambda: len(recording.of_kind("input")) == 2,
            "the input after the field lost the focus",
            10,
        )
    finally:
        tab.close()
        recording.stop()


def test_a_select_is_reported_once_as_a_select(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.run(
            "const select = document.querySelector('select'); select.selectedIndex = 1;"
            " select.dispatchEvent(new Event('input', {bubbles: true}));"
            " select.dispatchEvent(new Event('change', {bubbles: true}));"
        )
        (event,) = until(lambda: recording.of_kind("select"), "a select event", 10)
        waited_a_moment()

        assert recording.kinds() == ["select"]
        assert event.action == activity.Action("select", "select", 'select[name="country"]', None)
    finally:
        tab.close()
        recording.stop()


def test_a_submit_is_reported(home: Path, browser: tuple[Browser, str], site: str) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.run("document.querySelector('form').requestSubmit()")
        (event,) = until(lambda: recording.of_kind("submit"), "a submit event", 10)

        assert event.action == activity.Action("submit", "form", 'form[id="signup"]', None)
    finally:
        tab.close()
        recording.stop()


def test_the_page_s_own_scripts_can_neither_see_the_listener_nor_report_in_its_place(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.click('[data-testid="save"]')
        until(lambda: recording.of_kind("click"), "a click, which shows the listener is in", 10)

        assert tab.run("window.whatThePageSees()") == "undefined and undefined"
        forged = tab.run(
            "(() => { try { window.seeressEvent(JSON.stringify("
            "{type: 'input', tag: 'input', fingerprint: 'forged', field: 'text'}));"
            " return 'called'; } catch (error) { return error.name; } })()"
        )
        assert forged == "TypeError"
        tab.click('[data-testid="save"]')
        until(lambda: len(recording.of_kind("click")) == 2, "the second click", 10)

        assert recording.kinds() == ["click", "click"]
        assert "forged" not in recording.written()
    finally:
        tab.close()
        recording.stop()


# ------------------------------------------------------------- the connection ---


def test_a_page_open_before_the_capture_started_is_listened_to(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        recording = record_on(home, address)

        def a_click() -> list[activity.Event]:
            tab.click('[data-testid="save"]')
            return recording.of_kind("click")

        until(a_click, "a click on a page that was already open", 10)
    finally:
        tab.close()
        if "recording" in locals():
            recording.stop()


def test_a_navigation_keeps_the_listener(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.click('[data-testid="save"]')
        until(lambda: len(recording.of_kind("click")) == 1, "the click on the first page", 10)

        tab.go(f"{site}/second")
        tab.click('[data-testid="save"]')
        until(lambda: len(recording.of_kind("click")) == 2, "the click on the second page", 10)

        assert [event.path for event in recording.events()] == ["/", "/second"]
    finally:
        tab.close()
        recording.stop()


def test_stopping_the_capture_ends_its_thread_and_the_page_is_listened_to_no_more(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    recording.start()  # a second start starts nothing more
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        alive = recording.recorder.running
        assert alive is True

        recording.stop()

        assert recording.recorder.running is False
        assert recording.recorder.error is None
        tab.click('[data-testid="save"]')
        waited_a_moment()
        assert recording.events() == []
    finally:
        tab.close()
        recording.stop()  # and a second stop is nothing


def test_an_address_where_nothing_listens_ends_the_capture_with_the_error(
    home: Path, browser: tuple[Browser, str]
) -> None:
    recorder = capture.Recorder("ws://127.0.0.1:9/devtools/browser/none", lambda event: None)
    recorder.start()

    until(lambda: recorder.error, "the error of a capture that could not connect", 10)

    assert isinstance(recorder.error, OSError)
    assert recorder.running is False
    recorder.stop()


def test_an_event_that_cannot_be_written_ends_the_capture_and_keeps_the_error(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A capture that cannot write ends, and says so, instead of going on and recording
    nothing."""
    own, address = browser
    (home / "watch").write_text("a file where the folder should be", encoding="utf-8")
    recorder = capture.Recorder(address, written_into(home))
    recorder.start()
    tab = own.tab(f"{site}/")
    try:

        def a_click() -> BaseException | None:
            tab.click('[data-testid="save"]')
            return recorder.error

        until(a_click, "the error of a capture that could not write", 10)
    finally:
        tab.close()
        recorder.stop()

    assert isinstance(recorder.error, OSError)
    assert recorder.running is False


# ---------------------------------------------------- what is kept of nothing ---


def test_the_title_of_a_page_is_nowhere_in_what_the_capture_wrote(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/search?q={QUERY}#result", context=None)
    try:
        tab.wait_loaded()
        tab.type_text(TYPED, 'input[name="query"]')
        tab.click('[data-testid="save"]')
        tab.run("document.querySelector('form').requestSubmit()")
        until(lambda: len(recording.events()) == 3, "an input, a click and a submit", 10)
    finally:
        tab.close()
        recording.stop()

    written = "".join(path.read_text(encoding="utf-8") for path in (home / "watch").iterdir())
    assert TITLE not in written
    assert QUERY not in written
    nothing_of(TYPED, written)
