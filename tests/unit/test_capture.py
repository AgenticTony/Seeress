"""The capture without a browser: what it makes of what the browser says, and what the
listener in the page may read.

A unit test cannot open a socket, so the connection is not tried here. The part of the
capture that answers the browser is a plain object, and it is tried on invented messages.
tests/e2e/test_capture.py tries the listener and the connection on a browser that Seeress
starts.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from seeress import activity, capture
from seeress.errors import CaptureError

TS = "2026-11-20T09:15:05"
WHEN = activity.epoch(TS)
TITLE = "a page whose title must never be written"
QUERY = "a-query-that-must-never-be-written"
# A browser names a tab by thirty-two hexadecimal characters in upper case, and a context
# by as many. An event keeps eight of a tab's, as they are written.
T1 = "1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D"
T2 = "9F8E7D6C5B4A39281706F5E4D3C2B1A0"


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
    return capture.event_for(text, address, T1, WHEN)


def test_a_click_on_a_page_becomes_an_event() -> None:
    event = read(payload())

    assert event == activity.Event(
        1,
        TS,
        "click",
        "example.org",
        "/invoices/7",
        T1[:8],
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
        pytest.param("https:///no-host/", id="a web address with no host"),
        pytest.param("http:opaque", id="a web address with no authority"),
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
    assert event.target == T1[:8]
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


def test_the_listener_is_a_script_and_nothing_else() -> None:
    """It was once written with a backslash before its first line, and so was no script at
    all: the browser refused it at its first character, and no test on invented messages
    could see that."""
    assert capture.LISTENER.startswith("(() => {")
    assert capture.LISTENER.rstrip().endswith("})();")
    assert all(line.strip() != "\\" for line in capture.LISTENER.splitlines())


def test_the_listener_leaves_a_frame_alone_and_installs_once() -> None:
    assert "window.top !== window" in capture.LISTENER
    assert capture.LISTENER.count("__seeressListening") == 2


# ---------------------------------------------------- what the browser says ---


# A browser names a tab by thirty-two hexadecimal characters, and a context by as many. An
# event keeps eight of a tab's.
DEFAULT = "AC94CB6B4ADE5E53B64778803D358969"
OTHER = "A1B75576658455A5D62CDAF2618221CA"  # a context made as a private window's is

# What the capture asks to begin with, in order.
OPENING = [
    "Target.setDiscoverTargets",
    "Target.getBrowserContexts",
    "Target.getTargets",
    "Storage.getCookies",
]

# A browser names a frame by thirty-two characters too.
FRAME = "6764B342A115E57CB0DA39C83A95618A"
INSIDE = "9E8D7C6B5A4F3E2D1C0B9A8F7E6D5C4B"  # a frame inside the page
WORLD_ID = 7  # what the browser calls the world the listener runs in


# What is sent on a session once a tab is attached, and then for each new document.
ATTACHING = ["Runtime.enable", "Runtime.addBinding", "Page.getFrameTree"]
LISTENING = ["Page.createIsolatedWorld", "Runtime.evaluate"]


def tree_of(frame: str = FRAME) -> dict[str, Any]:
    """The answer to the third command of attaching: the main frame of the tab."""
    return {"frameTree": {"frame": {"id": frame, "url": "https://example.org/a"}}}


def world(number: int = WORLD_ID) -> dict[str, Any]:
    """The answer to a command that asks for the world of the capture by name."""
    return {"executionContextId": number}


def document_in(frame: str = FRAME, session: str | None = "S1", number: int = 9) -> dict[str, Any]:
    """What the browser says when a new document is in a frame: its world of its own."""
    return {
        "method": "Runtime.executionContextCreated",
        "sessionId": session,
        "params": {
            "context": {
                "id": number,
                "origin": "https://example.org",
                "name": "",
                "auxData": {"isDefault": True, "type": "default", "frameId": frame},
            }
        },
    }


def info(
    target_id: str = T1,
    *,
    url: str = "https://example.org/a?x=1#top",
    kind: str = "page",
    context: str | None = DEFAULT,
) -> dict[str, Any]:
    """What the browser says about a target: as the browser says it, with a title and the
    context it is in."""
    return {
        "targetId": target_id,
        "type": kind,
        "title": TITLE,
        "url": url,
        "attached": False,
        "canAccessOpener": False,
        "browserContextId": context,
    }


def no_context(about: dict[str, Any]) -> dict[str, Any]:
    """The same, as a browser that names no context on a tab of its default one says it."""
    return {name: value for name, value in about.items() if name != "browserContextId"}


def contexts(default: str | None = DEFAULT) -> dict[str, Any]:
    """The answer to the second command: which context is the default one. A browser that
    names none leaves it out."""
    answer: dict[str, Any] = {"browserContextIds": []}
    if default is not None:
        answer["defaultBrowserContextId"] = default
    return answer


def targets(*open: dict[str, Any]) -> dict[str, Any]:
    """The answer to the third command: which targets are open now."""
    return {"targetInfos": list(open)}


def cookies(*names: str) -> dict[str, Any]:
    """The answer to the fourth command: which cookies the browser holds, by name."""
    return {"cookies": [{"name": name, "value": "x", "domain": "d"} for name in names]}


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
    """A capture on invented messages, once the browser has answered its first commands:
    what it sent, and what it would write.

    open are the tabs the browser says are open. default is the context it names as its
    default one, or None for a browser that names none.
    """

    def __init__(self, *open: dict[str, Any], default: str | None = DEFAULT) -> None:
        self.events: list[activity.Event] = []
        self.sessions = capture.Sessions(self.events.append)
        self.sent = self.sessions.opens()
        self.hears(answer(2, contexts(default)))
        self.hears(answer(3, targets(*open)))
        self.hears(answer(4, cookies()))  # and it carries no marker

    def hears(self, message: object) -> list[dict[str, Any]]:
        commands = self.sessions.hears(message, WHEN)
        self.sent += commands
        return commands

    def told_of(self, target_id: str = T1, **about: Any) -> dict[str, Any]:
        (command,) = self.hears(created(info(target_id, **about)))
        return command

    def last(self, method: str) -> dict[str, Any]:
        """The last command sent with this method."""
        return [command for command in self.sent if command["method"] == method][-1]

    def listen(self, session: str = "S1", frame: str = FRAME) -> None:
        """The browser answers the commands that put the listener in the tab: with its main
        frame, and then with the world the listener runs in."""
        (asked,) = self.hears(answer(self.last("Page.getFrameTree")["id"], tree_of(frame)))
        assert asked["method"] == "Page.createIsolatedWorld"
        (put,) = self.hears(answer(asked["id"], world()))
        assert put["method"] == "Runtime.evaluate"

    def attach(self, target_id: str = T1, session: str = "S1", **about: Any) -> None:
        """A page the browser tells of, attached as session, and listened to."""
        command = self.told_of(target_id, **about)
        self.hears(answer(command["id"], {"sessionId": session}))
        self.listen(session)

    def methods(self) -> list[str]:
        return [command["method"] for command in self.sent]


def test_the_capture_asks_the_browser_four_things_to_begin_with() -> None:
    """To tell of every target from now on, which context is its default one, which targets
    are open now, and which cookies it holds. A browser tells of the tabs already open only
    when asked, and a cookie says whether it is Seeress's own."""
    held = Held()

    assert [command["method"] for command in held.sent] == OPENING
    assert held.sent[0]["params"] == {"discover": True}
    assert all(command["params"] == {} for command in held.sent[1:])


