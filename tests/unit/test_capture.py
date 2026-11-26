"""The capture without a browser: what it makes of what the browser says, and what the
listener in the page may read.

A unit test cannot open a socket, so the connection is not tried here. The part of the
capture that answers the browser is a plain object, and it is tried on invented messages.
Where what is asked of the connection is the point, a stand-in takes the connection's
place, and no socket is opened.
tests/e2e/test_capture.py tries the listener and the connection on a browser that Seeress
starts.
"""

import asyncio
import contextlib
import gc
import json
import logging
import os
import re
import signal
import stat
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from websockets.exceptions import WebSocketException

from seeress import activity, capture, config
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


def test_a_path_over_the_bound_is_cut_to_it_and_the_click_is_kept() -> None:
    """A page chooses the address of its own tab, and so chose the size of each event: on a
    real browser one click wrote a line of 2 MB. The path is cut, whole, and the event kept."""
    made = read(payload(), "https://example.org/" + "a" * 3000)

    assert made is not None
    assert made.path == "/" + "a" * 2047


def test_a_site_over_the_bound_makes_no_event() -> None:
    """No site has a name that long, and a cut site would be another site."""
    assert read(payload(), "https://" + "a" * 254 + "/") is None


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
        pytest.param(".title", id="the title of an element, which is prose"),
    ],
)
def test_the_listener_reads_no_property_that_holds_what_a_person_wrote(
    read_property: str,
) -> None:
    assert read_property not in capture.LISTENER


def test_the_listener_reads_no_attribute_but_the_five_it_names() -> None:
    """Three attributes that name an element, and two that say what kind of a field it is.
    Prose is no name: an aria-label says something, often about a person, and a page
    writes what it likes in one."""
    names = set(re.findall(r'getAttribute\("([a-z-]+)"\)', capture.LISTENER))

    assert names == {"data-testid", "id", "name", "type", "autocomplete"}


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


def contexts(default: str = DEFAULT) -> dict[str, Any]:
    """The answer to the second command: which context is the default one."""
    return {"browserContextIds": [], "defaultBrowserContextId": default}


def targets(*open: dict[str, Any]) -> dict[str, Any]:
    """The answer to the third command: which targets are open now."""
    return {"targetInfos": list(open)}


def cookies(*names: str) -> dict[str, Any]:
    """The answer to the fourth command: which cookies the browser holds, by name. Each is
    held for the marker's own site, as the marker itself is."""
    return {
        "cookies": [{"name": name, "value": "x", "domain": "seeress.invalid"} for name in names]
    }


def a_cookie_of(site: str, name: str) -> dict[str, Any]:
    """A cookie a page of that site set, as the browser tells it."""
    return {"name": name, "value": "1", "domain": site, "path": "/"}


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
    default one.
    """

    def __init__(self, *open: dict[str, Any], default: str = DEFAULT) -> None:
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


def test_a_cookie_named_as_the_marker_of_another_site_is_no_marker() -> None:
    """A page may name a cookie of its own site as the marker is named. The marker is set at
    seeress.invalid and nowhere else. Before, any cookie so named ended the capture, and a
    logger on a person's own Chrome tried again every five seconds, and ended again."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, targets_id, _, cookies_id = (command["id"] for command in sessions.opens())
    sent: list[dict[str, Any]] = []
    sent += sessions.hears(answer(contexts_id, contexts()), WHEN)
    sent += sessions.hears(answer(targets_id, targets(info(), info(T2))), WHEN)
    third = created(info("ABCDEFABCDEFABCDEFABCDEFABCDEFAB"))
    assert sent + sessions.hears(third, WHEN) == []  # three tabs are held

    sent = sessions.hears(
        answer(
            cookies_id,
            {
                "cookies": [
                    a_cookie_of("example.com", "seeress_body"),
                    a_cookie_of("seeress.invalid", "other"),
                ]
            },
        ),
        WHEN,
    )

    assert [command["method"] for command in sent] == ["Target.attachToTarget"] * 3


def test_a_site_that_ends_with_the_marker_s_is_not_the_marker_s() -> None:
    """The marker's site is the whole of it, not its tail."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, targets_id, _, cookies_id = (command["id"] for command in sessions.opens())
    sent: list[dict[str, Any]] = []
    sent += sessions.hears(answer(contexts_id, contexts()), WHEN)
    sent += sessions.hears(answer(targets_id, targets(info(), info(T2))), WHEN)
    sent += sessions.hears(created(info("ABCDEFABCDEFABCDEFABCDEFABCDEFAB")), WHEN)
    assert sent == []  # three tabs are held

    sent = sessions.hears(
        answer(cookies_id, {"cookies": [a_cookie_of("notseeress.invalid", "seeress_body")]}),
        WHEN,
    )

    assert [command["method"] for command in sent] == ["Target.attachToTarget"] * 3


def test_the_marker_held_as_a_domain_cookie_is_the_marker_all_the_same() -> None:
    """Chrome keeps a cookie two ways: for its host alone, with the site as it is, and for
    a domain, with a dot before the site. Seeress sets its marker the first way. Held the
    other way, it is the marker all the same."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, targets_id, _, cookies_id = (command["id"] for command in sessions.opens())
    sent: list[dict[str, Any]] = []
    sent += sessions.hears(answer(contexts_id, contexts()), WHEN)
    sent += sessions.hears(answer(targets_id, targets(info(), info(T2))), WHEN)
    sent += sessions.hears(created(info("ABCDEFABCDEFABCDEFABCDEFABCDEFAB")), WHEN)
    assert sent == []  # three tabs are held

    with pytest.raises(CaptureError, match="This browser is Seeress's own"):
        sessions.hears(
            answer(cookies_id, {"cookies": [a_cookie_of(".seeress.invalid", capture.MARKER)]}),
            WHEN,
        )

    assert sessions.listened == frozenset()
    assert sessions.hears(created(info(T2, url="https://two.example/b")), WHEN) == []


@pytest.mark.parametrize(
    "odd",
    [
        {"value": "x", "domain": "seeress.invalid"},  # no name
        {"name": "seeress_body"},  # no site
        {"name": "seeress_body", "domain": 1},  # a site that is not text
        "seeress_body",  # not a cookie
    ],
)
def test_a_cookie_that_cannot_be_read_makes_the_answer_unreadable(odd: object) -> None:
    """A cookie with no name or no site, as text, cannot be told from the marker. A check
    that could not be made is no check: nothing in that browser is captured."""
    sessions = capture.Sessions(lambda event: None)
    contexts_id, _, _, cookies_id = (command["id"] for command in sessions.opens())
    sessions.hears(answer(contexts_id, contexts()), WHEN)
    sessions.hears(created(info()), WHEN)

    with pytest.raises(CaptureError, match="could not be read"):
        sessions.hears(
            answer(cookies_id, {"cookies": [a_cookie_of("seeress.invalid", "other"), odd]}), WHEN
        )

    assert sessions.hears(created(info(T2)), WHEN) == []


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


@pytest.mark.parametrize(
    ("at", "what"),
    [
        pytest.param(0, "to tell of its targets", id="what it will be told of"),
        pytest.param(1, "which context is its default one", id="the default context"),
        pytest.param(2, "which targets are open", id="the targets that are open"),
    ],
)
def test_a_browser_that_refuses_what_the_capture_begins_by_asking_is_captured_from_no_more(
    at: int, what: str
) -> None:
    """Without one of these the capture would be deaf, to the tabs that come, to what is the
    browser's own, or to the tabs already open. It ends with an error that says so, instead
    of going on connected and recording nothing."""
    sessions = capture.Sessions(lambda event: None)
    commands = sessions.opens()

    with pytest.raises(CaptureError, match=what):
        sessions.hears(refused(commands[at]["id"]), WHEN)


def test_a_browser_that_names_no_default_context_is_captured_from_no_more() -> None:
    """The capture listens to a tab only when the browser puts it in the context it names as
    its default one. A browser that names none cannot be asked which tab is a person's and
    which a private window, and nothing in it is captured."""
    sessions = capture.Sessions(lambda event: None)
    (_, contexts_id, _, _) = (command["id"] for command in sessions.opens())

    with pytest.raises(CaptureError, match="default"):
        sessions.hears(answer(contexts_id, {"browserContextIds": []}), WHEN)


