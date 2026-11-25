"""The capture without a browser: what it makes of what the browser says, and what the
listener in the page may read.

A unit test cannot open a socket, so the connection is not tried here. The part of the
capture that answers the browser is a plain object, and it is tried on invented messages.
tests/e2e/test_capture.py tries the listener and the connection on a browser that Seeress
starts.
"""

import json
import re
from typing import Any

import pytest

from seeress import activity, capture

TS = "2026-11-20T09:15:05"
WHEN = activity.epoch(TS)
TITLE = "a page whose title must never be written"
QUERY = "a-query-that-must-never-be-written"
# A browser names a tab by thirty-two hexadecimal characters. An event keeps eight.
T1 = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
T2 = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"


# ------------------------------------------------------------------- a payload ---


def payload(**changes: Any) -> str:
    """What the listener reports for a click, changed as asked."""
    said: dict[str, Any] = {
        "type": "click",
        "tag": "button",
        "fingerprint": 'button[data-testid="save"]',
    }
    return json.dumps({**said, **changes})


def typed(**changes: Any) -> str:
    """What the listener reports for an input, changed as asked."""
    said: dict[str, Any] = {
        "type": "input",
        "tag": "input",
        "fingerprint": 'input[name="query"]',
        "field": "text",
    }
    return json.dumps({**said, **changes})


def read(text: str, address: str = "https://example.org/invoices/7") -> activity.Event | None:
    return capture.event_for(text, address, "1a2b3c4d5e6f", WHEN)


def test_a_click_on_a_page_becomes_an_event() -> None:
    event = read(payload())

    assert event == activity.Event(
        1,
        TS,
        "click",
        "example.org",
        "/invoices/7",
        "1a2b3c4d",
        activity.Action("click", "button", 'button[data-testid="save"]', None),
    )


@pytest.mark.parametrize(
    "address, site, path",
    [
        pytest.param(
            "https://example.org/invoices/7?tab=all#top",
            "example.org",
            "/invoices/7",
            id="query and fragment",
        ),
        pytest.param(
            "https://example.org/invoices/7#top", "example.org", "/invoices/7", id="fragment"
        ),
        pytest.param("https://example.org", "example.org", "/", id="no path"),
        pytest.param("https://www.example.org/a?x=1", "example.org", "/a", id="a leading www."),
        pytest.param("http://Intranet.Corp/a/b/", "intranet.corp", "/a/b/", id="upper case"),
        pytest.param("https://example.org:8443/a?x=1", "example.org", "/a", id="a port"),
        pytest.param(
            "https://example.org/a%20b/?q=1",
            "example.org",
            "/a%20b/",
            id="a path as the browser writes it",
        ),
    ],
)
def test_an_address_gives_a_site_and_a_path_and_nothing_else(
    address: str, site: str, path: str
) -> None:
    event = read(payload(), address)

    assert event is not None
    assert (event.site, event.path) == (site, path)
    assert set(event.as_dict()) == {"v", "ts", "kind", "site", "path", "target", "action"}


def test_who_is_named_in_an_address_is_not_kept() -> None:
    """The user information of an address never reaches an event."""
    event = read(payload(), "https://anna:hunter2@example.org/invoices")

    assert event is not None
    assert event.site == "example.org"
    written = json.dumps(event.as_dict())
    assert "anna" not in written
    assert "hunter2" not in written


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("chrome://newtab/", id="a page of the browser's own"),
        pytest.param("chrome://inspect/#remote-debugging", id="the browser's settings"),
        pytest.param("about:blank", id="about:blank"),
        pytest.param("edge://settings", id="a page of another browser's"),
        pytest.param("file:///Users/anna/a-page.html", id="a file on this machine"),
        pytest.param("data:text/html,<b>hi</b>", id="a page of data"),
        pytest.param("https://[::1", id="an address that cannot be split"),
        pytest.param("", id="no address yet"),
    ],
)
def test_an_address_that_is_no_page_on_the_web_makes_no_event(address: str) -> None:
    assert read(payload(), address) is None