def test_the_tabs_already_open_are_attached() -> None:
    held = Held(info(), info(T2, url="https://two.example/b"))

    assert held.methods() == [*OPENING, "Target.attachToTarget", "Target.attachToTarget"]
    assert held.sessions.listened == frozenset()
    (first, second) = [c for c in held.sent if c["method"] == "Target.attachToTarget"]
    held.hears(answer(first["id"], {"sessionId": "S1"}))
    held.hears(answer(second["id"], {"sessionId": "S2"}))
    assert held.sessions.listened == frozenset({T1, T2})


def test_a_tab_is_held_until_the_browser_has_named_its_default_context_and_shown_no_marker() -> (
    None
):
    """A new tab may be told of before the answers come. It is held, and attached once both
    have come, whichever comes second: which context is the browser's own, and that it
    carries no marker."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, _, _, cookies_id = (command["id"] for command in sessions.opens())

    assert sessions.hears(created(info()), WHEN) == []  # nothing is known of either
    assert sessions.hears(answer(cookies_id, cookies()), WHEN) == []  # its context is unknown
    (attach,) = sessions.hears(answer(contexts_id, contexts()), WHEN)  # and now both are

    assert attach["method"] == "Target.attachToTarget"
    assert sessions.listened == frozenset()
    assert sessions.hears(answer(contexts_id, contexts()), WHEN) == []  # each is asked of once
    assert sessions.hears(answer(cookies_id, cookies()), WHEN) == []


def test_a_tab_told_of_after_both_answers_is_attached_at_once() -> None:
    held = Held()

    (attach,) = held.hears(created(info()))

    assert attach["method"] == "Target.attachToTarget"


def test_the_marker_the_capture_looks_for_is_the_one_seeress_sets() -> None:
    from seeress import bodies

    assert capture.MARKER == "seeress_body"
    assert capture.MARKER == bodies.MARKER_KEY


def test_a_browser_that_carries_the_marker_is_attached_to_nothing_and_the_capture_ends() -> None:
    """The plan's test. Seeress's marker is a cookie, and a cookie is the whole browser's:
    nothing in such a browser is a person's work."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, targets_id, _, cookies_id = (command["id"] for command in sessions.opens())
    sent: list[dict[str, Any]] = []
    sent += sessions.hears(answer(contexts_id, contexts()), WHEN)
    sent += sessions.hears(answer(targets_id, targets(info(), info(T2))), WHEN)
    sent += sessions.hears(created(info("ABCDEFABCDEFABCDEFABCDEFABCDEFAB")), WHEN)

    assert sent == []  # three tabs are held, and nothing is attached while the marker is unknown
    with pytest.raises(CaptureError, match="This browser is Seeress's own") as caught:
        sessions.hears(answer(cookies_id, cookies("other", capture.MARKER)), WHEN)

    assert "Nothing in it is captured." in str(caught.value)
    assert sessions.listened == frozenset()
    assert sessions.hears(created(info(T2, url="https://two.example/b")), WHEN) == []
    assert sessions.hears(answer(contexts_id, contexts()), WHEN) == []