def test_a_tab_that_names_no_context_is_not_the_browser_s_own_once_it_names_one() -> None:
    """Every tab that is not in a private window names the default context. One that names
    none cannot be said to be in it, and is not attached."""
    held = Held()
    about = info()
    del about["browserContextId"]

    assert held.hears(created(about)) == []
    assert held.methods() == OPENING


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


def test_a_tab_whose_frame_tree_was_refused_is_dropped_and_not_counted_as_listened() -> None:
    """The listener needs the frame of its tab, and the capture never asked for the frame
    again. Before, the tab stayed attached and deaf, and was counted as listening: its next
    document was not listened to, and nothing said so."""
    held = Held()
    (attach,) = held.hears(created(info()))
    held.hears(answer(attach["id"], {"sessionId": "S1"}))
    assert held.sessions.listened == frozenset({T1})  # attached, while its frame is asked for

    assert held.hears(refused(held.last("Page.getFrameTree")["id"])) == []

    assert held.sessions.listened == frozenset()


def test_a_tab_whose_world_was_refused_is_dropped_and_not_counted_as_listened() -> None:
    held = Held()
    (attach,) = held.hears(created(info()))
    held.hears(answer(attach["id"], {"sessionId": "S1"}))
    (asked,) = held.hears(answer(held.last("Page.getFrameTree")["id"], tree_of(FRAME)))
    assert asked["method"] == "Page.createIsolatedWorld"

    assert held.hears(refused(asked["id"])) == []

    assert held.sessions.listened == frozenset()


@pytest.mark.parametrize("refused_what", ["attach", "frame", "world"])
def test_the_next_document_of_a_dropped_tab_asks_for_it_again(refused_what: str) -> None:
    """A tab is dropped when its attach, its frame tree or its world was refused. The browser
    tells of a tab again when its address changes, and then the capture attaches it again.
    Before, a dropped tab was deaf for good, whatever the browser told of it after."""
    held = Held()
    (attach,) = held.hears(created(info()))
    if refused_what == "attach":
        refused_id = attach["id"]
    else:
        held.hears(answer(attach["id"], {"sessionId": "S1"}))
        if refused_what == "frame":
            refused_id = held.last("Page.getFrameTree")["id"]
        else:
            (asked,) = held.hears(answer(held.last("Page.getFrameTree")["id"], tree_of(FRAME)))
            refused_id = asked["id"]
    assert held.hears(refused(refused_id)) == []
    assert held.sessions.listened == frozenset()

    (again,) = held.hears(changed(info(url="https://one.example/next")))

    assert again["method"] == "Target.attachToTarget"
    assert [
        command["method"] for command in held.hears(answer(again["id"], {"sessionId": "S2"}))
    ] == [
        "Runtime.enable",
        "Runtime.addBinding",
        "Page.getFrameTree",
    ]
    held.listen(session="S2")
    assert held.sessions.listened == frozenset({T1})


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


def test_an_event_outside_the_schedule_counts_as_that_and_not_as_over_the_cap(
    tmp_path: Path,
) -> None:
    """The schedule is judged first. A burst outside the hours a person chose is dropped
    for that reason alone, and the count says so: it does not read as a tab that was busy
    in the hours kept."""
    gate = capture.Gate(tmp_path)

    passed(gate, clicks(250, first="2026-11-20T07:00:00"))  # Friday, before eight

    assert activity.read_events(tmp_path) == []
    assert gate.dropped == {"schedule": 250, "cap": 0}


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


def test_a_lone_surrogate_in_an_action_s_name_makes_no_event() -> None:
    """A lone surrogate can be written in an attribute, and no file can hold it. The event
    is refused where it is made, by the rule that reads one back, and the capture goes on:
    before, the write raised UnicodeEncodeError, and that ended the capture."""
    alone = json.loads('"\\ud800"')  # what json.loads makes of what the page sent
    payload = {"type": "click", "tag": "button", "fingerprint": 'button[name="a' + alone + 'b"]'}

    assert capture.event_for(json.dumps(payload), "https://example.org/", T1, WHEN) is None
    with pytest.raises(ValueError):
        activity.Event.parse(
            {
                "v": 1,
                "ts": "2026-11-20T09:15:00",
                "kind": "click",
                "site": "example.org",
                "path": "/",
                "target": T1[:8],
                "action": payload,
            }
        )

    whole = json.loads('"\\ufffd"')  # the replacement character is whole, and is kept
    kept = {"type": "click", "tag": "button", "fingerprint": 'button[name="a' + whole + 'b"]'}

    assert capture.event_for(json.dumps(kept), "https://example.org/", T1, WHEN) is not None

    late = {
        "type": "click",
        "tag": "button",
        "fingerprint": 'button[name="' + "a" * 180 + alone + '"]',
    }
    assert len(late["fingerprint"]) <= 200  # and it is late in a long name

    assert capture.event_for(json.dumps(late), "https://example.org/", T1, WHEN) is None


def test_an_event_that_cannot_be_written_ends_the_gate_with_the_error(tmp_path: Path) -> None:
    """The gate writes through write_event, which raises. Nothing is swallowed."""
    (tmp_path / "watch").write_text("a file where the folder should be", encoding="utf-8")
    gate = capture.Gate(tmp_path)

    with pytest.raises(OSError):
        gate(clicks(1)[0])

    assert gate.written == 0


# ------------------------------------------------------ what is kept, and the status ---


# A day that is not today's, whenever the test runs.
TODAY = date(2027, 3, 31)


def held(home: Path, day: str) -> Path:
    """A file of the capture that holds one event written on that day, as 20261025."""
    ts = f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00"
    return activity.write_event(home, clicks(1, first=ts)[0], now=activity.epoch(ts))


def others(watch: Path) -> list[Path]:
    """Files of the record that the capture did not write, one of each kind there is."""
    made = []
    for name in (
        "spike-20261001.jsonl",
        "spike-20261020.jsonl",
        "agent-runs.jsonl",
        "flow-runs.jsonl",
        "activity-notes.jsonl",
        "activity-2026103.jsonl",
        "capture-status.json",
    ):
        path = watch / name
        path.write_text("a line that is nobody's to delete here\n", encoding="utf-8")
        made.append(path)
    return made


def test_retention_deletes_what_is_older_than_the_days_kept_in_day_order(tmp_path: Path) -> None:
    """Thirty days are kept. Today is 31 March 2027: a file of 1 March is thirty days old
    and is kept; four before it are older and go, their oldest day first."""
    gone = [held(tmp_path, day) for day in ("20270101", "20270115", "20270202", "20270228")]
    kept = held(tmp_path, "20270301")
    new = held(tmp_path, "20270331")

    deleted = capture.retention(tmp_path, 30, today=TODAY)

    assert deleted == gone
    assert not any(path.exists() for path in gone)
    assert kept.exists() and new.exists()


def test_retention_touches_no_file_but_the_capture_s(tmp_path: Path) -> None:
    """The spike logger's files least of all."""
    held(tmp_path, "20261001")
    untouched = others(tmp_path / "watch")

    assert len(capture.retention(tmp_path, 30, today=date(2026, 11, 25))) == 1

    assert all(path.exists() for path in untouched)
    assert sorted(path.name for path in (tmp_path / "watch").iterdir()) == sorted(
        path.name for path in untouched
    )


def test_retention_with_no_day_given_uses_the_machine_s(tmp_path: Path) -> None:
    forty_days_ago = (date.today() - timedelta(days=40)).strftime("%Y%m%d")
    old = held(tmp_path, forty_days_ago)

    deleted = capture.retention(tmp_path, 30)

    assert deleted == [old]
    assert not old.exists()


def test_retention_of_a_record_that_is_not_there_is_nothing(tmp_path: Path) -> None:
    assert capture.retention(tmp_path, 30, today=date(2026, 11, 25)) == []
    assert list(tmp_path.iterdir()) == []


def test_what_is_kept_is_done_now_and_again_every_so_often_until_stopped() -> None:
    """A clock the test can move: every is a hundredth of a second here."""
    said: list[int] = []
    stop = asyncio.Event()

    def keep() -> None:
        said.append(len(said))
        if len(said) == 3:
            stop.set()
        if len(said) > 3:  # asked of after the stop: that is a capture that never rests
            raise AssertionError("kept after the stop")

    asyncio.run(capture.hourly(keep, 0.01, stop))

    assert said == [0, 1, 2]


