"""End to end, with a real browser: the capture.

These start a real Chromium, headless, on a throwaway profile inside a throwaway Seeress
home, and the capture on a connection of its own to it. Each test stops the browser. They
never touch the user's Chrome: looking up its address fails loudly in the fixture.

Run them with:  .venv/bin/python -m pytest -m e2e
"""

import gc
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seeress import activity, bodies, capture
from seeress.errors import CaptureError

from .cdp import Browser, listened, until
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
        listened(tab)
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
        listened(tab)
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

        listened(open_page)
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
        listened(tab)
        in_frame = (
            'document.querySelector("iframe").contentDocument'
            ".querySelector('[data-testid=\"in-frame\"]')"
        )
        until(lambda: tab.run(f"!!{in_frame}"), "the frame to have loaded", 10)
        x, y = tab.run(
            "(() => { const outer = document.querySelector('iframe').getBoundingClientRect();"
            f" const inner = {in_frame}.getBoundingClientRect();"
            " return [outer.left + inner.left + inner.width / 2,"
            " outer.top + inner.top + inner.height / 2]; })()"
        )
        tab.click_at(x, y)  # a mouse's click, on the button that is in the frame
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
        listened(tab)
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
        listened(tab)
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
        listened(tab)
        # The element itself is clicked, on padding of its own: a mouse that clicked in the
        # middle of it would click the text inside it.
        x, y = tab.run(
            "(() => { const w = document.querySelector('my-widget'); w.style.padding = '12px';"
            " const r = w.getBoundingClientRect(); return [r.left + 4, r.top + 4]; })()"
        )
        tab.click_at(x, y)
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
        listened(tab)
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
        listened(tab)
        tab.pick("select", "d")  # for Denmark, as a keyboard chooses
        (event,) = until(lambda: recording.of_kind("select"), "a select event", 10)
        waited_a_moment()

        assert recording.kinds() == ["select"]  # and once: not the input a select also fires
        assert event.action == activity.Action("select", "select", 'select[name="country"]', None)
    finally:
        tab.close()
        recording.stop()


def test_a_submit_is_reported(home: Path, browser: tuple[Browser, str], site: str) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.click('button[type="submit"]')  # as a person submits a form: its button is clicked
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
        listened(tab)
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
        tab.wait_loaded()  # open before the capture is, which is the point
        recording = record_on(home, address)
        listened(tab)

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
        listened(tab)
        tab.click('[data-testid="save"]')
        until(lambda: len(recording.of_kind("click")) == 1, "the click on the first page", 10)

        tab.go(f"{site}/second")
        listened(tab)
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
        listened(tab)
        tab.click('[data-testid="save"]')
        (heard,) = until(lambda: recording.of_kind("click"), "a click before the stop", 10)
        alive = recording.recorder.running
        assert alive is True

        recording.stop()

        assert recording.recorder.running is False
        assert recording.recorder.error is None
        tab.click('[data-testid="save"]')
        waited_a_moment()
        assert recording.events() == [heard]  # the one from before, and no more
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


# ---------------------------------------------------------------- the marker ---