def test_a_browser_whose_cookies_cannot_be_read_is_attached_to_nothing() -> None:
    """A check that could not be made is no check. Nothing is captured in a browser that
    could not be asked whether it is Seeress's own."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, _, _, cookies_id = (command["id"] for command in sessions.opens())
    sessions.hears(answer(contexts_id, contexts()), WHEN)
    sessions.hears(created(info()), WHEN)

    with pytest.raises(CaptureError, match="could not be asked"):
        sessions.hears(refused(cookies_id), WHEN)

    assert sessions.hears(created(info(T2)), WHEN) == []


def test_an_answer_about_the_cookies_that_cannot_be_read_is_no_check() -> None:
    sessions = capture.Sessions(lambda event: None)
    _, _, _, cookies_id = (command["id"] for command in sessions.opens())

    with pytest.raises(CaptureError, match="could not be read"):
        sessions.hears(answer(cookies_id, {"cookies": "none"}), WHEN)


def test_a_page_of_the_default_context_is_attached_and_listened_to() -> None:
    held = Held()

    (attach,) = held.hears(created(info()))

    assert attach["method"] == "Target.attachToTarget"
    assert attach["params"] == {"targetId": T1, "flatten": True}
    commands = held.hears(answer(attach["id"], {"sessionId": "S1"}))

    assert [command["method"] for command in commands] == ATTACHING
    assert all(command["sessionId"] == "S1" for command in commands)
    assert commands[1]["params"] == {
        "name": capture.BINDING,
        "executionContextName": capture.WORLD,
    }
    held.hears(answer(commands[2]["id"], tree_of()))
    held.hears(answer(held.last("Page.createIsolatedWorld")["id"], world()))
    (asked, put) = held.sent[-2:]
    assert asked["params"] == {"frameId": FRAME, "worldName": capture.WORLD}
    assert put["params"] == {"expression": capture.LISTENER, "contextId": WORLD_ID}
    # The commands hold the listener, and nothing the browser said about the page.
    assert TITLE not in json.dumps(held.sent)
    assert QUERY not in json.dumps(held.sent)
    assert held.events == []


def test_each_new_document_in_the_tab_is_listened_to_again() -> None:
    """A browser does not run a script registered for new documents in a world of its own.
    So when it says a new document is in the main frame, the capture asks for its world
    there and puts the listener in it."""
    held = Held()
    held.attach()
    before = held.methods()

    (asked,) = held.hears(document_in())

    assert asked["params"] == {"frameId": FRAME, "worldName": capture.WORLD}
    (put,) = held.hears(answer(asked["id"], world(11)))

    assert put["params"] == {"expression": capture.LISTENER, "contextId": 11}
    assert held.methods() == [*before, *LISTENING]
    assert held.hears(answer(put["id"], {"result": {}})) == []


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(document_in(frame=INSIDE), id="a document in a frame inside the page"),
        pytest.param(document_in(session="S9"), id="a session nothing is held for"),
        pytest.param(document_in(session=None), id="the browser's own connection"),
        pytest.param(
            {
                "method": "Runtime.executionContextCreated",
                "sessionId": "S1",
                "params": {
                    "context": {
                        "id": 9,
                        "name": "seeress",
                        "auxData": {"isDefault": False, "type": "isolated", "frameId": FRAME},
                    }
                },
            },
            id="a world of the page's own in the main frame",
        ),
    ],
)
def test_no_other_context_of_a_tab_starts_a_listener(message: dict[str, Any]) -> None:
    held = Held()
    held.attach()
    before = held.methods()

    assert held.hears(message) == []

    assert held.methods() == before


@pytest.mark.parametrize(
    "about",
    [
        pytest.param(
            info(context=OTHER), id="a page of a second context, as a private window is in"
        ),
        pytest.param(info(kind="iframe"), id="a frame that is a target of its own"),
        pytest.param(info(kind="service_worker"), id="a service worker"),
        pytest.param(info(kind="browser"), id="the browser"),
        pytest.param(info(kind="browser_ui"), id="the browser's own interface"),
        pytest.param(info(kind="other"), id="anything else"),
    ],
)
def test_anything_but_a_page_of_the_default_context_is_left_alone(about: dict[str, Any]) -> None:
    held = Held()

    assert held.hears(created(about)) == []
    assert held.hears(changed(about)) == []
    assert held.hears(destroyed(about["targetId"])) == []
    assert held.methods() == OPENING


def test_a_page_of_a_second_context_that_is_already_open_is_left_alone() -> None:
    held = Held(info(context=OTHER), info(T2, context=OTHER, kind="iframe"))

    assert held.methods() == OPENING
    assert held.sessions.listened == frozenset()


def test_a_browser_that_names_no_default_context_has_for_its_own_a_tab_that_names_none() -> None:
    """An older browser says neither which context is its default one nor which a tab is
    in, when it is in that one."""
    held = Held(no_context(info()), default=None)

    assert held.methods() == [*OPENING, "Target.attachToTarget"]
    assert held.hears(created(info(context=OTHER))) == []
    assert held.hears(answer(3, targets(no_context(info())))) == []  # told of again
    (another,) = held.hears(answer(3, targets(no_context(info(T2)))))
    assert another["method"] == "Target.attachToTarget"  # another that names no context
    named = Held(default=None)
    assert named.hears(created(info())) == []  # a tab that names a context is not its own


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
    assert held.methods() == [*OPENING, "Target.attachToTarget"]
    assert held.sessions.listened == frozenset()

    (again,) = held.hears(created(info()))

    assert again["method"] == "Target.attachToTarget"


def test_a_late_answer_for_a_tab_that_went_away_attaches_nothing() -> None:
    held = Held()
    command = held.told_of()
    held.hears(destroyed(T1))

    assert held.hears(answer(command["id"], {"sessionId": "S1"})) == []
    assert held.methods() == [*OPENING, "Target.attachToTarget"]
    assert held.sessions.listened == frozenset()


def test_a_page_the_browser_tells_of_again_is_not_attached_again() -> None:
    held = Held()
    held.attach()

    assert held.hears(created(info())) == []
    assert held.hears(answer(3, targets(info()))) == []
    assert held.methods() == [*OPENING, "Target.attachToTarget", *ATTACHING, *LISTENING]


def test_two_tabs_attaching_at_once_are_each_answered() -> None:
    """Each command has its own id, and the browser's answer finds it, in whatever order the
    answers come."""
    held = Held()
    first, second = held.told_of(T1), held.told_of(T2, url="https://two.example/b")
    assert len({command["id"] for command in held.sent}) == len(OPENING) + 2

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
    before = held.methods()

    messages: tuple[object, ...] = (
        {"params": {}},
        [],
        "a string",
        None,
        {"method": "Runtime.consoleAPICalled", "sessionId": "S1", "params": {}},
        {"method": "Target.targetCreated", "params": "not an object"},
        {"method": "Target.targetCreated"},
        {"method": "Target.targetCreated", "params": {"targetInfo": None}},
        {"method": "Target.targetInfoChanged", "params": {"targetInfo": "not a target"}},
        {"method": "Target.targetDestroyed", "params": {}},
        {"method": "Target.browserContextCreated", "params": {"browserContextId": OTHER}},
        {"method": "Runtime.bindingCalled", "params": {"name": "seeressEvent"}},
        answer(99),
        {"id": "not a number"},
        answer(2, contexts(OTHER)),  # the default context is asked of once, at the start
        answer(4, cookies(capture.MARKER)),  # and so is the marker
    )

    for message in messages:
        assert held.hears(message) == []
    assert held.events == []
    assert held.sessions.listened == frozenset({T1})
    assert held.methods() == before


# ---------------------------------------------------------------- the recorder ---


def test_a_recorder_stops_before_it_ever_started() -> None:
    """And may be stopped again, as the cleaning-up of a test does."""
    recorder = capture.Recorder("ws://127.0.0.1:1/devtools/browser/none", lambda event: None)

    recorder.stop()
    recorder.stop()

    assert recorder.running is False
    assert recorder.error is None


# --------------------------------------------------------------------- the gate ---


def clicks(
    count: int, *, tab: str = T1[:8], first: str = "2026-11-20T09:15:00"
) -> list[activity.Event]:
    """count clicks in one tab, fifty a second from first: a thousand in twenty seconds.
    Friday 20 November 2026, a working day, inside the default schedule."""
    start = activity.epoch(first)
    return [
        activity.Event(
            1,
            activity.clock(start + number // 50),
            "click",
            "example.org",
            "/",
            tab,
            activity.Action("click", "button", "b", None),
        )
        for number in range(count)
    ]


def passed(gate: capture.Gate, events: list[activity.Event]) -> None:
    for event in events:
        gate(event)


def test_a_minute_of_a_thousand_events_in_one_tab_writes_two_hundred(tmp_path: Path) -> None:
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(1000))

    assert len(activity.read_events(tmp_path)) == 200
    assert gate.written == 200
    assert gate.dropped == {"schedule": 0, "cap": 800}


def test_a_thousand_events_in_each_of_two_tabs_write_two_hundred_each(tmp_path: Path) -> None:
    """The cap is for one tab. What floods one tab takes nothing from another."""
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(1000))
    passed(gate, clicks(1000, tab=T2[:8]))

    assert len(activity.read_events(tmp_path)) == 400
    assert gate.dropped == {"schedule": 0, "cap": 1600}


def test_an_event_after_a_quiet_minute_opens_a_new_window(tmp_path: Path) -> None:
    """The first event of a tab after a quiet minute opens the window again."""
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(200))  # 09:15:00 to 09:15:03: the two hundred of a window
    passed(gate, clicks(1, first="2026-11-20T09:15:30"))  # the 201st of that window
    passed(gate, clicks(1, first="2026-11-20T09:16:00"))  # sixty seconds after it opened
    passed(gate, clicks(1, first="2026-11-20T09:16:59"))  # in the second window
    passed(gate, clicks(1, first="2026-11-20T09:17:00"))  # sixty seconds on: a third

    assert gate.written == 203
    assert gate.dropped == {"schedule": 0, "cap": 1}
    assert [event.ts for event in activity.read_events(tmp_path)][-3:] == [
        "2026-11-20T09:16:00",
        "2026-11-20T09:16:59",
        "2026-11-20T09:17:00",
    ]


def test_the_cap_is_two_hundred_events_in_sixty_seconds() -> None:
    assert capture.CAP == 200
    assert capture.WINDOW == 60


def test_an_event_outside_the_schedule_is_dropped_and_one_inside_is_written(
    tmp_path: Path,
) -> None:
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(1, first="2026-11-20T07:59:59"))  # Friday, before eight
    passed(gate, clicks(1, first="2026-11-20T08:00:00"))
    passed(gate, clicks(1, first="2026-11-20T17:59:59"))
    passed(gate, clicks(1, first="2026-11-20T18:00:00"))  # after six

    assert [event.ts for event in activity.read_events(tmp_path)] == [
        "2026-11-20T08:00:00",
        "2026-11-20T17:59:59",
    ]
    assert gate.dropped == {"schedule": 2, "cap": 0}


def test_a_day_that_is_not_in_the_schedule_drops_every_event_of_it(tmp_path: Path) -> None:
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(3, first="2026-11-21T10:00:00"))  # Saturday
    passed(gate, clicks(3, first="2026-11-22T10:00:00"))  # Sunday
    passed(gate, clicks(3, first="2026-11-23T10:00:00"))  # Monday

    assert len(activity.read_events(tmp_path)) == 3
    assert gate.dropped == {"schedule": 6, "cap": 0}


def test_the_schedule_of_the_gate_is_the_one_it_is_given(tmp_path: Path) -> None:
    gate = capture.Gate(tmp_path, schedule=activity.Schedule(frozenset({5}), 10, 11))

    passed(gate, clicks(1, first="2026-11-20T10:30:00"))  # Friday: a working day by default
    passed(gate, clicks(1, first="2026-11-21T10:30:00"))  # Saturday, ten to twelve

    assert [event.ts for event in activity.read_events(tmp_path)] == ["2026-11-21T10:30:00"]


def test_what_passes_the_gate_is_written_as_it_came_and_for_the_day_of_its_own_second(
    tmp_path: Path,
) -> None:
    gate = capture.Gate(tmp_path)
    (event,) = clicks(1, first="2026-11-20T09:15:07")

    gate(event)

    (written,) = activity.read_events(tmp_path)
    assert written == event
    assert [path.name for path in (tmp_path / "watch").iterdir()] == ["activity-20261120.jsonl"]


def test_an_event_that_cannot_be_written_ends_the_gate_with_the_error(tmp_path: Path) -> None:
    """The gate writes through write_event, which raises. Nothing is swallowed."""
    (tmp_path / "watch").write_text("a file where the folder should be", encoding="utf-8")
    gate = capture.Gate(tmp_path)

    with pytest.raises(OSError):
        gate(clicks(1)[0])

    assert gate.written == 0