def test_what_is_kept_ends_with_its_error() -> None:
    """A failure to delete is not hidden: the capture ends with it."""

    def keep() -> None:
        raise OSError("the record could not be deleted")

    with pytest.raises(OSError, match="could not be deleted"):
        asyncio.run(capture.hourly(keep, 0.01, asyncio.Event()))


# ------------------------------------------------------------------- the status ---


def said_to_be(
    home: Path, state: str, reason: str | None = None, **counts: Any
) -> dict[str, object]:
    """Write the status once, and return what the file holds."""
    capture.write_status(
        home,
        state,
        reason,
        events=counts.get("events", 0),
        last_event=counts.get("last_event"),
        now=counts.get("now", WHEN),
    )
    path = home / "watch" / "capture-status.json"
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_the_status_file_holds_what_the_capture_says_of_itself(tmp_path: Path) -> None:
    data = said_to_be(tmp_path, "connected", None, events=7, last_event=TS)

    assert data == {
        "pid": os.getpid(),
        "state": "connected",
        "reason": None,
        "since": TS,
        "events": 7,
        "last_event": TS,
    }
    assert stat.S_IMODE((tmp_path / "watch" / "capture-status.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "watch").stat().st_mode) == 0o700
    # The report replaces what was there and leaves nothing beside it.
    assert [path.name for path in (tmp_path / "watch").iterdir()] == ["capture-status.json"]


def test_the_status_file_is_replaced_whole_and_never_rewritten_in_place(tmp_path: Path) -> None:
    """A reader finds one report or the other, never none and never half of one. Shown by
    a reader that opened the file before a report was written: after it, the reader still
    holds the report it opened. A file rewritten in place would put the new one under its
    eyes."""
    capture.write_status(tmp_path, "connected", None, events=0, last_event=None, now=WHEN)
    path = tmp_path / "watch" / "capture-status.json"

    with path.open(encoding="utf-8") as reader:
        capture.write_status(tmp_path, "stopped", None, events=3, last_event=TS, now=WHEN + 1)
        held = reader.read()

    assert json.loads(held)["state"] == "connected"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "stopped"
    assert [each.name for each in path.parent.iterdir()] == ["capture-status.json"]


def test_the_status_is_written_into_a_folder_made_private_when_it_is_already_there_and_open(
    tmp_path: Path,
) -> None:
    (tmp_path / "watch").mkdir()
    (tmp_path / "watch").chmod(0o755)  # as the older logger made it

    capture.write_status(tmp_path, "connected", None, events=0, last_event=None, now=WHEN)

    assert stat.S_IMODE((tmp_path / "watch").stat().st_mode) == 0o700


def test_one_event_is_one_event(tmp_path: Path) -> None:
    said_to_be(tmp_path, "connected", None, events=1, last_event=TS)

    assert report(tmp_path).endswith(f"(pid {os.getpid()}): 1 event, the last at {TS}.")


def test_the_since_of_a_state_is_kept_while_the_state_stays_the_same(tmp_path: Path) -> None:
    said_to_be(tmp_path, "connected", None, now=WHEN)
    later = WHEN + 3600

    data = said_to_be(tmp_path, "connected", None, events=3, last_event=TS, now=later)

    assert data["since"] == TS  # of the first report
    assert data["events"] == 3

    down = said_to_be(tmp_path, "down", "ConnectionRefusedError", now=later + 60)

    assert down["since"] == activity.clock(later + 60)  # the state changed

    again = said_to_be(tmp_path, "down", "ConnectionRefusedError", now=later + 120)

    assert again["since"] == down["since"]  # and then it did not


def alive_only_this_process(pid: int) -> bool:
    return pid == os.getpid()


def report(home: Path) -> str:
    return capture.status(home, alive=alive_only_this_process)


def test_with_no_status_file_the_capture_has_never_run(tmp_path: Path) -> None:
    assert report(tmp_path) == "Capture has never run here."


def test_with_a_status_file_that_cannot_be_read_the_capture_has_never_run(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "capture-status.json").write_text("{not json", encoding="utf-8")
    assert report(tmp_path) == "Capture has never run here."

    (watch / "capture-status.json").write_text("[1, 2]", encoding="utf-8")  # no object
    assert report(tmp_path) == "Capture has never run here."

    (watch / "capture-status.json").write_text('{"state": "somewhere"}', encoding="utf-8")
    assert report(tmp_path) == "Capture has never run here."


def test_a_capture_whose_process_is_gone_is_not_running(tmp_path: Path) -> None:
    said_to_be(tmp_path, "connected", None, events=2, last_event=TS)

    gone = capture.status(tmp_path, alive=lambda pid: False)

    assert gone == f"Capture is not running. It last reported connected at {TS}."


def test_a_capture_that_is_recording_says_since_when_and_how_much(tmp_path: Path) -> None:
    said_to_be(tmp_path, "connected", None, events=2, last_event=TS)

    assert report(tmp_path) == (
        f"Capture is recording since {TS} (pid {os.getpid()}): 2 events, the last at {TS}."
    )

    said_to_be(tmp_path, "connected", None, events=0, last_event=None)

    assert report(tmp_path).endswith(f"(pid {os.getpid()}): 0 events.")


def test_a_capture_that_is_down_says_since_when_and_why(tmp_path: Path) -> None:
    said_to_be(tmp_path, "down", "ConnectionRefusedError: nothing listens on 9")

    assert report(tmp_path) == (
        f"Capture is down since {TS}: ConnectionRefusedError: nothing listens on 9."
        " Is remote debugging on in Chrome? chrome://inspect/#remote-debugging"
    )


def test_a_status_that_tells_no_time_is_no_report(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "capture-status.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "state": "connected",
                "reason": None,
                "since": "yesterday",
                "events": 1,
                "last_event": None,
            }
        ),
        encoding="utf-8",
    )

    assert report(tmp_path) == "Capture has never run here."


def test_the_status_says_what_the_gate_has_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status is told of the state, and counts what passes the gate. It is written at
    once for a state, and now and then for an event. The clock does not move here: before,
    "0 events" held only because less than a minute had passed."""
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    status = capture.Status(tmp_path)
    gate = capture.Gate(tmp_path, status=status)

    status.state("connected")
    passed(gate, clicks(3, first="2026-11-20T09:15:00"))
    assert report(tmp_path).endswith(" 0 events.")  # an event does not write the file at once

    status.flush()

    assert report(tmp_path).endswith("3 events, the last at 2026-11-20T09:15:00.")


def test_a_status_told_to_write_often_writes_as_the_events_come(tmp_path: Path) -> None:
    """every is how long a count may stay unwritten. Nothing, here: each event is written."""
    status = capture.Status(tmp_path, every=0)
    gate = capture.Gate(tmp_path, status=status)
    status.state("connected")

    passed(gate, clicks(2, first="2026-11-20T09:15:00"))

    assert report(tmp_path).endswith("2 events, the last at 2026-11-20T09:15:00.")


def test_the_since_of_a_new_process_starts_again(tmp_path: Path) -> None:
    """What another process said of itself is not this one's since."""
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "capture-status.json").write_text(
        json.dumps(
            {
                "pid": os.getpid() + 1,
                "state": "connected",
                "reason": None,
                "since": TS,
                "events": 5,
                "last_event": TS,
            }
        ),
        encoding="utf-8",
    )
    later = WHEN + 3600

    data = said_to_be(tmp_path, "connected", None, now=later)

    assert data["since"] == activity.clock(later)


def test_a_capture_that_stopped_is_not_running(tmp_path: Path) -> None:
    said_to_be(tmp_path, "stopped", None)

    assert report(tmp_path) == f"Capture is not running. It last reported stopped at {TS}."


def test_a_capture_that_is_down_whose_process_is_gone_says_why_it_went(tmp_path: Path) -> None:
    said_to_be(tmp_path, "down", "CaptureError: this browser is Seeress's own")

    gone = capture.status(tmp_path, alive=lambda pid: False)

    assert gone == (
        f"Capture is not running. It last reported down at {TS}:"
        " CaptureError: this browser is Seeress's own."
    )


def test_a_capture_that_is_down_with_no_reason_is_no_report(tmp_path: Path) -> None:
    said_to_be(tmp_path, "down", None)

    assert report(tmp_path) == "Capture has never run here."