def test_a_site_the_schema_refuses_makes_no_event() -> None:
    """A site is a name. A number is not one."""
    assert read(payload(), "http://[::1]:8080/") is None
    assert read(payload(), "http://127.0.0.1:8080/a") is not None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("click", id="a word"),
        pytest.param("", id="empty"),
        pytest.param("{", id="not JSON"),
        pytest.param("[]", id="a list"),
        pytest.param("null", id="null"),
        pytest.param('"click"', id="a string in JSON"),
        pytest.param("{}", id="no member"),
        pytest.param(payload(tag="Save"), id="a tag the schema refuses"),
        pytest.param(payload(fingerprint=""), id="a fingerprint the schema refuses"),
        pytest.param(payload(field="text"), id="a field on a click"),
        pytest.param(payload(unknown="x"), id="a member the schema does not know"),
        pytest.param(
            json.dumps({k: v for k, v in json.loads(typed()).items() if k != "field"}),
            id="an input without a field",
        ),
        pytest.param(typed(field=None), id="an input whose field is null"),
        pytest.param(typed(field="color"), id="a field of a kind the schema does not know"),
    ],
)
def test_a_payload_that_is_not_of_the_shape_makes_no_event(text: str) -> None:
    assert read(text) is None


def test_the_kind_of_a_field_is_carried() -> None:
    event = read(typed(field="password"), "https://mail.example.org/")

    assert event is not None
    assert event.kind == "input"
    assert event.action == activity.Action("input", "input", 'input[name="query"]', "password")


def test_a_tab_is_named_by_eight_characters_and_the_second_is_the_one_given() -> None:
    event = read(payload())

    assert event is not None
    assert event.target == "1a2b3c4d"
    assert event.ts == TS


# ---------------------------------------------------------------- the listener ---


@pytest.mark.parametrize(
    "read_property",
    [
        pytest.param(".value", id="what a field holds"),
        pytest.param(".textContent", id="the text of an element"),
        pytest.param(".innerText", id="the text an element shows"),
        pytest.param(".placeholder", id="the hint of a field"),
        pytest.param(".selectedIndex", id="the choice of a select"),
    ],
)
def test_the_listener_reads_no_property_that_holds_what_a_person_wrote(
    read_property: str,
) -> None:
    assert read_property not in capture.LISTENER


def test_the_listener_reads_no_attribute_but_the_six_it_names() -> None:
    """Four attributes that name an element, and two that say what kind of a field it is."""
    names = set(re.findall(r'getAttribute\("([a-z-]+)"\)', capture.LISTENER))

    assert names == {"data-testid", "id", "name", "aria-label", "type", "autocomplete"}


def test_the_listener_reports_through_the_binding() -> None:
    assert capture.BINDING == "seeressEvent"
    assert f"window.{capture.BINDING}" in capture.LISTENER


def test_the_listener_leaves_a_frame_alone_and_installs_once() -> None:
    assert "window.top !== window" in capture.LISTENER
    assert capture.LISTENER.count("__seeressListening") == 2


# ---------------------------------------------------- what the browser says ---


def info(
    target_id: str = T1,
    *,
    url: str = "https://example.org/a?x=1#top",
    kind: str = "page",
    context: str | None = None,
) -> dict[str, Any]:
    """What the browser says about a target: as the browser says it, with a title."""
    return {
        "targetId": target_id,
        "type": kind,
        "title": TITLE,
        "url": url,
        "attached": False,
        "canAccessOpener": False,
        "browserContextId": context,
    }


def created(about: dict[str, Any]) -> dict[str, Any]:
    return {"method": "Target.targetCreated", "params": {"targetInfo": about}}


def changed(about: dict[str, Any]) -> dict[str, Any]:
    return {"method": "Target.targetInfoChanged", "params": {"targetInfo": about}}


def destroyed(target_id: str) -> dict[str, Any]:
    return {"method": "Target.targetDestroyed", "params": {"targetId": target_id}}


def said(session: str | None, text: str, *, name: str = "seeressEvent") -> dict[str, Any]:
    return {
        "method": "Runtime.bindingCalled",
        "sessionId": session,
        "params": {"executionContextId": 3, "name": name, "payload": text},
    }