def test_a_browser_that_carries_the_marker_is_attached_to_nothing_and_the_capture_ends(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """The plan's test. The marker is planted as the product plants it: with the browser
    tool, through bodies.ensure, on the browser Seeress started for the client door."""
    own, address = browser
    bodies.ensure("client", headless=True)
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        until(lambda: recording.recorder.error, "the capture to end", 10)

        error = recording.recorder.error
        assert isinstance(error, CaptureError)
        assert "This browser is Seeress's own: it carries the marker." in str(error)
        tab.wait_loaded()  # and listens nowhere: no tab here waits for the capture
        tab.click('[data-testid="save"]')
        tab.type_text(TYPED, 'input[name="query"]')
        waited_a_moment()
        assert recording.events() == []
        assert recording.recorder.running is False
    finally:
        tab.close()
        recording.stop()


def test_the_capture_takes_no_notice_of_any_cookie_but_the_marker(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A cookie of another name, for another site, is nobody's business. It is not in the
    way of the capture, and nothing of it is written."""
    own, address = browser
    tab = own.tab(f"{site}/")
    tab.wait_loaded()
    tab.call(
        "Network.setCookie",
        {"name": "a-personal-cookie", "value": "something-of-the-person", "url": f"{site}/"},
    )
    tab.close()
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)

        def a_click() -> list[activity.Event]:
            tab.click('[data-testid="save"]')
            return recording.of_kind("click")

        until(a_click, "a click", 10)
        waited_a_moment()

        assert recording.recorder.error is None
        assert "a-personal-cookie" not in recording.written()
        assert "something-of-the-person" not in recording.written()
    finally:
        tab.close()
        recording.stop()


# ---------------------------------------------------- what is kept of nothing ---


def test_a_cookie_a_page_sets_with_the_marker_s_name_is_no_marker(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A page may name a cookie of its own site as the marker is named. The marker is set at
    seeress.invalid and nowhere else. Before, any cookie so named ended the capture, and a
    logger on a person's own Chrome tried again every five seconds, and ended again."""
    own, address = browser
    tab = own.tab(f"{site}/")
    recording: Recording | None = None
    try:
        tab.wait_loaded()
        tab.run("document.cookie = 'seeress_body=1; Max-Age=31536000; Path=/'")
        recording = record_on(home, address)
        listened(tab)
        tab.click('[data-testid="save"]')
        heard = until(
            lambda: recording.of_kind("click") or recording.recorder.error,
            "a click in the page, with such a cookie in the jar",
            10,
        )
    finally:
        tab.close()
        if recording is not None:
            recording.stop()

    assert recording.recorder.error is None, recording.recorder.error
    assert len(heard) == 1  # the click was recorded: the capture went on
    assert recording.events() == heard


def test_the_title_of_a_page_is_nowhere_in_what_the_capture_wrote(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/search?q={QUERY}#result", context=None)
    try:
        listened(tab)
        tab.type_text(TYPED, 'input[name="query"]')
        tab.click('[data-testid="save"]')
        tab.click('button[type="submit"]')
        until(lambda: len(recording.events()) == 4, "an input, two clicks and a submit", 10)
    finally:
        tab.close()
        recording.stop()

    written = "".join(path.read_text(encoding="utf-8") for path in (home / "watch").iterdir())
    assert TITLE not in written
    assert QUERY not in written
    nothing_of(TYPED, written)


# ------------------------------------------------------ what is kept, and the status ---


def said(home: Path) -> dict[str, object]:
    """What the capture last said of itself under this home, and nothing before it has."""
    try:
        found: dict[str, object] = json.loads(
            (home / "watch" / "capture-status.json").read_text(encoding="utf-8")
        )
        return found
    except (OSError, ValueError):
        return {}


def a_click_on(day: str) -> activity.Event:
    """A click at ten in the morning of that day, as 2026-10-25."""
    return activity.Event(1, f"{day}T10:00:00", "click", "example.org", "/", "1A2B3C4D", None)


def test_a_capture_on_an_address_where_nothing_listens_says_it_is_down_and_why(
    home: Path,
) -> None:
    recorder = capture.Recorder(
        "ws://127.0.0.1:9/devtools/browser/none", lambda event: None, status=capture.Status(home)
    )
    recorder.start()
    try:
        until(lambda: recorder.error, "the error of a capture that could not connect", 10)
    finally:
        recorder.stop()

    report = capture.status(home)

    assert report.startswith("Capture is down since ")
    assert "ConnectionRefusedError" in report
    assert report.endswith("Is remote debugging on in Chrome? chrome://inspect/#remote-debugging")


def test_a_capture_that_is_recording_says_since_when_and_what_it_has_written(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """The count is written as the events come here, so that the test need not wait: every
    is nothing. The schedule is the whole day, whatever the hour the test runs at."""
    own, address = browser
    status = capture.Status(home, every=0)
    gate = capture.Gate(home, schedule=activity.Schedule(frozenset(range(7)), 0, 23), status=status)
    recorder = capture.Recorder(address, gate, status=status)
    recorder.start()
    tab = own.tab(f"{site}/")
    try:
        until(lambda: said(home).get("state") == "connected", "the capture to be recording", 10)
        since = said(home)["since"]
        listened(tab)
        tab.click('[data-testid="save"]')
        until(lambda: said(home)["events"] == 1, "the capture to count the click", 10)
        count, last = said(home)["events"], said(home)["last_event"]
        events = activity.read_events(home)
        recording = capture.status(home)
    finally:
        tab.close()
        recorder.stop()

    assert len(events) == count
    assert events[-1].ts == last
    assert recording == (
        f"Capture is recording since {since} (pid {os.getpid()}):"
        f" {count} event{'' if count == 1 else 's'}, the last at {last}."
    )
    assert recorder.error is None
    assert capture.status(home) == (
        f"Capture is not running. It last reported stopped at {said(home)['since']}."
    )


def test_what_is_kept_is_done_as_soon_as_the_capture_connects_and_then_not_at_once(
    home: Path, browser: tuple[Browser, str]
) -> None:
    """Again comes every so often: after an hour by default, and not before."""
    kept: list[int] = []
    recorder = capture.Recorder(browser[1], lambda event: None, keep=lambda: kept.append(len(kept)))
    recorder.start()
    try:
        until(lambda: kept, "what is kept to be done as the capture connects", 10)
        time.sleep(0.3)
    finally:
        recorder.stop()

    assert kept == [0]
    assert recorder.error is None


def test_what_is_kept_is_done_again_every_so_often(
    home: Path, browser: tuple[Browser, str]
) -> None:
    kept: list[int] = []
    recorder = capture.Recorder(
        browser[1], lambda event: None, keep=lambda: kept.append(len(kept)), every=0.05
    )
    recorder.start()
    try:
        until(lambda: len(kept) >= 3, "what is kept to be done again and again", 10)
    finally:
        recorder.stop()

    assert kept[:3] == [0, 1, 2]
    assert recorder.error is None


def test_what_is_past_the_days_kept_is_deleted_as_the_capture_connects(
    home: Path, browser: tuple[Browser, str]
) -> None:
    """Thirty days are kept, and today is 25 November 2026. A click of 26 October is thirty
    days old and stays; one of 25 October is older and goes."""
    old = activity.write_event(
        home, a_click_on("2026-10-25"), now=activity.epoch("2026-10-25T10:00:00")
    )
    kept = activity.write_event(
        home, a_click_on("2026-10-26"), now=activity.epoch("2026-10-26T10:00:00")
    )

    def keep() -> None:
        capture.retention(home, 30, today=date(2026, 11, 25))

    recorder = capture.Recorder(browser[1], lambda event: None, keep=keep)
    recorder.start()
    try:
        until(lambda: not old.exists(), "what is past the days kept to be deleted", 10)
    finally:
        recorder.stop()

    assert kept.exists()
    assert recorder.error is None


def test_a_fault_of_the_capture_s_own_is_kept_and_said(
    home: Path, browser: tuple[Browser, str], site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is not one of the capture's errors is a fault. It is kept as the error of the
    capture and nothing is raised on the thread, and the status says the capture failed,
    and why."""
    own, address = browser
    raised: list[object] = []
    monkeypatch.setattr(threading, "excepthook", raised.append)

    def a_fault(event: activity.Event) -> None:
        raise RuntimeError("a fault of the capture's own")

    recorder = capture.Recorder(address, a_fault, status=capture.Status(home))
    recorder.start()
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.click('[data-testid="save"]')
        until(lambda: recorder.error, "the fault to be kept as the error of the capture", 10)
    finally:
        tab.close()
        recorder.stop()

    assert raised == []  # nothing raised on the thread
    assert str(recorder.error) == "a fault of the capture's own"
    assert capture.status(home) == (
        f"Capture is not running. It last reported failed at {said(home)['since']}:"
        " RuntimeError: a fault of the capture's own."
    )


def test_a_status_that_cannot_be_written_hides_no_error(home: Path) -> None:
    """The folder of the record is a file here, so nothing can be said. What ended the
    capture is the error raised all the same, and not the failure to say it."""
    (home / "watch").write_text("a file where the folder should be", encoding="utf-8")
    recorder = capture.Recorder(
        "ws://127.0.0.1:9/devtools/browser/none", lambda event: None, status=capture.Status(home)
    )
    recorder.start()
    try:
        until(lambda: recorder.error, "the error of a capture that could not connect", 10)
    finally:
        recorder.stop()

    assert type(recorder.error).__name__ == "ConnectionRefusedError"


def a_capture_whose_keeping_failed(address: str, home: Path) -> str:
    """Run a capture whose keeping of the record fails at once. Returns what the status says
    of it, and holds nothing of the capture: what it left behind is free to be destroyed."""

    def keep() -> None:
        raise OSError("the record could not be deleted")

    recorder = capture.Recorder(address, lambda event: None, keep=keep, status=capture.Status(home))
    recorder.start()
    try:
        until(lambda: recorder.error, "the error of what could not be kept", 10)
    finally:
        recorder.stop()

    assert str(recorder.error) == "the record could not be deleted"
    return capture.status(home)


def test_a_failure_of_what_is_kept_ends_the_capture_and_says_why(
    home: Path, browser: tuple[Browser, str], caplog: pytest.LogCaptureFixture
) -> None:
    """What keeps the record is done on the loop of the capture. A failure there is not
    hidden: the capture ends with it, and the status says why. And the capture leaves
    nothing behind it unread: the answer it was waiting for is given up, and read."""
    report = a_capture_whose_keeping_failed(browser[1], home)
    assert report.startswith("Capture is down since ")
    assert "OSError: the record could not be deleted" in report

    gc.collect()  # what the capture left behind is destroyed here, and says so if unread
    complained = [
        record.getMessage()
        for record in caplog.records
        if record.name == "asyncio" and "Task" in record.getMessage()
    ]
    assert complained == []


def test_nothing_is_kept_by_a_capture_that_cannot_connect(home: Path) -> None:
    """What is kept is done once the capture has connected, and not before."""
    kept: list[int] = []
    recorder = capture.Recorder(
        "ws://127.0.0.1:9/devtools/browser/none", lambda event: None, keep=lambda: kept.append(0)
    )
    recorder.start()
    try:
        until(lambda: recorder.error, "the error of a capture that could not connect", 10)
    finally:
        recorder.stop()

    assert kept == []


# --------------------------------------------------------- what the sixth review found ---


def test_a_fingerprint_is_cut_whole_and_never_inside_a_character(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A name whose two hundredth and two hundred and first units of sixteen bits are an
    emoji: `button[data-testid="` takes twenty, and a hundred and seventy-nine letters take
    what is left before it. Cut at two hundred in the page, the name would end in half of
    the emoji, and no file could hold the line it was written on."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "(() => { const b = document.createElement('button');"
            " b.setAttribute('data-testid', 'a'.repeat(179) + '\\u{1F600}' + 'tail');"
            " document.body.appendChild(b); })()"
        )
        tab.click(f'[data-testid^="{"a" * 179}"]')
        found = until(
            lambda: recording.of_kind("click") or recording.recorder.error,
            "a click on the element with the long name",
            10,
        )
    finally:
        tab.close()
        recording.stop()

    assert recording.recorder.error is None, recording.recorder.error
    (event,) = found
    assert len(event.action.fingerprint) == 200
    assert event.action.fingerprint.endswith("\U0001f600")
    written = recording.written()
    assert "\U0001f600" in written  # whole
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in written)  # and no half of one


def test_the_browser_names_its_default_context_and_puts_its_tabs_in_it(
    home: Path, browser: tuple[Browser, str]
) -> None:
    """The capture listens to a tab only when the browser puts it in the context it names as
    its default one. What the real browser answers is held here, as it is."""
    own, _ = browser

    contexts = own.call("Target.getBrowserContexts")
    default = contexts.get("defaultBrowserContextId")

    assert isinstance(default, str) and default
    pages = [
        info for info in own.call("Target.getTargets")["targetInfos"] if info["type"] == "page"
    ]
    assert pages
    assert all(info.get("browserContextId") == default for info in pages)


def test_one_large_message_from_a_page_does_not_end_the_capture(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """Every tab has its runtime on, so what a page says to its console comes to the
    capture. A message over the old bound of four mebibytes ended the connection, and every
    tab with it."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        tab.wait_loaded()
        tab.run("console.log('x'.repeat(5 * 2**20))")
        waited_a_moment()
        tab.click('[data-testid="save"]')
        found = until(
            lambda: recording.of_kind("click") or recording.recorder.error,
            "a click after a large message from the page",
            10,
        )
    finally:
        tab.close()
        recording.stop()

    assert recording.recorder.error is None, recording.recorder.error
    (event,) = found
    assert event.kind == "click"


# ------------------------------------------------- only what a person does is theirs ---


def test_a_click_a_page_s_script_makes_is_not_taken_for_a_person_s(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A page can say that a click happened. The browser does not trust such an event, and
    neither does the capture: it would let a page write what it chose, at any rate."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        for _ in range(5):
            tab.click_by_script('[data-testid="save"]')
        waited_a_moment()
        assert recording.events() == []

        tab.click('[data-testid="save"]')  # the browser's own click, as a mouse makes it
        (event,) = until(lambda: recording.of_kind("click"), "a click event", 10)
    finally:
        tab.close()
        recording.stop()

    assert event.action == activity.Action("click", "button", 'button[data-testid="save"]', None)


def test_an_input_a_page_s_script_makes_is_not_taken_for_a_person_s(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "for (const field of document.querySelectorAll('input')) {"
            " field.value = 'written by a script';"
            " field.dispatchEvent(new Event('input', {bubbles: true})); }"
        )
        waited_a_moment()
        assert recording.events() == []

        tab.type_text(TYPED, 'input[name="query"]')
        (event,) = until(lambda: recording.of_kind("input"), "an input event", 10)
    finally:
        tab.close()
        recording.stop()

    assert (event.action.tag, event.action.field) == ("input", "text")


def test_a_change_a_page_s_script_makes_on_a_select_is_not_taken_for_a_person_s(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "const select = document.querySelector('select'); select.selectedIndex = 1;"
            " select.dispatchEvent(new Event('input', {bubbles: true}));"
            " select.dispatchEvent(new Event('change', {bubbles: true}));"
        )
        waited_a_moment()
        assert recording.events() == []

        tab.pick("select", "s")  # back to Sweden, as a keyboard chooses: a change that is real
        (event,) = until(lambda: recording.of_kind("select"), "a select event", 10)
    finally:
        tab.close()
        recording.stop()

    assert event.action == activity.Action("select", "select", 'select[name="country"]', None)


def test_a_lone_surrogate_in_a_name_does_not_end_the_capture(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """An attribute may hold an unpaired surrogate, and no file can hold one. Before, the
    write of the click raised UnicodeEncodeError, and that ended the capture in every tab:
    a page could end it with one attribute. The name is made whole in the page, and the
    click is recorded."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "(() => { const b = document.createElement('button'); b.id = 'surrogate';"
            " b.setAttribute('data-testid', 'probe-\\ud800-end');"
            " document.body.appendChild(b); })()"
        )
        tab.click("#surrogate")
        heard = until(
            lambda: recording.of_kind("click") or recording.recorder.error,
            "a click on the element so named",
            10,
        )
    finally:
        tab.close()
        recording.stop()

    assert recording.recorder.error is None, repr(recording.recorder.error)
    assert len(heard) == 1
    assert heard[0].action is not None
    assert heard[0].action.fingerprint == 'button[data-testid="probe-\ufffd-end"]'
    assert heard[0].action.fingerprint.encode("utf-8")  # and so a file can hold it


def test_a_long_address_or_a_long_tag_makes_no_line_over_its_bound(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A page chooses the address of its own tab and the name of its own element. Before, one
    click wrote a line of 2 MB, and at 200 events a minute that was 25 GB an hour from one
    tab. The path is cut at 2,048 characters and the tag at 100, and the click is kept."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/" + "a" * 3000)  # a page's own long address, by a navigation
    try:
        listened(tab)
        tab.run(
            "(() => { const b = document.createElement('x-' + 'a'.repeat(5000));"
            " b.id = 'long'; b.textContent = 'long'; document.body.appendChild(b); })()"
        )
        tab.click("#long")
        heard = until(
            lambda: recording.of_kind("click") or recording.recorder.error,
            "a click on the element so named",
            10,
        )
    finally:
        tab.close()
        recording.stop()

    assert recording.recorder.error is None, repr(recording.recorder.error)
    assert len(heard) == 1
    assert heard[0].path == "/" + "a" * 2047
    assert heard[0].action is not None
    assert heard[0].action.tag == "x-" + "a" * 98
    assert all(len(event.path) <= 2048 for event in recording.events())


def test_prose_is_no_name_for_an_element(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """An aria-label says something, often about a person, and a page writes what it likes
    in one. So it is not read. An element that has no other name is told apart by its
    place, and the prose is nowhere in what was written."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "(() => { const b = document.createElement('button');"
            " b.setAttribute('aria-label', 'Order 4711 for Anna: paid');"
            " document.body.appendChild(b); })()"
        )
        tab.click("button[aria-label]")
        (event,) = until(lambda: recording.of_kind("click"), "a click on the unnamed element", 10)
    finally:
        tab.close()
        recording.stop()

    assert event.action is not None
    assert event.action.tag == "button"
    placed = event.action.fingerprint
    assert placed.startswith("html:nth-child(1)>body:nth-child(2)>button:nth-child(")
    written = recording.written()
    for word in ("Order", "4711", "Anna", "paid", "aria"):
        assert word not in written


# -------------------------------------------------- a page cannot silence the capture ---


def test_a_page_that_stops_its_events_going_further_cannot_silence_the_capture(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """A page may stop every event at the window, so that nothing lower down hears it. The
    capture's listener sits on the window too, and hears first."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "for (const type of ['click', 'input', 'change', 'submit'])"
            " window.addEventListener(type, (event) => event.stopPropagation(), true);"
        )
        tab.click('[data-testid="save"]')
        (event,) = until(lambda: recording.of_kind("click"), "a click event", 10)
    finally:
        tab.close()
        recording.stop()

    assert event.action == activity.Action("click", "button", 'button[data-testid="save"]', None)


def test_the_capture_hears_again_after_a_page_opens_its_document_again(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """Opening a document again takes away every listener of its window and keeps the
    world the listener ran in. A script that writes to a loaded page does this, and the
    tab would have been deaf until it navigated."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        for opening in (1, 2):
            tab.run(
                "document.open(); document.write('<button data-testid=\"save\">Save</button>');"
                " document.close();"
            )
            waited_a_moment()
            tab.click('[data-testid="save"]')
            until(
                lambda opening=opening: len(recording.of_kind("click")) == opening,
                f"click number {opening}, after reopening number {opening}",
                10,
            )
        one, two = recording.of_kind("click")
    finally:
        tab.close()
        recording.stop()

    assert [event.action for event in (one, two)] == [
        activity.Action("click", "button", 'button[data-testid="save"]', None)
    ] * 2


def test_an_element_that_has_an_id_and_a_test_id_is_named_by_its_test_id(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """Of the attributes that may name an element, the first that is there names it, always
    in the same order: a test id, then an id, then a name."""
    own, address = browser
    recording = record_on(home, address)
    tab = own.tab(f"{site}/")
    try:
        listened(tab)
        tab.run(
            "(() => { const b = document.createElement('button'); b.id = 'the-one';"
            " b.setAttribute('data-testid', 'the-test'); document.body.appendChild(b); })()"
        )
        tab.click("#the-one")
        (event,) = until(lambda: recording.of_kind("click"), "a click on the element", 10)
    finally:
        tab.close()
        recording.stop()

    assert event.action == activity.Action(
        "click", "button", 'button[data-testid="the-test"]', None
    )


# ---------------------------------------------------------------- the logger ---


def test_python_m_seeress_capture_is_the_logger(
    home: Path, browser: tuple[Browser, str], site: str
) -> None:
    """The entry point as a process, on the address of a browser Seeress started and a home
    of its own: it records a click, and stops when its time is up, and says what it wrote.
    The home's settings say all day, so that the hour the test runs at is nothing to it."""
    own, address = browser
    (home / "config.toml").write_text(
        '[capture]\ndays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]\n'
        'from = "00:00"\nto = "24:00"\n',
        encoding="utf-8",
    )
    tab = own.tab(f"{site}/")
    # The interpreter of this run, an address given, and a home of the test's own.
    logger = subprocess.Popen(
        [sys.executable, "-m", "seeress.capture", address, "--home", str(home), "--seconds", "4"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SEERESS_HOME": str(home)},
    )
    try:
        listened(tab)
        tab.click('[data-testid="save"]')
        until(lambda: activity.read_events(home), "the click in the record", 10)
        out, err = logger.communicate(timeout=20)
    finally:
        tab.close()
        if logger.poll() is None:
            logger.kill()
            logger.wait()

    assert logger.returncode == 0, err
    assert out == ""
    assert "1 events written; 0 outside the schedule; 0 over the cap" in err
    assert "It last reported stopped at" in err
    assert capture.status(home).startswith("Capture is not running. It last reported stopped at ")