def test_whether_a_process_is_alive_is_asked_of_the_system() -> None:
    assert capture._alive(os.getpid())
    assert capture._alive(2**31 - 1) is False  # further than any system numbers its processes
    assert capture._alive(1)  # the system's own, which this user may not signal
    assert capture._alive(0) is False  # the group of this process, and no process at all


def test_on_windows_a_process_is_never_asked_by_a_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There a signal of zero is no question: it ends the process, with no word said."""
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(NotImplementedError, match="Windows"):
        capture._alive(os.getpid())


def test_nothing_is_said_before_the_capture_has_a_state(tmp_path: Path) -> None:
    capture.Status(tmp_path).flush()

    assert report(tmp_path) == "Capture has never run here."
    assert list(tmp_path.iterdir()) == []


def test_a_file_of_eight_digits_that_are_no_day_is_left_alone(tmp_path: Path) -> None:
    """Not the capture's to judge, and not a reason to stop keeping the rest."""
    odd = tmp_path / "watch" / "activity-99999999.jsonl"
    odd.parent.mkdir()
    odd.write_text("eight digits that are no day\n", encoding="utf-8")

    assert capture.retention(tmp_path, 1, today=TODAY) == []
    assert odd.exists()


def test_what_is_kept_is_done_before_any_wait() -> None:
    """Were the wait to come first, an hour would pass before anything was kept."""
    stop = asyncio.Event()
    said: list[int] = []

    def keep() -> None:
        said.append(1)
        stop.set()

    asyncio.run(asyncio.wait_for(capture.hourly(keep, 3600, stop), 5))

    assert said == [1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 0),
        ("pid", -5),
        ("pid", True),
        ("events", -1),
        ("events", "many"),
        ("events", True),
        ("last_event", "yesterday"),
        ("state", 5),
        ("reason", 5),
    ],
)
def test_a_report_short_of_its_kind_is_no_report(tmp_path: Path, field: str, value: object) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    whole: dict[str, object] = {
        "pid": os.getpid(),
        "state": "connected",
        "reason": None,
        "since": TS,
        "events": 1,
        "last_event": TS,
    }
    (watch / "capture-status.json").write_text(
        json.dumps({**whole, field: value}), encoding="utf-8"
    )

    assert capture.status(tmp_path) == "Capture has never run here."


def test_the_count_is_written_again_only_when_its_time_has_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clock the test moves. Sixty seconds between one writing of the count and the
    next, counted from the last time it was written."""
    now = [WHEN]
    monkeypatch.setattr(time, "time", lambda: now[0])
    status = capture.Status(tmp_path, every=60)
    gate = capture.Gate(tmp_path, status=status)
    status.state("connected")

    now[0] += 30
    passed(gate, clicks(1))
    assert report(tmp_path).endswith(" 0 events.")  # thirty seconds on: not yet

    now[0] += 31
    passed(gate, clicks(1))
    assert report(tmp_path).endswith(" 2 events, the last at 2026-11-20T09:15:00.")

    now[0] += 39  # a hundred since the start, and thirty-nine since the count was written
    passed(gate, clicks(1))
    assert report(tmp_path).endswith(" 2 events, the last at 2026-11-20T09:15:00.")

    status.flush()
    assert report(tmp_path).endswith(" 3 events, the last at 2026-11-20T09:15:00.")


def test_the_reason_of_a_state_is_written_with_it(tmp_path: Path) -> None:
    capture.Status(tmp_path).state("down", "nothing listens on 9")

    said = report(tmp_path)

    assert said.startswith("Capture is down since ")
    assert said.endswith(
        ": nothing listens on 9."
        " Is remote debugging on in Chrome? chrome://inspect/#remote-debugging"
    )


def test_what_is_not_written_is_not_counted(tmp_path: Path) -> None:
    """An event the schedule refuses, and one that cannot be written, are not events
    written. 21 November 2026 is a Saturday."""
    status = capture.Status(tmp_path)
    gate = capture.Gate(tmp_path, status=status)
    status.state("connected")

    passed(gate, clicks(2, first="2026-11-21T09:15:00"))
    (tmp_path / "watch" / "activity-20261120.jsonl").mkdir()  # a folder where the file is
    with pytest.raises(OSError):
        gate(clicks(1)[0])

    assert status.events == 0


def test_the_listener_cuts_a_fingerprint_whole_and_where_the_record_can_hold_it() -> None:
    """On code points, and at the length the record allows. In the page a string is counted
    in units of sixteen bits, and an emoji is two of them: cut there, a name that ends in
    an emoji ends in half of one, and no file can hold the line."""
    whole = f"""
        fingerprint: Array.from(naming(element) || place(element))
          .slice(0, {activity.FINGERPRINT_AT_MOST})
          .join(""),
"""

    assert whole in capture.LISTENER
    # A page may name a custom element as long as it likes, and the tag is cut in the page
    # too, at the bound the record allows.
    tag = f"""
        tag: Array.from(element.localName).slice(0, {activity.TAG_AT_MOST}).join(""),
"""
    assert tag in capture.LISTENER
    assert (
        capture.LISTENER.count(".slice(") == 2
    )  # the fingerprint and the tag: nothing else is cut


# ------------------------------------------------- what the loop leaves behind it ---


def test_a_task_that_is_still_going_is_cancelled_when_it_is_left() -> None:
    """The answer the capture was waiting for, when it stops waiting: given up, and at
    once."""

    async def scenario() -> bool:
        never = asyncio.Event()
        task = asyncio.ensure_future(never.wait())
        await asyncio.wait_for(capture._left(task), 5)  # for a task never given up
        return task.cancelled()

    assert asyncio.run(scenario()) is True


def test_a_task_that_ended_with_an_error_is_read_when_it_is_left() -> None:
    """Left unread, the loop says so when the task is destroyed: an error nobody saw. So
    that nothing of the capture is left behind it unread, the end of a task is read."""
    said: list[object] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: said.append(context.get("message")))

        async def failing() -> None:
            raise RuntimeError("an error nobody awaits")

        task = asyncio.ensure_future(failing())
        await asyncio.sleep(0)  # it has ended, with its error
        await capture._left(task)
        del task
        gc.collect()  # destroyed here, unread or not

    asyncio.run(scenario())

    assert said == []


# ------------------------------------------------------- the connection, in place ---


def test_the_capture_s_connection_has_no_proxy_and_no_bound_on_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy must never see what is said on this machine, and a bound on one message let
    any page end the capture. A stand-in is asked for the connection in place of the
    browser: it refuses, and what it was asked is read."""
    asked: dict[str, object] = {}

    class Refused:
        def __init__(self, address: str, **options: object) -> None:
            asked.update(options)

        async def __aenter__(self) -> None:
            raise OSError("nothing listens on 9")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(capture, "connect", Refused)

    with pytest.raises(OSError, match="nothing listens"):
        asyncio.run(capture.watch("ws://127.0.0.1:9/devtools/browser/none", lambda event: None))

    assert asked == {"max_size": None, "proxy": None}


def test_the_listener_takes_only_what_the_browser_did() -> None:
    """A page can say that a click, an input, a submit or a change happened. The browser does
    not trust such an event, and neither does the listener: before anything else is done
    with an event, it is asked whether the browser did it."""
    gate = "          if (!event.isTrusted) return;\n          work(event);\n"

    assert gate in capture.LISTENER


def test_the_listener_sits_on_the_window_and_hears_first() -> None:
    """A page may stop its events from going further, at the document or lower down, so
    that nothing there hears them. The window hears first, and the listener sits on it."""
    sits = "      window.addEventListener(type, handler, true);\n"

    assert sits in capture.LISTENER
    assert "document.addEventListener" not in capture.LISTENER


def test_an_error_of_the_listener_never_reaches_the_page() -> None:
    """What the listener does with an event is wrapped, so that a fault of its own is
    nothing to the page."""
    wrapped = (
        "          work(event);\n"
        "        } catch (error) {\n"
        "          // Nothing of this listener's may reach the page.\n"
        "        }\n"
    )

    assert wrapped in capture.LISTENER


def test_the_listener_puts_itself_back_when_the_document_is_opened_again() -> None:
    """Opening a document again takes away every listener of the window and keeps the
    world the listener ran in. So the flag that keeps the listener to one in a document
    would keep it out for good."""
    watches = "    }).observe(document, { childList: true });\n"

    assert watches in capture.LISTENER
    assert capture.LISTENER.count("window.addEventListener(type, handler, true);") == 2