def answer(identifier: int, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": identifier, "result": result or {}}


def refused(identifier: int) -> dict[str, Any]:
    return {"id": identifier, "error": {"code": -32602, "message": "No target with given id"}}


class Held:
    """A capture on invented messages: what it sent, and what it would write."""

    def __init__(self) -> None:
        self.events: list[activity.Event] = []
        self.sessions = capture.Sessions(self.events.append)
        self.sent: list[dict[str, Any]] = []

    def hears(self, message: object) -> list[dict[str, Any]]:
        commands = self.sessions.hears(message, WHEN)
        self.sent += commands
        return commands

    def told_of(self, target_id: str = T1, **about: Any) -> dict[str, Any]:
        (command,) = self.hears(created(info(target_id, **about)))
        return command

    def attach(self, target_id: str = T1, session: str = "S1", **about: Any) -> None:
        """A page the browser has told of, attached now as session."""
        command = self.told_of(target_id, **about)
        self.hears(answer(command["id"], {"sessionId": session}))

    def methods(self) -> list[str]:
        return [command["method"] for command in self.sent]


LISTENING = [
    "Runtime.enable",
    "Runtime.addBinding",
    "Page.addScriptToEvaluateOnNewDocument",
]


def test_the_capture_asks_the_browser_to_tell_of_every_target() -> None:
    held = Held()

    assert held.sessions.opens() == [
        {"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}}
    ]


def test_a_page_of_the_default_context_is_attached_and_listened_to() -> None:
    held = Held()

    (attach,) = held.hears(created(info()))

    assert attach["method"] == "Target.attachToTarget"
    assert attach["params"] == {"targetId": T1, "flatten": True}
    commands = held.hears(answer(attach["id"], {"sessionId": "S1"}))

    assert [command["method"] for command in commands] == LISTENING
    assert all(command["sessionId"] == "S1" for command in commands)
    assert commands[1]["params"] == {"name": capture.BINDING, "executionContextName": capture.WORLD}
    assert commands[2]["params"] == {
        "source": capture.LISTENER,
        "worldName": capture.WORLD,
        "runImmediately": True,
    }
    # The commands hold the listener, and nothing the browser said about the page.
    assert TITLE not in json.dumps(commands)
    assert QUERY not in json.dumps(commands)
    assert held.events == []


@pytest.mark.parametrize(
    "about",
    [
        pytest.param(info(context="C1"), id="a page of a second context"),
        pytest.param(info(kind="iframe"), id="a frame that is a target of its own"),
        pytest.param(info(kind="service_worker"), id="a service worker"),
        pytest.param(info(kind="browser"), id="the browser"),
        pytest.param(info(kind="other"), id="anything else"),
    ],
)
def test_anything_but_a_page_of_the_default_context_is_left_alone(about: dict[str, Any]) -> None:
    held = Held()

    assert held.hears(created(about)) == []
    assert held.hears(changed(about)) == []
    assert held.hears(destroyed(about["targetId"])) == []


def test_an_event_is_written_when_the_listener_of_a_tab_reports() -> None:
    held = Held()
    held.attach()

    assert held.hears(said("S1", payload())) == []

    assert held.events == [
        activity.Event(
            1,
            TS,
            "click",
            "example.org",
            "/a",
            T1[:8],
            activity.Action("click", "button", 'button[data-testid="save"]', None),
        )
    ]
    written = json.dumps([event.as_dict() for event in held.events])
    assert TITLE not in written
    assert QUERY not in written


def test_the_site_and_path_are_what_the_browser_last_said_of_the_tab() -> None:
    """A tab that went on to another page is reported on that page."""
    held = Held()
    held.attach()
    held.hears(changed(info(url="https://other.example/b?x=2")))
    held.hears(said("S1", payload()))

    (event,) = held.events
    assert (event.site, event.path) == ("other.example", "/b")


def test_the_address_is_kept_from_before_a_tab_is_attached() -> None:
    held = Held()
    command = held.told_of(url="https://first.example/one")
    held.hears(answer(command["id"], {"sessionId": "S1"}))
    held.hears(said("S1", payload()))

    (event,) = held.events
    assert (event.site, event.path) == ("first.example", "/one")


def test_a_page_whose_address_is_no_page_on_the_web_writes_no_event() -> None:
    held = Held()
    held.attach(url="about:blank")
    held.hears(said("S1", payload()))

    assert held.events == []


def test_a_tab_that_went_away_is_no_longer_listened_to() -> None:
    held = Held()
    held.attach()

    assert held.sessions.listened == frozenset({T1})
    assert held.hears(destroyed(T1)) == []
    assert held.sessions.listened == frozenset()

    held.hears(said("S1", payload()))

    assert held.events == []


def test_a_tab_whose_attach_was_refused_is_dropped() -> None:
    """Nothing is sent on a session that was never given, and the tab may be told of again."""
    held = Held()
    command = held.told_of()

    assert held.hears(refused(command["id"])) == []
    assert held.methods() == ["Target.attachToTarget"]
    assert held.sessions.listened == frozenset()

    (again,) = held.hears(created(info()))

    assert again["method"] == "Target.attachToTarget"


def test_a_late_answer_for_a_tab_that_went_away_attaches_nothing() -> None:
    held = Held()
    command = held.told_of()
    held.hears(destroyed(T1))

    assert held.hears(answer(command["id"], {"sessionId": "S1"})) == []
    assert held.methods() == ["Target.attachToTarget"]
    assert held.sessions.listened == frozenset()


def test_a_page_the_browser_tells_of_again_is_not_attached_again() -> None:
    held = Held()
    held.attach()

    assert held.hears(created(info())) == []
    assert held.methods() == ["Target.attachToTarget", *LISTENING]


def test_two_tabs_attaching_at_once_are_each_answered() -> None:
    """Each command has its own id, and the browser's answer finds it, in whatever order the
    answers come."""
    held = Held()
    first, second = held.told_of(T1), held.told_of(T2, url="https://two.example/b")
    assert len({command["id"] for command in held.sent}) == 2

    late = held.hears(answer(first["id"], {"sessionId": "S1"}))
    early = held.hears(answer(second["id"], {"sessionId": "S2"}))

    assert [command["sessionId"] for command in early] == ["S2"] * 3
    assert [command["sessionId"] for command in late] == ["S1"] * 3
    assert held.sessions.listened == frozenset({T1, T2})
    held.hears(said("S2", payload()))
    held.hears(said("S1", payload()))
    assert [(event.target, event.site) for event in held.events] == [
        (T2[:8], "two.example"),
        (T1[:8], "example.org"),
    ]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(said("S1", payload(), name="anotherBinding"), id="a binding of another name"),
        pytest.param(said("S9", payload()), id="a session nothing is held for"),
        pytest.param(said(None, payload()), id="the browser's own connection"),
        pytest.param(said("S1", "not JSON"), id="a payload that is not JSON"),
    ],
)
def test_what_no_listener_of_the_capture_said_makes_no_event(message: dict[str, Any]) -> None:
    held = Held()
    held.attach()

    held.hears(message)

    assert held.events == []


def test_a_message_that_says_nothing_the_capture_uses_is_ignored() -> None:
    held = Held()
    held.attach()

    messages: tuple[object, ...] = (
        {"params": {}},
        [],
        "a string",
        None,
        {"method": "Runtime.consoleAPICalled", "sessionId": "S1", "params": {}},
        {"method": "Target.targetCreated", "params": "not an object"},
        {"method": "Target.targetCreated"},
        answer(99),
        {"id": "not a number"},
        {"method": "Target.targetDestroyed", "params": {}},
        {"method": "Runtime.bindingCalled", "params": {"name": "seeressEvent"}},
    )

    for message in messages:
        assert held.hears(message) == []
    assert held.events == []
    assert held.sessions.listened == frozenset({T1})


# ---------------------------------------------------------------- the recorder ---


def test_a_recorder_stops_before_it_ever_started() -> None:
    """And may be stopped again, as the cleaning-up of a test does."""
    recorder = capture.Recorder("ws://127.0.0.1:1/devtools/browser/none", lambda event: None)

    recorder.stop()
    recorder.stop()

    assert recorder.running is False
    assert recorder.error is None