class Standing:
    """A stand-in for the browser's connection. It lets the capture in, keeps what it is
    sent, and never says a word. refused makes it refuse the next connection instead."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.let_in = threading.Event()
        self.attempts = 0
        self.refused: BaseException | None = None

    def __call__(self, address: str, **options: object) -> "Standing":
        return self

    async def __aenter__(self) -> "Standing":
        self.attempts += 1
        if self.refused is not None:
            raise self.refused
        self.let_in.set()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> None:
        await asyncio.Event().wait()  # a word that never comes

    async def close(self) -> None:
        return None


def awaited(condition: Callable[[], object], seconds: float = 10.0) -> object:
    """Wait, on the test's own thread, for condition to come back with something."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = condition()
        if found:
            return found
        time.sleep(0.01)
    raise AssertionError(f"waited {seconds:g} s, and did not get what was waited for")


def test_an_error_of_the_connection_that_ends_the_capture_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser that goes away says so in the library's own error. It is kept as any other
    is, and nothing is raised on the thread."""
    standing = Standing()
    standing.refused = WebSocketException("the browser went away")
    monkeypatch.setattr(capture, "connect", standing)
    recorder = capture.Recorder("ws://127.0.0.1:9/devtools/browser/none", lambda event: None)
    recorder.start()
    try:
        awaited(lambda: recorder.error)
    finally:
        recorder.stop()

    assert isinstance(recorder.error, WebSocketException)


def test_a_recorder_that_failed_once_shows_no_error_while_it_runs_well_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error is the end of a run. A run that goes on after it has none to show, until it
    ends with one."""
    standing = Standing()
    standing.refused = OSError("nothing listens on 9")
    monkeypatch.setattr(capture, "connect", standing)
    recorder = capture.Recorder("ws://127.0.0.1:9/devtools/browser/none", lambda event: None)
    recorder.start()
    try:
        awaited(lambda: recorder.error)
    finally:
        recorder.stop()

    standing.refused = None
    recorder.start()
    try:
        # Let in is not enough: the four are sent a moment after the connection is made,
        # and a test that read them at once read three of them, once in a dozen runs.
        awaited(lambda: len(standing.sent) == 4)
        assert standing.sent == [  # once: a second start starts nothing more
            '{"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": true}}',
            '{"id": 2, "method": "Target.getBrowserContexts", "params": {}}',
            '{"id": 3, "method": "Target.getTargets", "params": {}}',
            '{"id": 4, "method": "Storage.getCookies", "params": {}}',
        ]
        assert recorder.running
        assert recorder.error is None
        thread = recorder._thread
        recorder.start()  # a second start starts nothing more
        assert recorder._thread is thread  # the same thread, and no other begun
        assert standing.attempts == 2  # one refused, one let in
    finally:
        recorder.stop()

    assert recorder.error is None
    assert standing.sent[-1].startswith('{"id": 4,')  # and nothing more was sent after the stop
    assert recorder.running is False


class Slow:
    """A stand-in for a browser that takes its time to let the capture in, as one that is
    not answering does for ten seconds, and then gives up."""

    def __init__(self) -> None:
        self.gave_up = False

    def __call__(self, address: str, **options: object) -> "Slow":
        return self

    async def __aenter__(self) -> "Slow":
        await asyncio.sleep(3)
        self.gave_up = True
        raise TimeoutError("the browser did not let the capture in")

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_a_capture_stopped_before_the_browser_let_it_in_stops_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop asked for while the browser was being reached waited for the connection to
    succeed or to give up, and came back as a timeout. It is neither waited for nor an
    error."""
    slow = Slow()
    monkeypatch.setattr(capture, "connect", slow)
    recorder = capture.Recorder("ws://127.0.0.1:9/devtools/browser/none", lambda event: None)
    recorder.start()
    time.sleep(0.1)  # in the attempt
    began = time.monotonic()

    recorder.stop()

    assert time.monotonic() - began < 1
    assert recorder.error is None
    assert recorder.running is False
    assert slow.gave_up is False  # given up, and not waited for


# ----------------------------------------------- what the record holds, and forgetting ---


def written(home: Path, day: str, *kinds: str) -> Path:
    """A file of the capture that holds one event of each kind, written on that day."""
    when = f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:0"
    path: Path | None = None
    for number, kind in enumerate(kinds):
        event = activity.Event(
            1,
            f"{when}{number}",
            kind,
            "example.org",
            "/",
            "1A2B3C4D",
            activity.Action(kind, "button", "b", "text" if kind == "input" else None),
        )
        path = activity.write_event(home, event, now=activity.epoch(event.ts))
    assert path is not None
    return path


def test_the_review_says_of_each_day_what_it_holds_oldest_first(tmp_path: Path) -> None:
    """One line per day that is held: the day, how many events, the size of the file, and
    how many of each kind, the most first. A last line says what the settings keep."""
    older = written(tmp_path, "20261027", "click", "select", "click")
    newer = written(tmp_path, "20261120", "input", "click")

    said = capture.review(tmp_path, today=TODAY)

    assert said == (
        f"2026-10-27   3 events   {older.stat().st_size} bytes   click 2, select 1\n"
        f"2026-11-20   2 events   {newer.stat().st_size} bytes   click 1, input 1\n"
        "30 days are kept, as the settings say. Files written before 2027-03-01 will be deleted."
    )


def test_the_review_takes_the_days_kept_from_the_settings(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[capture]\nkeep_days = 7\n", encoding="utf-8")

    said = capture.review(tmp_path, today=TODAY)

    assert said == (
        "The capture holds no day.\n"
        "7 days are kept, as the settings say. Files written before 2027-03-24 will be deleted."
    )


def test_the_review_reads_and_writes_nothing(tmp_path: Path) -> None:
    written(tmp_path, "20261027", "click")
    others(tmp_path / "watch")
    before = {path: path.read_bytes() for path in (tmp_path / "watch").iterdir()}

    capture.review(tmp_path, today=TODAY)

    after = {path: path.read_bytes() for path in (tmp_path / "watch").iterdir()}
    assert after == before


def test_the_review_counts_a_line_it_cannot_read_and_no_other_file(tmp_path: Path) -> None:
    """The spike logger's files are not the capture's, and neither is a file of eight
    digits that are no day."""
    kept = written(tmp_path, "20261027", "click")
    with kept.open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")
    others(tmp_path / "watch")

    (line, last) = capture.review(tmp_path, today=TODAY).splitlines()

    assert line.startswith("2026-10-27   1 event   ")
    assert line.endswith("   click 1, unreadable 1")
    assert last.startswith("30 days are kept")


def test_the_review_says_what_the_older_logger_holds_by_the_day_of_each_line(
    tmp_path: Path,
) -> None:
    """While both run, the mine reads both records, and the review showed the capture's
    days alone. A file of the older logger is named for the day it started and holds lines
    of many days: each is counted on the day of its line, and a line that holds no event is
    not. The title on a line is not read."""
    written(tmp_path, "20261126", "click", "input")
    title = "a title that must not survive"
    logged = [
        {
            "ts": "2026-11-24T09:00:00",
            "kind": "nav",
            "target": "A1B2C3D4",
            "url": "https://one.example/a",
            "title": title,
        },
        {
            "ts": "2026-11-24T09:05:00",
            "kind": "nav",
            "url": "https://one.example/c",
            "title": title,
        },
        {
            "ts": "2026-11-24T09:06:00",
            "kind": "focus",
            "url": "https://one.example/b",
            "title": title,
        },
        {
            "ts": "2026-11-26T10:00:00",
            "kind": "tab_open",
            "target": "A1B2C3D4",
            "url": "https://two.example/",
            "title": title,
        },
        {
            "ts": "2026-11-26T10:01:00",
            "kind": "focus",
            "url": "https://two.example/inbox",
            "title": title,
        },
        {"ts": "2026-11-26T10:02:00", "kind": "chrome_down", "error": "invented"},
    ]
    (tmp_path / "watch" / "spike-20261124.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in logged) + "a line that is nobody's to read\n",
        encoding="utf-8",
    )

    said = capture.review(tmp_path, today=TODAY)

    (first, *rest) = said.splitlines()
    assert first.startswith("2026-11-26   2 events   ")
    assert rest == [
        "The older logger holds:",  # the most first, and by name where as many
        "2026-11-24   3 events   navigate 2, focus 1",
        "2026-11-26   2 events   focus 1, navigate 1",
        "30 days are kept, as the settings say. Files written before 2027-03-01 will be deleted.",
    ]
    assert title not in said


def test_the_review_says_when_only_the_older_logger_holds_a_day(tmp_path: Path) -> None:
    (tmp_path / "watch").mkdir()
    line = {
        "ts": "2026-11-24T09:00:00",
        "kind": "nav",
        "target": "A1B2C3D4",
        "url": "https://one.example/",
    }
    (tmp_path / "watch" / "spike-20261124.jsonl").write_text(
        json.dumps(line) + "\n", encoding="utf-8"
    )

    assert capture.review(tmp_path, today=TODAY) == (
        "The capture holds no day.\n"
        "The older logger holds:\n"
        "2026-11-24   1 event   navigate 1\n"
        "30 days are kept, as the settings say. Files written before 2027-03-01 will be deleted."
    )


def test_the_review_counts_a_line_cut_in_the_middle_of_a_character(tmp_path: Path) -> None:
    """A crash or a power loss can leave half of a character at the end of a day's file.
    Before, the review died on it, with UnicodeDecodeError, and said nothing of the day."""
    kept = written(tmp_path, "20261027", "click")
    with kept.open("ab") as stream:
        stream.write(b'{"v": 1, "fingerprint": "\xf0\x9f')  # inside an emoji

    (line, last) = capture.review(tmp_path, today=TODAY).splitlines()

    assert line.startswith("2026-10-27   1 event   ")
    assert line.endswith("   click 1, unreadable 1")
    assert last.startswith("30 days are kept")


def test_forget_names_what_is_before_the_day_and_deletes_nothing_without_a_yes(
    tmp_path: Path,
) -> None:
    old = held(tmp_path, "20261001")
    kept = held(tmp_path, "20261015")

    said = capture.forget(tmp_path, "2026-10-10")

    assert said == (
        "1 file is held from before 2026-10-10: activity-20261001.jsonl\n"
        "--yes is needed to delete it."
    )
    assert old.exists() and kept.exists()


def test_forget_names_more_than_one_file_as_more_than_one(tmp_path: Path) -> None:
    """The words said "2 files is held" for as long as more than one was, and the one test
    of these words held a single file."""
    older = held(tmp_path, "20261001")
    old = held(tmp_path, "20261009")

    said = capture.forget(tmp_path, "2026-10-10")

    assert said == (
        "2 files are held from before 2026-10-10: "
        "activity-20261001.jsonl, activity-20261009.jsonl\n"
        "--yes is needed to delete them."
    )
    assert older.exists() and old.exists()


def test_forget_with_a_yes_deletes_what_is_before_the_day_and_nothing_else(tmp_path: Path) -> None:
    older = held(tmp_path, "20261001")
    old = held(tmp_path, "20261009")
    kept = held(tmp_path, "20261010")  # the day itself is kept: only what is before it goes
    untouched = others(tmp_path / "watch")

    said = capture.forget(tmp_path, "2026-10-10", yes=True)

    assert said == (
        "Deleted 2 files held from before 2026-10-10: "
        "activity-20261001.jsonl, activity-20261009.jsonl"
    )
    assert not older.exists() and not old.exists()
    assert kept.exists()
    assert all(path.exists() for path in untouched)
    assert sorted(path.name for path in (tmp_path / "watch").iterdir()) == sorted(
        [kept.name, *(path.name for path in untouched)]
    )


def test_forget_when_nothing_is_held_from_before_the_day_says_so(tmp_path: Path) -> None:
    held(tmp_path, "20261010")

    assert (
        capture.forget(tmp_path, "2026-10-10", yes=True)
        == "Nothing is held from before 2026-10-10."
    )


@pytest.mark.parametrize(
    "before",
    ["2026-10-1", "10/10/2026", "20261010", "2026-13-01", "2026-02-30", "", "yesterday"],
)
def test_forget_refuses_a_day_that_is_no_day(tmp_path: Path, before: str) -> None:
    held(tmp_path, "20261001")

    with pytest.raises(CaptureError, match="is no day"):
        capture.forget(tmp_path, before, yes=True)
    assert (tmp_path / "watch" / "activity-20261001.jsonl").exists()


@pytest.mark.parametrize(
    ("size", "said"),
    [
        pytest.param(0, "0 bytes", id="nothing"),
        pytest.param(512, "512 bytes", id="less than a kilobyte"),
        pytest.param(1024, "1.0 kB", id="a kilobyte"),
        pytest.param(1536, "1.5 kB", id="a kilobyte and a half"),
        pytest.param(5 * 1024 * 1024, "5.0 MB", id="megabytes"),
    ],
)
def test_the_size_of_a_file_is_written_as_a_person_reads_it(size: int, said: str) -> None:
    assert capture._size(size) == said


def test_with_no_home_given_the_record_is_read_in_the_seeress_home(home: Path) -> None:
    """The commands give no home: the Seeress home is read. Of the test that is the
    throwaway one, `.seeress` inside the home of the test, and nothing else."""
    seeress = home / ".seeress"
    held(seeress, "20261001")

    assert capture.review().startswith("2026-10-01   1 event   ")
    assert capture.status() == "Capture has never run here."
    assert capture.forget(None, "2026-10-10", yes=True).startswith(
        "Deleted 1 file held from before"
    )
    assert list((seeress / "watch").iterdir()) == []


# ------------------------------------------------------------------- the logger ---


def test_the_logger_connects_and_stops_when_its_time_is_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO, logger="seeress.capture")  # what it says is info
    standing = Standing()
    monkeypatch.setattr(capture, "connect", standing)
    began = time.monotonic()

    said = capture.run("ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=0.2)

    assert said == 0
    assert time.monotonic() - began < 2
    assert len(standing.sent) == 4  # it connected, once
    assert capture.status(tmp_path).startswith("Capture is not running. It last reported stopped")
    said_lines = [record.getMessage() for record in caplog.records]
    assert [line for line in said_lines if "events written" in line] == [
        "0 events written; 0 outside the schedule; 0 over the cap"
    ]
    assert any("It last reported stopped at" in line for line in said_lines)
    assert capsys.readouterr().out == ""  # the library prints nothing


def test_with_no_address_given_the_logger_reads_it_where_chrome_publishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup the personal door uses, and no other: where Chrome publishes the address
    for this user. The stand-in notes the door it was asked for."""
    standing = Standing()
    asked: list[str | None] = []
    doors: list[str] = []

    def connect(address: str | None, **options: object) -> Standing:
        asked.append(address)
        return standing

    def published(door: str) -> dict[str, str]:
        doors.append(door)
        return {"BU_CDP_WS": "ws://127.0.0.1:9/published"}

    monkeypatch.setattr(capture, "connect", connect)
    monkeypatch.setattr(config, "browser_env", published)

    assert capture.run(home=tmp_path, seconds=0.1) == 0

    assert asked == ["ws://127.0.0.1:9/published"]
    assert doors == ["personal"]


def test_when_chrome_publishes_no_address_the_logger_connects_to_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chrome is not running, or remote debugging is off. Nothing is connected to: the
    status says why, and it tries again."""
    asked: list[str | None] = []

    def connect(address: str | None, **options: object) -> Standing:
        asked.append(address)
        raise AssertionError("nothing must be connected to")

    monkeypatch.setattr(capture, "connect", connect)
    monkeypatch.setattr(config, "browser_env", lambda profile: {})

    assert capture.run(home=tmp_path, seconds=0.1) == 1

    assert asked == []
    said = capture.status(tmp_path)
    assert said.startswith("Capture is down since ")
    assert "publishes no address" in said


class Refused:
    """A recorder that ends at once, with its error kept and its status told. It stands in
    for the capture's own where the logger's loop is the point and its threads are not."""

    made: ClassVar[list["Refused"]] = []

    def __init__(
        self,
        address: str,
        emit: Callable[[activity.Event], None],
        *,
        keep: Callable[[], None] | None = None,
        every: float = 3600.0,
        status: capture.Status | None = None,
    ) -> None:
        self.address = address
        self.status = status
        self.error: BaseException | None = None
        self.running = False
        Refused.made.append(self)

    def start(self) -> None:
        self.error = OSError("nothing listens on 9")
        if self.status is not None:
            self.status.state("down", "OSError: nothing listens on 9")

    def stop(self) -> None:
        return None


class Stopped(threading.Event):
    """A stop that, once set, still takes its time when it is waited on.

    The logger waits on its stop between two looks at the capture. Under a rule broken it
    takes no notice of the stop, and a wait that returned at once set it turning in a tight
    loop, in a thread of its own: which starved the run of every test after it. Correct code
    never waits on a stop that is set.
    """

    def wait(self, timeout: float | None = None) -> bool:
        time.sleep(timeout or 0)
        return self.is_set()


class Clock(threading.Event):
    """A clock the test moves, and a stop that is set when the test says. Each wait advances
    the clock by what was waited, and nothing is waited for in truth."""

    def __init__(self, *, stop_after_waits: int | None = None) -> None:
        super().__init__()
        self.now = 0.0
        self.waits: list[float | None] = []
        self.stop_after_waits = stop_after_waits
        self.stopped = False

    def monotonic(self) -> float:
        return self.now

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        self.now += timeout or 0.0
        if self.stop_after_waits is not None and len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def test_the_logger_tries_again_after_a_pause_when_the_connection_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser is away: each attempt ends at once, and the next comes RETRY_SECONDS
    later, for as long as the logger runs. A clock the test moves, so that the count is
    exact: at 0, 5, 10 and 15 seconds, and not after 16."""
    Refused.made.clear()
    clock = Clock()
    monkeypatch.setattr(capture, "Recorder", Refused)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    said = capture.run(
        "ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=16, stop=clock
    )

    assert said == 1
    assert [refused.address for refused in Refused.made] == [
        "ws://127.0.0.1:9/devtools/browser/one"
    ] * 4
    assert clock.waits == [
        5.0,
        5.0,
        5.0,
        5.0,
    ]  # the pause, and no more: a recorder that has ended is not waited for
    assert "nothing listens on 9" in capture.status(tmp_path)


class Unwritten(Refused):
    """Refused, where the status cannot be written, as on a full disk: the capture's own lets
    that error go, and keeps the one that ended it."""

    def start(self) -> None:
        with contextlib.suppress(OSError):
            super().start()


def test_the_logger_says_why_the_capture_ended_when_the_status_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full disk: the status file keeps what it last said, and `capture-status` reads that.
    Before, the reason reached nobody: the logger went on trying, four times in sixteen
    seconds, and its last words were the stale status. The reason is said once as it goes,
    however many attempts end with it, and once more at the end."""

    def no_space(*args: object, **kwargs: object) -> None:
        raise OSError("No space left on device")

    Refused.made.clear()
    clock = Clock()
    monkeypatch.setattr(capture, "Recorder", Unwritten)
    monkeypatch.setattr(capture, "write_status", no_space)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    caplog.set_level(logging.INFO, logger="seeress.capture")

    said = capture.run(
        "ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=16, stop=clock
    )

    assert said == 1
    assert len(Refused.made) == 4
    why = [
        record.getMessage()
        for record in caplog.records
        if "nothing listens on 9" in record.getMessage()
    ]
    assert len(why) == 2
    assert "the capture ended" in why[0]
    assert "last capture ended" in why[1]


class Sometimes(Refused):
    """Refused with a reason for each attempt: one for each of the first attempts as given,
    and the last of them for every attempt after."""

    reasons: ClassVar[list[str]] = []

    def start(self) -> None:
        given = min(len(Refused.made) - 1, len(self.reasons) - 1)
        self.error = OSError(self.reasons[given])
        if self.status is not None:
            with contextlib.suppress(OSError):
                self.status.state("down", f"OSError: {self.error}")


def test_the_logger_says_a_reason_again_only_when_it_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four attempts: two reasons, so the reason is said twice as it goes, and once at the
    end. Not twelve times in a minute for one fault that stays."""
    Sometimes.reasons = ["the port is closed", "the browser refused"]
    Refused.made.clear()
    clock = Clock()
    monkeypatch.setattr(capture, "Recorder", Sometimes)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    caplog.set_level(logging.INFO, logger="seeress.capture")

    capture.run("ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=16, stop=clock)

    assert len(Refused.made) == 4
    said = [
        record.getMessage()
        for record in caplog.records
        if "the capture ended" in record.getMessage()
    ]
    assert said == [
        "the capture ended: OSError: the port is closed",
        "the capture ended: OSError: the browser refused",
    ]


def test_a_stop_ends_the_logger_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The signal comes in the first pause. No second attempt is made, and nothing more is
    waited for."""
    Refused.made.clear()
    clock = Clock(stop_after_waits=1)
    monkeypatch.setattr(capture, "Recorder", Refused)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    said = capture.run(
        "ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=16, stop=clock
    )

    assert said == 1  # it was down when it was stopped
    assert len(Refused.made) == 1
    assert clock.waits == [5.0]


def test_the_entry_point_takes_an_address_a_home_and_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def run(
        address: str | None = None,
        *,
        home: Path | None = None,
        seconds: float | None = None,
        stop: threading.Event | None = None,
    ) -> int:
        seen.update(address=address, home=home, seconds=seconds, stopped=stop is not None)
        return 0

    history: list[tuple[int, object]] = []

    def noted(signum: int, handler: object) -> int:
        history.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(capture, "run", run)
    monkeypatch.setattr(signal, "signal", noted)

    argv = ["ws://127.0.0.1:9/devtools/browser/one", "--home", str(tmp_path), "--seconds", "0.5"]

    assert capture.main(argv) == 0

    assert seen == {
        "address": "ws://127.0.0.1:9/devtools/browser/one",
        "home": tmp_path,
        "seconds": 0.5,
        "stopped": True,
    }
    assert [number for number, _ in history] == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGHUP,
    ]  # the three are taken, and given back


def test_a_signal_that_asks_the_entry_point_to_end_ends_it_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Closing the terminal, or a launch agent being unloaded, sends one. The logger stops
    as it ran, and says so. The signal is sent for real, to this process: a handler of the
    test's own stands by first, so that were none installed the test would end, and not the
    run."""
    logging.getLogger("seeress").handlers.clear()  # a fresh stream for this test
    standing = Standing()
    monkeypatch.setattr(capture, "connect", standing)
    real = capture.run
    # The handler that was there, to be put back at the end. Before, the one of the test's
    # was left behind for the rest of the session.
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: None)

    def in_hand(
        address: str | None = None,
        *,
        home: Path | None = None,
        seconds: float | None = None,
        stop: threading.Event | None = None,
    ) -> int:
        threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return real(address, home=home, seconds=30, stop=stop)

    monkeypatch.setattr(capture, "run", in_hand)

    began = time.monotonic()
    argv = ["ws://127.0.0.1:9/devtools/browser/one", "--home", str(tmp_path)]

    try:
        assert capture.main(argv) == 0
        assert time.monotonic() - began < 5
        assert "It last reported stopped at" in capsys.readouterr().err
    finally:
        signal.signal(signal.SIGTERM, before)


def test_the_entry_point_exits_one_and_says_why_when_the_capture_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logging.getLogger("seeress").handlers.clear()  # a fresh stream for this test
    standing = Standing()
    standing.refused = OSError("nothing listens on 9")
    monkeypatch.setattr(capture, "connect", standing)

    said = capture.main(
        ["ws://127.0.0.1:9/devtools/browser/one", "--home", str(tmp_path), "--seconds", "0.1"]
    )

    assert said == 1
    assert "nothing listens on 9" in capsys.readouterr().err


def test_a_settings_file_seeress_cannot_read_is_refused_by_the_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logging.getLogger("seeress").handlers.clear()  # a fresh stream for this test
    (tmp_path / "config.toml").write_text("[capture]\nkeep_days = 0\n", encoding="utf-8")

    assert capture.main(["ws://127.0.0.1:9/x", "--home", str(tmp_path), "--seconds", "0.1"]) == 1

    assert "keep_days" in capsys.readouterr().err


def test_the_entry_point_exits_one_and_says_why_when_the_status_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Chrome publishes no address, and the status that would say so cannot be written, as
    on a full disk. The error is said as the other reasons are, and the exit is one. An
    error left uncaught there ends the command with a traceback."""

    def connect(address: str, **options: object) -> Standing:
        raise AssertionError("nothing must be connected to")

    def no_space(*args: object, **kwargs: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(capture, "connect", connect)
    monkeypatch.setattr(config, "browser_env", lambda profile: {})
    monkeypatch.setattr(capture, "write_status", no_space)
    caplog.set_level(logging.ERROR, logger="seeress.capture")

    said = capture.main(["--home", str(tmp_path)])

    assert said == 1
    assert [record.getMessage() for record in caplog.records] == [
        "the capture cannot run: No space left on device"
    ]


# ------------------------------------------ what the seventh review found no test held ---


class Closing(Standing):
    """Standing, which counts how often it is asked to close its connection."""

    closed: ClassVar[int] = 0

    async def close(self) -> None:
        Closing.closed += 1


class SlowToClose(Standing):
    """Standing, which takes half a second to close its connection."""

    async def close(self) -> None:
        await asyncio.sleep(0.5)


NOWHERE = "ws://127.0.0.1:9/devtools/browser/none"


def test_the_connection_is_closed_when_the_capture_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each reconnect leaked a websocket until it was collected."""
    Closing.closed = 0
    closing = Closing()
    monkeypatch.setattr(capture, "connect", closing)
    recorder = capture.Recorder(NOWHERE, lambda event: None)
    recorder.start()
    try:
        awaited(closing.let_in.is_set)
    finally:
        recorder.stop()

    assert Closing.closed == 1


def test_the_loop_of_the_recorder_is_closed_when_the_capture_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector and a descriptor leaked per capture."""
    monkeypatch.setattr(capture, "connect", Standing())
    made: list[asyncio.AbstractEventLoop] = []
    new_loop = asyncio.new_event_loop

    def noted() -> asyncio.AbstractEventLoop:
        made.append(new_loop())
        return made[-1]

    monkeypatch.setattr(asyncio, "new_event_loop", noted)
    recorder = capture.Recorder(NOWHERE, lambda event: None)
    recorder.start()
    try:
        awaited(lambda: made and made[0].is_running())
    finally:
        recorder.stop()

    assert len(made) == 1 and made[0].is_closed()


def test_with_no_home_given_the_review_reads_where_seeress_home_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEERESS_HOME moves the Seeress home, and the commands of the record follow it. Read
    under the home folder instead, they spoke of another record."""
    elsewhere, home_folder = tmp_path / "elsewhere", tmp_path / "home"
    written(elsewhere, "20261027", "click")
    monkeypatch.setenv("SEERESS_HOME", str(elsewhere))
    monkeypatch.setattr(Path, "home", lambda: home_folder)

    (line, _) = capture.review(today=TODAY).splitlines()

    assert line.startswith("2026-10-27   1 event")


def test_the_logger_s_gate_takes_its_schedule_from_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings name the schedule. A gate left on the default recorded outside the
    hours a person chose, and not in all of them."""
    (tmp_path / "config.toml").write_text(
        '[capture]\nfrom = "22:00"\nto = "24:00"\n', encoding="utf-8"
    )
    schedules: list[object] = []

    class Noted(capture.Gate):
        def __init__(
            self,
            home: Path,
            *,
            schedule: activity.Schedule = activity.DEFAULT_SCHEDULE,
            status: capture.Status | None = None,
        ) -> None:
            super().__init__(home, schedule=schedule, status=status)
            schedules.append(schedule)

    monkeypatch.setattr(capture, "Gate", Noted)
    Refused.made.clear()
    clock = Clock()
    monkeypatch.setattr(capture, "Recorder", Refused)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    capture.run("ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=16, stop=clock)

    assert schedules == [config.CaptureSettings(start=22, end=23).schedule]


def test_the_logger_keeps_the_days_the_settings_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text("[capture]\nkeep_days = 7\n", encoding="utf-8")
    kept: list[tuple[Path, int]] = []

    def retention(home: Path, days: int) -> list[Path]:
        kept.append((home, days))
        return []

    monkeypatch.setattr(capture, "retention", retention)
    standing = Standing()
    monkeypatch.setattr(capture, "connect", standing)
    stop = Stopped()
    said: list[int] = []
    # A daemon: under a rule broken, a logger that never ends must not keep the process
    # alive after its own summary. One did, for four minutes, with the run already told,
    # and it was turning all the while.
    logger = threading.Thread(
        target=lambda: said.append(capture.run(NOWHERE, home=tmp_path, stop=stop)), daemon=True
    )
    logger.start()
    try:
        awaited(standing.let_in.is_set)
        awaited(lambda: kept)
    finally:
        stop.set()
        logger.join(10)

    assert kept == [(tmp_path, 7)]
    assert not logger.is_alive(), "the logger did not end when it was stopped"


def test_stop_waits_for_the_thread_of_the_capture_however_long_it_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop returns when the capture has ended, and not before. One that waited for a
    moment only returned with the capture still closing its connection."""
    slow = SlowToClose()
    monkeypatch.setattr(capture, "connect", slow)
    recorder = capture.Recorder(NOWHERE, lambda event: None)
    recorder.start()
    try:
        awaited(slow.let_in.is_set)
    finally:
        recorder.stop()

    assert recorder.running is False


def test_no_path_of_the_record_reaches_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A log is not the record: it has none of the record's protection, retention or
    forget. What the capture writes of an event is a count."""
    caplog.set_level(logging.DEBUG, logger="seeress")
    all_day = activity.Schedule(frozenset({0, 1, 2, 3, 4, 5, 6}), 0, 23)
    gate = capture.Gate(tmp_path, schedule=all_day, status=capture.Status(tmp_path))
    event = activity.Event(
        1, "2026-11-20T09:15:00", "click", "example.org", "/private/invoices/7", T1[:8]
    )

    passed(gate, [event])

    assert "/private/invoices/7" not in caplog.text


SITE = "a-site-that-must-never-be-logged.example"
PATH = "/a-path/that-must-never-be-logged/7"
FINGERPRINT = 'button[data-testid="never-logged"]'
ONE = activity.Event(
    1, TS, "click", SITE, PATH, T1[:8], activity.Action("click", "button", FINGERPRINT, None)
)


class Through(Refused):
    """Refused, after one event has passed through the gate it was given, as the capture's
    own recorder does when the listener of a tab reports."""

    def __init__(
        self,
        address: str,
        emit: Callable[[activity.Event], None],
        *,
        keep: Callable[[], None] | None = None,
        every: float = 3600.0,
        status: capture.Status | None = None,
    ) -> None:
        self.gate = emit
        super().__init__(address, emit, keep=keep, every=every, status=status)

    def start(self) -> None:
        self.gate(ONE)
        super().start()


def test_no_site_or_path_of_the_record_reaches_the_log_through_the_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A log is not the record: it has none of the record's protection, retention or forget.
    The gate alone was held to this, and a logger that said each event's site and path as
    its run ended passed every test. One event passes through the real gate, into the real
    record under the home, and the run of the logger ends: what is said is a count."""
    caplog.set_level(logging.DEBUG, logger="seeress")
    clock = Clock()
    monkeypatch.setattr(capture, "Recorder", Through)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    said = capture.run(
        "ws://127.0.0.1:9/devtools/browser/one", home=tmp_path, seconds=1, stop=clock
    )

    assert said == 1
    assert clock.waits == [5.0]  # one attempt, and the clock ran out in the pause after it
    kept = [
        (event.site, event.path, event.action.fingerprint if event.action else None)
        for event in activity.read_events(tmp_path)
    ]
    assert kept == [(SITE, PATH, FINGERPRINT)]  # it is in the record, so a log could hold it
    lines = [record.getMessage() for record in caplog.records]
    assert [line for line in lines if "events written" in line] == [
        "1 events written; 0 outside the schedule; 0 over the cap"
    ]
    out, err = capsys.readouterr()
    for line in (*lines, out, err):
        assert SITE not in line
        assert PATH not in line
        assert FINGERPRINT not in line


def test_the_handlers_there_were_are_put_back_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's own handler, and not the default one."""

    def mine(*_: object) -> None:
        return None

    monkeypatch.setattr(capture, "connect", Standing())
    before = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, mine)
    try:
        capture.main([NOWHERE, "--home", str(tmp_path), "--seconds", "0"])
        assert signal.getsignal(signal.SIGHUP) is mine
    finally:
        signal.signal(signal.SIGHUP, before)
