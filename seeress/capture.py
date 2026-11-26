"""The capture: a listener in every page of a browser, and what it reports.

The capture is a second client of the browser's DevTools connection. It attaches to every
tab of the browser's default context, and so never to a private window, and in each it runs
a listener, in a world of the page that the page's own scripts cannot reach. The listener
reports what the browser itself did: a click, an input, a submit and a select. It reads an
element's tag, three attributes that name it and two that say what kind of a field it is,
and nothing else: never what a field holds, never an attribute that holds prose, and never
the text of a page.

The site and the path come from the browser, not from the page. The capture takes them from
the address the browser last gave for the tab, without the query and the fragment. So no
page can say what site it is on.

Before an event is written it passes a gate: only in the schedule, and no more than two
hundred a minute from one tab. What is refused is counted and not written.

The record is kept for a number of days and no longer: retention deletes the files of
the capture whose day is past them, and touches no other file. And the capture says of
itself, in one file, whether it is recording and since when, or why it is not: status
reads that file.

Nothing here looks for a browser. The capture takes an address, and whose browser is behind
it is for the caller to know. This module starts nothing on anyone's browser.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Protocol, TypeGuard
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException
from websockets.typing import Data

from . import activity, config, log
from .activity import DEFAULT_SCHEDULE, Event, Schedule, due, epoch, write_event
from .errors import CaptureError, SeeressError

# The cookie by which Seeress knows a browser for its own, named once with the door that
# plants it. A browser that carries it is one Seeress started for a door, and nothing in it
# is a person's work.
MARKER = config.MARKER_KEY
MARKER_SITE = config.MARKER_HOST

# What the listener reports through, and the world of the page it runs in. A world of its own
# keeps the binding and the listener out of the reach of the page's own scripts: a page can
# neither report in the listener's place nor change what it reads.
BINDING = "seeressEvent"
WORLD = "seeress"

# What the capture says of itself, for capture-status to read.
STATUS_FILE = "capture-status.json"
# Every field a report holds. The shape is contracts/capture-status.schema.json, and a
# contract test keeps the two in agreement: a report short of a field is no report.
STATUS_REQUIRED = frozenset({"pid", "state", "reason", "since", "events", "last_event"})

# How often the record is kept short again, and how long a count of events may wait
# before it is written.
HOURLY = 3600.0
STATUS_EVERY = 60.0

# What runs in every tab. It is a raw string, so the backslashes of its regular expressions
# are its own: a raw string has no continued lines, and the first line is the first line.
LISTENER = r"""(() => {
  "use strict";
  try {
    // Only what a person does in the page itself is theirs. A frame inside the page, an
    // advertisement for one, is not listened to.
    if (window.top !== window) return;
    // Once in a document. The flag lives in this world alone, where the page cannot see it.
    if (window.__seeressListening) return;
    window.__seeressListening = true;

    // An identifier with a digit in it is another one on every visit, so its digits become #.
    const stable = (text) => text.replace(/[0-9]/g, "#");

    // An attribute may hold an unpaired surrogate, and no file can hold one. The name is
    // made whole in the page: an unpaired one becomes the replacement character, and the
    // person's click is recorded. Before, writing such a click ended the capture in every
    // tab, and one attribute in one page could end it.
    const whole = (text) => text.toWellFormed();

    // Three attributes may name an element, as tag[name="value"], or none of them does.
    // An id with a digit in it is not stable, so it does not name an element. No attribute
    // that holds prose names one: an aria-label says something, often about a person, and a
    // page writes what it likes in it.
    const attribute = (element, name, text) => {
      if (!text) return null;
      if (name === "id" && /[0-9]/.test(text)) return null;
      return element.localName + "[" + name + '="' + stable(whole(text)) + '"]';
    };
    const naming = (element) =>
      attribute(element, "data-testid", element.getAttribute("data-testid")) ||
      attribute(element, "id", element.getAttribute("id")) ||
      attribute(element, "name", element.getAttribute("name"));

    // An element that no attribute names is told apart by its place and the place of three
    // parents, the outermost first.
    const place = (element) => {
      const places = [];
      for (let at = element; at && places.length < 4; at = at.parentElement) {
        const parent = at.parentElement;
        const index = parent ? Array.prototype.indexOf.call(parent.children, at) + 1 : 1;
        places.unshift(at.localName + ":nth-child(" + index + ")");
      }
      return places.join(">");
    };

    // The binding is looked for when there is something to report, and not once at the
    // start: a document may have this script before the browser has put the binding in
    // its world.
    const report = (type, element, field) => {
      const send = window.seeressEvent;
      if (typeof send !== "function") return;
      const said = {
        type: type,
        // Cut here too: a page may name a custom element as long as it likes.
        tag: Array.from(element.localName).slice(0, 100).join(""),
        // Cut on code points, and never inside one. Cut on the page's units of sixteen
        // bits, a name that ends in an emoji would end in half of it, and no file could
        // hold the line it is written on.
        fingerprint: Array.from(naming(element) || place(element))
          .slice(0, 200)
          .join(""),
      };
      if (field) said.field = field;
      send(JSON.stringify(said));
    };

    const kinds = new Set([
      "text", "search", "email", "tel", "url", "number", "date", "checkbox", "radio", "file",
    ]);
    // The kind of a field, from what the page says the field is, and never from what it holds.
    // The hints are the tokens of the autocomplete attribute, wherever they come in it.
    const kindOf = (element) => {
      const type = (element.getAttribute("type") || "").toLowerCase();
      const autocomplete = element.getAttribute("autocomplete") || "";
      const hints = autocomplete.toLowerCase().split(/[ \t\n\r\f]+/);
      if (
        type === "password" ||
        hints.includes("current-password") ||
        hints.includes("new-password")
      ) {
        return "password";
      }
      if (hints.includes("one-time-code") || type === "otp") return "one-time-code";
      if (hints.some((hint) => hint.startsWith("cc-"))) return "card";
      return kinds.has(type) ? type : "other";
    };

    const elementOf = (event) => {
      const element = event.target;
      return element && element.nodeType === 1 ? element : null;
    };

    // The field being typed into, since it last got the focus. A field is reported once a
    // visit of the keyboard, not once a keystroke.
    let typing = null;

    // Each listener is wrapped, so that nothing of this listener's may reach the page. And
    // only what the browser itself did is the person's: a page's script can say that a
    // click, an input or a change happened, and the browser does not trust what it says.
    // Each sits on the window, where it hears first: a page that stops its events from
    // going further, at the document or below, cannot keep them from it.
    const handlers = [];
    const on = (type, work) => {
      const handler = (event) => {
        try {
          if (!event.isTrusted) return;
          work(event);
        } catch (error) {
          // Nothing of this listener's may reach the page.
        }
      };
      handlers.push([type, handler]);
      window.addEventListener(type, handler, true);
    };

    on("click", (event) => {
      const element = elementOf(event);
      if (element) report("click", element);
    });
    on("input", (event) => {
      const element = elementOf(event);
      // A select says that it changed as an input too. It is reported once, as a select.
      if (!element || typing === element || element.localName === "select") return;
      typing = element;
      report("input", element, kindOf(element));
    });
    on("submit", (event) => {
      const element = elementOf(event);
      if (element) report("submit", element);
    });
    on("change", (event) => {
      const element = elementOf(event);
      if (element && element.localName === "select") report("select", element);
    });
    on("focusout", (event) => {
      if (typing === event.target) typing = null;
    });

    // A page that opens its document again takes away every listener of the window and
    // keeps this world, so that the flag above would keep the listener out for good. A
    // script that writes to a loaded page does this. When the children of the document
    // itself are taken away, the listeners are put back.
    const arm = () => {
      for (const [type, handler] of handlers) window.addEventListener(type, handler, true);
    };
    new MutationObserver((records) => {
      const reopened = records.some((record) =>
        Array.from(record.removedNodes).some((node) => node.nodeName === "HTML")
      );
      if (reopened) arm();
    }).observe(document, { childList: true });
  } catch (error) {
    // Nothing of this listener's may reach the page.
  }
})();
"""


def event_for(payload: str, address: str, target: str, when: float) -> Event | None:
    """The event in what the listener of a tab reported, or None when there is none.

    payload is what came through the binding. address is the address the browser last gave
    for the tab, and target the tab's identifier. when is the second, in seconds since the
    epoch. The site and the path are taken from the address, without its query and its
    fragment, its user information and its port.

    An address that is no page on the web makes no event. So does a payload that is not JSON,
    or not of the shape the schema asks for: the action is read by the same rules that read
    one back from the record.
    """
    try:
        action: object = json.loads(payload)
        parts = urlsplit(address)
    except ValueError:  # not JSON, or an address Python cannot split, such as "https://[::1"
        return None
    if not isinstance(action, dict) or parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if host is None:
        return None
    try:
        return Event.parse(
            {
                "v": 1,
                "ts": activity.clock(when),
                "kind": action.get("type"),
                "site": host.removeprefix("www."),
                "path": activity.path_at_most(parts.path or "/"),
                "target": target[:8],
                "action": action,
            }
        )
    except ValueError:
        return None


# --------------------------------------------------------- answering the browser ---


@dataclass
class _Tab:
    """What the capture holds for one tab: the address the browser last gave for it, the
    context it is in, and its session once it is attached."""

    address: str
    context: object
    session: str | None = None
    attaching: bool = False


def _is_a_page(info: object) -> TypeGuard[Mapping[str, Any]]:
    """True for what the browser says of a tab, and of no other kind of target."""
    return (
        isinstance(info, Mapping)
        and info.get("type") == "page"
        and isinstance(info.get("targetId"), str)
    )


class Sessions:
    """What one capture holds about the tabs of a browser, and how it answers the browser.

    The browser speaks in messages: events, which say what happened, and answers to the
    commands the capture sent. hears takes one message and returns the commands to send back.
    The connection is not here, so all of this can be tried without a browser.

    Four things are asked to begin with, and each answer changes what is done with a tab:
    which targets are open now, because a browser tells of the tabs already open only when
    asked; which context is its default one, because it names a context on every tab, the
    default one included; and which cookies it holds, for the marker that says the browser is
    Seeress's own. Until the browser has named its default context and shown no marker, a tab
    is held and not attached. A marker ends the capture with CaptureError, and a check that
    could not be made ends it the same way.

    A tab that is attached gets the binding, for the world of the capture alone, and is asked
    for its main frame. The listener is put into the world of that frame, and put in again
    whenever a new document is made there. It is not registered as a script for new
    documents: a browser does not run such a script in a world of its own.
    """

    def __init__(self, emit: Callable[[Event], None]) -> None:
        self._emit = emit
        self._tabs: dict[str, _Tab] = {}
        self._sessions: dict[str, str] = {}  # a session is one tab's
        self._frames: dict[str, str] = {}  # and holds the documents of one frame
        # What is waited for: the id of a command, and what it is about.
        self._pending: dict[int, tuple[str, str]] = {}
        self._last_id = 0
        self._default_context: object = None  # the context the browser names as its own
        self._context_known = False  # once the browser has answered the second command
        self._no_marker = False  # once the browser has shown that it carries none
        self._cookies_command = 0  # the id of the command that asks for the cookies
        # What the capture begins by asking, by the id of the command. A browser that
        # refuses one of these is captured from no more.
        self._opening: dict[int, str] = {}

    @property
    def listened(self) -> frozenset[str]:
        """The tabs that are attached, and so listened to, by their identifiers."""
        return frozenset(target for target, tab in self._tabs.items() if tab.session is not None)

    def opens(self) -> list[dict[str, Any]]:
        """The first commands, in this order: the browser is asked to tell of every target
        from now on, which context is its default one, which targets are open now, and which
        cookies it holds. A browser that refuses the first three is captured from no more:
        without them the capture would be deaf, and go on as though it were not."""
        asking = [
            (
                self._command("Target.setDiscoverTargets", {"discover": True}),
                "to tell of its targets",
            ),
            (self._command("Target.getBrowserContexts", {}), "which context is its default one"),
            (self._command("Target.getTargets", {}), "which targets are open"),
        ]
        self._opening = {command["id"]: asked for command, asked in asking}
        cookies = self._command("Storage.getCookies", {})
        self._cookies_command = cookies["id"]
        return [command for command, _ in asking] + [cookies]

    def hears(self, message: object, when: float) -> list[dict[str, Any]]:
        """One message from the browser, and the commands to send back for it."""
        if not isinstance(message, Mapping):
            return []
        identifier = message.get("id")
        if identifier is not None:
            return self._answered(identifier, message.get("result"), message.get("error"))
        method, params = message.get("method"), message.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return []
        if method == "Target.targetCreated":
            return self._told_of(params.get("targetInfo"))
        if method == "Target.targetInfoChanged":
            return self._moved(params.get("targetInfo"))
        elif method == "Target.targetDestroyed":
            self._gone(params.get("targetId"))
        elif method == "Runtime.executionContextCreated":
            return self._context_made(message.get("sessionId"), params.get("context"))
        elif method == "Runtime.bindingCalled":
            self._reported(
                message.get("sessionId"), params.get("name"), params.get("payload"), when
            )
        return []

    def _command(
        self, method: str, params: dict[str, Any], session: str | None = None
    ) -> dict[str, Any]:
        self._last_id += 1
        command: dict[str, Any] = {"id": self._last_id, "method": method, "params": params}
        if session is not None:
            command["sessionId"] = session
        return command

    def _told_of(self, info: object) -> list[dict[str, Any]]:
        """A target the browser tells of. A tab is held; it is attached when its context is
        known to be the default one, which may be later."""
        if not _is_a_page(info) or info["targetId"] in self._tabs:
            return []
        address = info["url"] if isinstance(info.get("url"), str) else ""
        self._tabs[info["targetId"]] = _Tab(address, info.get("browserContextId"))
        return self._attach(info["targetId"])

    def _attach(self, target: str) -> list[dict[str, Any]]:
        """Attach a held tab, if the browser has been shown to be a person's and not
        Seeress's own, if the tab is of its default context, and if it is not attached or
        being attached."""
        tab = self._tabs[target]
        if not self._ready() or not self._is_own(tab.context):
            return []
        if tab.session is not None or tab.attaching:
            return []
        tab.attaching = True
        command = self._command("Target.attachToTarget", {"targetId": target, "flatten": True})
        self._pending[command["id"]] = ("attach", target)
        return [command]

    def _ready(self) -> bool:
        """True when nothing more is needed before a tab may be attached: the browser has
        named its default context, and has shown that it carries no marker."""
        return self._context_known and self._no_marker

    def _is_own(self, context: object) -> bool:
        """True for the context the browser named as its default one. Every tab that is not
        in a private window is in it, and names it."""
        return self._context_known and context == self._default_context

    def _answered(self, identifier: object, result: object, error: object) -> list[dict[str, Any]]:
        """The answer to a command: to an attach, to the frame tree of a tab, to a world."""
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            return []
        if error is not None or not isinstance(result, Mapping):
            return self._refused(identifier)
        if "defaultBrowserContextId" in result or "browserContextIds" in result:
            return self._context_named(result)
        if "targetInfos" in result:
            return [command for info in result["targetInfos"] for command in self._told_of(info)]
        if "cookies" in result:
            return self._cookies_named(result)
        waiting = self._pending.pop(identifier, None)
        if waiting is None:
            return []
        what, which = waiting
        if what == "attach":
            return self._attached(which, result)
        if what == "frame":
            return self._frame_known(which, result)
        return self._world_made(which, result)

    def _refused(self, identifier: int) -> list[dict[str, Any]]:
        """The browser refused a command, or answered with nothing usable. An attach, a
        frame tree or a world that was refused drops its tab: deaf without it, and so not
        counted as listening. The tab may be told of again, and when the browser next tells
        of it, it is attached again. Before, only an attach did, and a tab whose frame was
        refused stayed deaf and was counted as listening."""
        waiting = self._pending.pop(identifier, None)
        if waiting is not None:
            what, which = waiting
            self._forget(which if what == "attach" else self._sessions.get(which))
            return []
        asked = self._opening.pop(identifier, None)
        if asked is not None:
            # Deaf without it, and connected all the same: ended, and said.
            raise CaptureError(
                f"The browser could not be asked {asked}, so nothing in it is captured."
            )
        if identifier == self._cookies_command:
            # A check that could not be made is no check: nothing is captured here.
            self._tabs.clear()
            raise CaptureError(
                "The browser could not be asked whether it is Seeress's own, so nothing in it"
                " is captured."
            )
        return []

    def _cookies_named(self, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The browser has said which cookies it holds. A cookie with the marker's name, held
        for the marker's site, says that it is Seeress's own: nothing in it is attached,
        and the capture ends. The name alone is not enough: a page may name a cookie of its
        own site as the marker is named, and before the site was looked at too, that ended
        the capture for good. Otherwise the tabs held until now may be attached. The names
        and the sites are looked at, and nothing is kept."""
        if self._no_marker:
            return []  # it is asked of once, at the start
        held = result.get("cookies", [])
        if not isinstance(held, list) or not _cookies_readable(held):
            self._tabs.clear()
            raise CaptureError(
                "The browser's answer about its cookies could not be read, so nothing in it is"
                " captured."
            )
        if any(
            cookie["name"] == MARKER and cookie["domain"].lstrip(".") == MARKER_SITE
            for cookie in held
        ):
            self._tabs.clear()
            raise CaptureError(
                "This browser is Seeress's own: it carries the marker. Nothing in it is captured."
            )
        self._no_marker = True
        return [command for target in list(self._tabs) for command in self._attach(target)]

    def _attached(self, target: str, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """A tab is attached. Its listener needs the binding, which is for its world alone,
        and the frame its documents are in."""
        tab = self._tabs.get(target)
        session = result.get("sessionId")
        if tab is None or not isinstance(session, str):
            # The tab went away while it was being attached.
            self._forget(target)
            return []
        tab.session, tab.attaching = session, False
        self._sessions[session] = target
        asked = self._command("Page.getFrameTree", {}, session)
        self._pending[asked["id"]] = ("frame", session)
        return [
            self._command("Runtime.enable", {}, session),
            self._command(
                "Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD}, session
            ),
            asked,
        ]

    def _frame_known(self, session: str, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The browser has said which frame holds the documents of a tab: the listener goes
        into the one that is there now."""
        frame = result.get("frameTree", {}).get("frame", {}).get("id")
        if isinstance(frame, str):
            self._frames[session] = frame
            return self._listen_in(session)
        return []

    def _listen_in(self, session: str) -> list[dict[str, Any]]:
        """Ask for the world of the capture in the main frame of a tab, by name. The browser
        gives the one it has, or makes it; the binding is in it either way."""
        asked = self._command(
            "Page.createIsolatedWorld",
            {"frameId": self._frames[session], "worldName": WORLD},
            session,
        )
        self._pending[asked["id"]] = ("world", session)
        return [asked]

    def _world_made(self, session: str, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The world is there. The listener is put in it. It installs once in a document, and
        looks for the binding when it has something to report."""
        context = result.get("executionContextId")
        if not isinstance(context, int) or isinstance(context, bool):
            return []
        return [
            self._command(
                "Runtime.evaluate", {"expression": LISTENER, "contextId": context}, session
            )
        ]

    def _context_made(self, session: object, context: object) -> list[dict[str, Any]]:
        """A world came to be in a tab. A browser does not run a script registered for new
        documents in a world of its own, so when the world of a new document is made in the
        main frame, the listener is put in again."""
        if (
            not isinstance(session, str)
            or session not in self._frames
            or not isinstance(context, Mapping)
        ):
            return []
        about = context.get("auxData")
        if (
            isinstance(about, Mapping)
            and about.get("isDefault") is True
            and about.get("frameId") == self._frames[session]
        ):
            return self._listen_in(session)
        return []

    def _context_named(self, result: Mapping[str, object]) -> list[dict[str, Any]]:
        """The browser has said which context is its default one. Without that the capture
        cannot tell a tab from a private window, and nothing in it is captured. The real
        browser names it in its answer, and a test on a real browser holds it to that."""
        if self._context_known:
            return []  # it is asked of once, at the start
        default = result.get("defaultBrowserContextId")
        if not isinstance(default, str) or not default:
            raise CaptureError(
                "The browser did not say which context is its default one, so nothing in it"
                " is captured."
            )
        self._default_context = default
        self._context_known = True
        return [command for target in list(self._tabs) for command in self._attach(target)]

    def _moved(self, info: object) -> list[dict[str, Any]]:
        """What the browser says of a tab has changed. Only its address is kept, for a tab
        that is held. A page that is not held is told of: the browser says so of a tab when
        its address changes, and a tab dropped because its attach, its frame or its world
        was refused is attached again, and so its next document is listened to."""
        if not isinstance(info, Mapping):
            return []
        target = info.get("targetId")
        tab = self._tabs.get(target) if isinstance(target, str) else None
        if tab is None:
            return self._told_of(info)
        if isinstance(info.get("url"), str):
            tab.address = info["url"]
        return []

    def _gone(self, target: object) -> None:
        self._forget(target if isinstance(target, str) else None)

    def _forget(self, target: str | None) -> None:
        """Forget a held tab: it is no longer listened to, and no longer counted as such.
        It may be told of again."""
        tab = self._tabs.pop(target, None) if target is not None else None
        if tab is not None and tab.session is not None:
            del self._sessions[tab.session]
            self._frames.pop(tab.session, None)

    def _reported(self, session: object, name: object, payload: object, when: float) -> None:
        """The listener of a tab reports. What it said becomes an event, if it is one."""
        if not isinstance(session, str) or session not in self._sessions:
            return
        if name != BINDING or not isinstance(payload, str):
            return
        target = self._sessions[session]
        tab = self._tabs.get(target)
        if tab is None:
            return
        event = event_for(payload, tab.address, target, when)
        if event is not None:
            self._emit(event)


# ------------------------------------------------- what is kept, and the status ---

# A file of the record that the capture wrote, by the day it was written on.
HELD = re.compile(r"activity-(\d{8})\.jsonl")


def _held(home: Path) -> list[tuple[date, Path]]:
    """The files of the capture with their days, the oldest day first. Only
    activity-YYYYMMDD.jsonl in the folder of the record is looked at: no other file there is
    the capture's to read, to name or to delete."""
    held: list[tuple[date, Path]] = []
    for path in sorted((home / "watch").glob("activity-*.jsonl")):
        found = HELD.fullmatch(path.name)
        if found is None:
            continue
        try:
            written = date(int(found[1][:4]), int(found[1][4:6]), int(found[1][6:]))
        except ValueError:  # eight digits that are no day: not the capture's to judge
            continue
        held.append((written, path))
    return held


def _before(home: Path, day: date) -> list[Path]:
    """The files of the capture written before a day, the oldest day first."""
    return [path for written, path in _held(home) if written < day]


def retention(home: Path, keep_days: int, *, today: date | None = None) -> list[Path]:
    """Delete the files of the capture whose day is more than keep_days before today.

    A file of the record is named for the day it was written on. One of keep_days days
    before today, to the day, is kept; one of a day before that goes. Returns what was
    deleted, its oldest day first. Only activity-YYYYMMDD.jsonl in the folder of the
    record is looked at: the spike logger's files, and every other file there, are not
    touched. Where there is no record nothing is made, and nothing is done. Today is the
    machine's when none is given.

    Raises OSError when a file cannot be deleted. Nothing is hidden: the capture ends with
    the error.
    """
    oldest_kept = (date.today() if today is None else today) - timedelta(days=keep_days)
    deleted = _before(home, oldest_kept)
    for path in deleted:
        path.unlink()
    return deleted


def review(home: Path | None = None, *, today: date | None = None) -> str:
    """What the record holds, in plain text for a person: what `seeress capture-review`
    prints.

    One line for each day the capture holds, the oldest first: the day, how many events,
    the size of the file, and how many of each kind, the most first. A line that cannot be
    read is counted, and not read.

    Then, while files of the older logger are in the folder of the record, what it holds:
    one line for each day of its lines, with its events by kind. The mine reads both
    records, and this shows both, so that the two can be compared day by day. A file of
    that logger is named for the day it started, and each of its lines is counted on the
    day of its own ts.

    A last line says how many days the settings keep, and from which day files will be
    deleted. It reads, and writes nothing. Raises ConfigError when the settings cannot be
    read.
    """
    home = home if home is not None else config.seeress_home()
    kept = config.load_settings(home).capture.keep_days
    day = date.today() if today is None else today
    lines = [_reviewed(written, path) for written, path in _held(home)] or [
        "The capture holds no day."
    ]
    older: dict[str, Counter[str]] = {}
    for event in activity.read_events(home, patterns=("spike-*.jsonl",)):
        older.setdefault(event.ts[:10], Counter())[event.kind] += 1
    if older:
        lines.append("The older logger holds:")
        lines += [f"{when}   {_counted(counts)}" for when, counts in sorted(older.items())]
    gone = (day - timedelta(days=kept)).isoformat()
    lines.append(
        f"{kept} days are kept, as the settings say. Files written before {gone} will be deleted."
    )
    return "\n".join(lines)


def _reviewed(written: date, path: Path) -> str:
    """One line for one day of the record."""
    counts: Counter[str] = Counter()
    # As bytes: a crash can leave half a character at the end of a file, and read as text
    # the whole file failed to decode, and the review with it.
    with path.open("rb") as stream:
        for line in stream:
            try:
                counts[activity.Event.parse(json.loads(line)).kind] += 1
            except ValueError:  # not text, not JSON, or not an event
                counts["unreadable"] += 1
    events = sum(count for kind, count in counts.items() if kind != "unreadable")
    count = f"{events} event" + ("" if events == 1 else "s")
    return f"{written.isoformat()}   {count}   {_size(path.stat().st_size)}   {_kinds(counts)}"


def _counted(counts: Counter[str]) -> str:
    """How many events, and how many of each kind: what one day of the older logger holds."""
    count = f"{sum(counts.values())} event" + ("" if sum(counts.values()) == 1 else "s")
    return f"{count}   {_kinds(counts)}"


def _kinds(counts: Counter[str]) -> str:
    """How many of each kind, the most first, and by name where as many."""
    return ", ".join(
        f"{kind} {count}"
        for kind, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


def _size(size: int) -> str:
    """The size of a file, as a person reads it."""
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} kB"
    return f"{size / (1024 * 1024):.1f} MB"


def forget(home: Path | None, before: str, *, yes: bool = False) -> str:
    """Delete the record from before a day, or say what would be deleted: what
    `seeress capture-forget` prints.

    before is a day, YYYY-MM-DD. Anything else is refused. Only the files of the capture
    are looked at, as in retention: the spike logger's files are never named, and never
    touched. Without yes nothing is deleted.

    Raises OSError when a file cannot be deleted.
    """
    home = home if home is not None else config.seeress_home()
    day = _day(before)
    gone = _before(home, day)
    if not gone:
        return f"Nothing is held from before {before}."
    how_many = f"{len(gone)} file" + ("" if len(gone) == 1 else "s")
    names = ", ".join(path.name for path in gone)
    if not yes:
        is_are = "is" if len(gone) == 1 else "are"
        it = "it" if len(gone) == 1 else "them"
        return (
            f"{how_many} {is_are} held from before {before}: {names}"
            f"\n--yes is needed to delete {it}."
        )
    for path in gone:
        path.unlink()
    return f"Deleted {how_many} held from before {before}: {names}"


def _day(text: str) -> date:
    """A day as a person writes it, YYYY-MM-DD, and as nothing else."""
    found = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", text)
    if found is not None:
        try:
            return date(int(found[1]), int(found[2]), int(found[3]))
        except ValueError:
            pass  # a day that is no day, like 2026-02-30
    raise CaptureError(f"{text!r} is no day. Write it as YYYY-MM-DD.")


async def hourly(keep: Callable[[], None], every: float, stop: asyncio.Event) -> None:
    """Do what keeps the record short now, and again every so often, until stop is set.

    keep runs on the loop of the capture, between its messages: it holds the capture for as
    long as it takes and no longer. An error of keep's is not caught here. It ends this,
    and the capture with it.
    """
    while not stop.is_set():
        keep()
        # The time came and the stop did not: that is the wait, over.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)


def write_status(
    home: Path,
    state: str,
    reason: str | None,
    *,
    events: int,
    last_event: str | None,
    now: float | None = None,
) -> None:
    """Say of the capture, in one file under home, what it is and what it has written.

    The file is home/watch/capture-status.json, for the user alone, and it replaces what
    was there whole, never rewritten in place: a reader finds one report or the other,
    never none and never half of one. Its folder is made private when it is there and
    open. It holds the process, the state, the reason if one is given, since when the
    state is what it is, how many events were written and when the last was. Since stays
    while the state stays the same in the same process: a report that changes nothing does
    not move it. A new process, or a new state, starts again from now, which is the
    machine's when none is given.

    Raises OSError when the file cannot be written, or its folder cannot be made private.
    """
    when = time.time() if now is None else now
    since = activity.clock(when)
    before = _report(home)
    if before is not None and before["state"] == state and before["pid"] == os.getpid():
        since = before["since"]
    said = {
        "pid": os.getpid(),
        "state": state,
        "reason": reason,
        "since": since,
        "events": events,
        "last_event": last_event,
    }
    watch = activity.private_folder(home / "watch")
    path = watch / STATUS_FILE
    pending = path.with_name(f"{path.name}.new")
    opened = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, activity.PRIVATE_FILE)
    with os.fdopen(opened, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(said) + "\n")
    os.replace(pending, path)


def _report(home: Path) -> dict[str, Any] | None:
    """What the status file says, or None when it is no report.

    A report holds every field of its kind, and its times are times. A capture that is
    down says why. Anything else, a file that cannot be read included, says nothing. The
    shape is contracts/capture-status.schema.json; a contract test keeps this reader and
    that schema in agreement.
    """
    try:
        said = json.loads((home / "watch" / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(said, dict) or set(said) < STATUS_REQUIRED:
        return None
    state, pid, reason = said.get("state"), said.get("pid"), said.get("reason")
    since, events, last = said.get("since"), said.get("events"), said.get("last_event")
    process, count = _whole(pid), _whole(events)
    if not (
        isinstance(state, str)
        and isinstance(since, str)
        and (reason is None or isinstance(reason, str))
        and process is not None  # not a bool: true is no process
        and process > 0
        and count is not None
        and count >= 0
    ):
        return None
    try:
        epoch(since)
        if last is not None:
            epoch(last)
    except (TypeError, ValueError):  # a time that is no time
        return None
    if state == "down" and reason is None:
        return None
    # JSON has one kind of number, and the schema takes a whole one written as a float. It
    # comes back as the integer it is, so that nothing downstream meets a float.
    return {**said, "pid": process, "events": count}


def _whole(value: object) -> int | None:
    """The value as a whole number, or None. JSON has one kind of number, so 4242.0 is 4242.
    True is not a number at all.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _alive(pid: int) -> bool:
    """Whether a process of that number is running, as this system says.

    Where a signal of zero is a question, it asks and does nothing else. A process this
    user may not signal is running all the same. It is not asked on Windows: there a zero
    signal is no question but an order to end the process.
    """
    if sys.platform == "win32":
        raise NotImplementedError("Whether a process is running is not asked on Windows.")
    if pid <= 0:  # a group of processes, or nothing: no process to find
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # running, though not this user's to signal
    return True


def status(home: Path | None = None, *, alive: Callable[[int], bool] = _alive) -> str:
    """What the capture says of itself under home, in a line for a person.

    With no report the capture has never run here. While its process lives, a capture that
    is recording says since when and how much, and one that is down says since when and
    why. Any other report, and any report whose process is gone, says the capture is not
    running, and what it last said.
    """
    said = _report(home if home is not None else config.seeress_home())
    if said is None:
        return "Capture has never run here."
    state, since, reason = said["state"], said["since"], said["reason"]
    pid = said["pid"]
    if alive(pid):
        if state == "connected":
            last = said["last_event"]
            count = f"{said['events']} event" + ("" if said["events"] == 1 else "s")
            if last is not None:
                count += f", the last at {last}"
            return f"Capture is recording since {since} (pid {pid}): {count}."
        if state == "down":
            return (
                f"Capture is down since {since}: {reason}."
                " Is remote debugging on in Chrome? chrome://inspect/#remote-debugging"
            )
    last = f"It last reported {state} at {since}"
    if reason is not None:
        last += f": {reason}"
    return f"Capture is not running. {last}."


class Status:
    """What the capture says of itself, as it goes.

    state() is written at once: a change of the capture is news now. An event that passes
    the gate only counts. The count is written when its turn has come, every so often, and
    whenever the state is written: a file for every click would be a second record.
    """

    def __init__(self, home: Path, *, every: float = STATUS_EVERY) -> None:
        self._home = home
        self._every = every
        self._state: str | None = None
        self._reason: str | None = None
        self._flushed = time.time()
        self.events = 0
        self.last_event: str | None = None

    def state(self, state: str, reason: str | None = None) -> None:
        """The capture is in this state, for this reason if one is given. Written at once,
        with what has been counted so far."""
        self._state, self._reason = state, reason
        self.flush()

    def written(self, event: Event) -> None:
        """One event has passed the gate and been written: counted, and the count written
        when its turn has come."""
        self.events += 1
        self.last_event = event.ts
        if time.time() - self._flushed >= self._every:
            self.flush()

    def flush(self) -> None:
        """Write the count with the state the capture is in. Nothing before a state."""
        if self._state is not None:
            write_status(
                self._home,
                self._state,
                self._reason,
                events=self.events,
                last_event=self.last_event,
            )
        self._flushed = time.time()


def _cookies_readable(held: list[Any]) -> bool:
    """Every cookie is an object with a name and a site, both text. Only so can the marker be
    told from the rest: a cookie that cannot be read may be it."""
    return all(
        isinstance(cookie, Mapping)
        and isinstance(cookie.get("name"), str)
        and isinstance(cookie.get("domain"), str)
        for cookie in held
    )


def _reason(error: BaseException) -> str:
    """Why the capture ended: the name of the error, and its words."""
    return f"{type(error).__name__}: {error}"


def _ended(status: Status | None, state: str, reason: str) -> None:
    """The capture ended, and why. A status that cannot be written hides nothing: the
    error that ended the capture is the error raised all the same."""
    if status is not None:
        with contextlib.suppress(OSError):
            status.state(state, reason)


# ------------------------------------------------------------------------- the gate ---


# No tab may put more than this many events in the record in one window of seconds. A page
# that fires a thousand clicks a minute is not a person at work, and a person reads no more.
CAP = 200
WINDOW = 60.0


class Gate:
    """Between the browser and the record. What passes is written; what is refused is counted
    and not written.

    An event passes in this order, and the first rule that refuses it wins: the schedule,
    then the cap for its tab. The address of the tab has been judged before, when the event
    was made. What passes is written with write_event, for the user alone, and told to
    the status when one is given.
    """

    def __init__(
        self, home: Path, *, schedule: Schedule = DEFAULT_SCHEDULE, status: Status | None = None
    ) -> None:
        self._home = home
        self._schedule = schedule
        self._status = status
        # A tab: when its window opened, and how many of its events passed in it.
        self._windows: dict[str, tuple[float, int]] = {}
        self.written = 0
        self.dropped = {"schedule": 0, "cap": 0}

    def __call__(self, event: Event) -> None:
        """One event from a tab. Written, or counted among the dropped."""
        when = epoch(event.ts)
        if not due(when, self._schedule):
            self.dropped["schedule"] += 1
            return
        tab = event.target or ""
        opened, passed = self._windows.get(tab, (when, 0))
        if when - opened >= WINDOW:  # the first event of a tab after a quiet minute
            opened, passed = when, 0
        self._windows[tab] = (opened, passed + 1)
        if passed >= CAP:
            self.dropped["cap"] += 1
            return
        write_event(self._home, event, now=when)
        self.written += 1
        if self._status is not None:
            self._status.written(event)


async def _left(task: asyncio.Future[Any] | None) -> None:
    """Leave a task the capture waited on. One that has ended has its end read, and one that
    is still going is cancelled and waited for, so that nothing of it is left behind to be
    destroyed unread."""
    if task is None:
        return
    if not task.done():
        task.cancel()
        await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


# ----------------------------------------------------------------- the connection ---


class _Socket(Protocol):
    """What the capture needs of its connection to the browser."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> Data: ...

    async def close(self) -> None: ...


class Nowhere:
    """A connection to nowhere, for a capture stopped before the browser let it in. What
    is sent to it is dropped. The loop reads nothing from it: it sees the stop first."""

    async def send(self, message: str) -> None:
        return None

    async def recv(self) -> Data:
        # The loop reads nothing from here: it sees the stop first. If it ever did, that
        # would be a fault, and it is said to be one.
        raise RuntimeError("a connection to nowhere has nothing to say")

    async def close(self) -> None:
        return None


class Connected:
    """The capture's connection to the browser, as a context.

    Entering waits for the browser to let the capture in, or for the stop, whichever comes
    first. Stopped first, the capture goes on with a connection to nowhere: the loop sees
    the stop first, and the attempt is given up. It is not waited for and not an error.
    Before, a stop asked for while the browser was being reached waited for the connection
    to succeed or to give up, ten seconds by default, and came back as a timeout.
    """

    def __init__(self, address: str, ended: asyncio.Event) -> None:
        # No bound on the size of one message. The capture is told whatever a page says to
        # its console, and under a bound one large line from any page ended the connection,
        # and the capture in every tab with it. No proxy: one must never see this machine.
        self._connection = connect(address, max_size=None, proxy=None)
        self._ended = ended
        self._socket: _Socket = Nowhere()

    async def __aenter__(self) -> _Socket:
        entering = asyncio.ensure_future(self._connection.__aenter__())
        stopping = asyncio.create_task(self._ended.wait())
        done, _ = await asyncio.wait({entering, stopping}, return_when=asyncio.FIRST_COMPLETED)
        await _left(stopping)
        if entering not in done:
            await _left(entering)
            return self._socket
        self._socket = entering.result()  # raises when the browser could not be reached
        return self._socket

    async def __aexit__(self, *exc: object) -> None:
        await self._socket.close()
        return None


async def watch(
    address: str,
    emit: Callable[[Event], None],
    *,
    stop: asyncio.Event | None = None,
    keep: Callable[[], None] | None = None,
    every: float = HOURLY,
    status: Status | None = None,
) -> None:
    """Listen to every tab of the browser at address until stop is set.

    Each event the listeners report is given to emit. One connection, for as long as it
    holds: when it ends, because the browser went away or refused it, watch ends with the
    error it got, and trying again is left to the caller. Only tabs of the browser's default
    context are listened to, so a private window is never captured. Nothing here is written:
    emit does the writing, and what it is given holds a site and a path, and no query.

    keep is done once the connection is made, and again every so often: what keeps the
    record short. A capture that cannot connect keeps nothing. An error of keep's ends
    the capture with it.

    status is told "connected" when the browser has answered its first command, "down" and
    the reason when the connection ends with an error, "failed" and the reason on a fault
    of the capture's own, and "stopped" when it ends as it was asked to. A status that
    cannot be written hides none of these: the error that ended the capture is raised.
    """
    sessions = Sessions(emit)
    ended = asyncio.Event() if stop is None else stop
    keeping: asyncio.Task[None] | None = None  # once the browser has let the capture in
    answered = False
    try:
        async with Connected(address, ended) as socket:
            if keep is not None:
                keeping = asyncio.create_task(hourly(keep, every, ended))
            for command in sessions.opens():
                await socket.send(json.dumps(command))
            stopping = asyncio.create_task(ended.wait())
            receiving: asyncio.Future[Any] | None = None
            try:
                while not ended.is_set():
                    receiving = asyncio.ensure_future(socket.recv())
                    waiting = (
                        {receiving, stopping}
                        if keeping is None
                        else {
                            receiving,
                            stopping,
                            keeping,
                        }
                    )
                    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                    if keeping is not None and keeping in done:
                        await keeping  # what keeps the record failed, if it did: so does this
                    if receiving not in done:
                        break  # stopped, as asked
                    message = json.loads(receiving.result())
                    for command in sessions.hears(message, time.time()):
                        await socket.send(json.dumps(command))
                    named = isinstance(message, Mapping) and "id" in message
                    if status is not None and not answered and named:
                        answered = True  # the browser has answered its first command
                        status.state("connected")
            finally:
                # However this ended, by a stop, by an error or by a fault, nothing the
                # loop waited on is left behind it.
                await _left(receiving)
                await _left(stopping)
    except (OSError, WebSocketException, CaptureError) as error:
        _ended(status, "down", _reason(error))
        raise
    except BaseException as error:  # a fault of the capture's own: said, and not swallowed
        _ended(status, "failed", _reason(error))
        raise
    else:
        if status is not None:
            status.state("stopped")
    finally:
        ended.set()
        if keeping is not None:
            await keeping


class Recorder:
    """A capture for a caller that has no event loop of its own, such as a test or the logger.

    start() runs watch on a thread of its own and returns at once. stop() ends it, and
    returns only when the thread has ended. It may be called before start(), and more than
    once. When watch ends with an error, the thread ends and the error is kept in `error`,
    where the caller finds it: nothing is printed, and nothing is raised on a thread. A
    fault of the capture's own is kept there the same way.

    keep, every and status are given to watch.
    """

    def __init__(
        self,
        address: str,
        emit: Callable[[Event], None],
        *,
        keep: Callable[[], None] | None = None,
        every: float = HOURLY,
        status: Status | None = None,
    ) -> None:
        self._address = address
        self._emit = emit
        self._keep = keep
        self._every = every
        self._status = status
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        """True while the thread of the capture is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> BaseException | None:
        """The error that ended the capture, or None while none has."""
        return self._error

    def start(self) -> None:
        """Run the capture. An error kept from a run before is not shown any more: a run
        that goes on has none to show, until it ends with one."""
        if self.running:
            return
        self._error = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._listen, name="seeress-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._ready.wait()
        if self._loop is not None and self._stop is not None:
            with contextlib.suppress(RuntimeError):  # the loop closed between two looks
                self._loop.call_soon_threadsafe(self._stop.set)
        thread.join()

    def _listen(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop, self._stop = loop, asyncio.Event()
        self._ready.set()
        try:
            loop.run_until_complete(
                watch(
                    self._address,
                    self._emit,
                    stop=self._stop,
                    keep=self._keep,
                    every=self._every,
                    status=self._status,
                )
            )
        except Exception as error:
            # The browser could not be reached, or it is Seeress's own, or it could not
            # say; or the record could not be written or kept; or a fault of the capture's
            # own. Kept for the caller, and nothing raised on a thread.
            self._error = error
        finally:
            loop.close()


# ------------------------------------------------------------------- the logger ---

# How long the logger waits before it tries the browser again, when the connection ended
# or the browser publishes no address.
RETRY_SECONDS = 5.0

# How often the logger looks whether the capture is still running.
POLL_SECONDS = 0.1

Handler = int | Callable[[int, FrameType | None], Any] | None

logger = logging.getLogger("seeress.capture")

# A signal that asks the process to end: closing the terminal, or unloading a launch agent.
ENDING_SIGNALS = tuple(
    number
    for number in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None))
    if number is not None
)


def run(
    address: str | None = None,
    *,
    home: Path | None = None,
    seconds: float | None = None,
    stop: threading.Event | None = None,
) -> int:
    """The capture as a logger: it connects, records, keeps the record short, and tries
    again whenever the connection ends, until it is stopped or seconds have passed.

    address is the browser's DevTools address. Left out, it is read where Chrome publishes
    it for this user, as the spike logger read it: when nothing is published, because Chrome
    is not running or remote debugging is off, nothing is connected to. The schedule and
    the days kept come from the settings under home, which is the Seeress home when none is
    given.

    Returns the exit status: 0 when the capture was stopped as it ran, and 1 when it was
    down, or had failed, when it stopped. Says on stderr what it wrote and what the status
    holds. Nothing is written to stdout.

    Raises ConfigError when the settings cannot be read.
    """
    home = home if home is not None else config.seeress_home()
    settings = config.load_settings(home)
    told = Status(home)
    gate = Gate(home, schedule=settings.capture.schedule, status=told)
    stop = stop if stop is not None else threading.Event()
    began = time.monotonic()
    last_error: BaseException | None = None
    last_reason: str | None = None

    def ended() -> bool:
        return stop.is_set() or (seconds is not None and time.monotonic() - began >= seconds)

    def keep() -> None:
        retention(home, settings.capture.keep_days)

    while not ended():
        where = address if address is not None else config.browser_env("personal").get("BU_CDP_WS")
        if where is None:
            told.state("down", "Chrome publishes no address for this user")
        else:
            recorder = Recorder(where, gate, keep=keep, status=told)
            recorder.start()
            while recorder.running and not ended():
                stop.wait(POLL_SECONDS)
            recorder.stop()
            error = recorder.error
            if error is not None:
                # The capture ended by itself, and the logger goes on. The status says why
                # when it can be written. When it cannot, on a full disk, these words are the
                # only place the reason reaches: said once, and again when it changes.
                last_error = error
                reason = _reason(error)
                if reason != last_reason:
                    logger.error("the capture ended: %s", reason)
                    last_reason = reason
        if ended():
            break
        # The browser is away, or went away, or refused: the status says which. Wait, then
        # try again, for as long as the logger runs.
        stop.wait(RETRY_SECONDS)

    report = _report(home)
    dropped = gate.dropped
    if last_error is not None:
        logger.error("the last capture ended with: %s", _reason(last_error))
    logger.info(
        "%d events written; %d outside the schedule; %d over the cap",
        gate.written,
        dropped["schedule"],
        dropped["cap"],
    )
    logger.info("%s", status(home))
    return 0 if report is not None and report["state"] == "stopped" else 1


def main(argv: Sequence[str] | None = None) -> int:
    """What `python -m seeress.capture` runs. Returns the exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m seeress.capture",
        description="The capture as a logger: it records what a person does in the tabs of a"
        " browser, for this machine alone, until it is asked to stop.",
    )
    parser.add_argument(
        "address",
        nargs="?",
        default=None,
        help="the browser's DevTools address, as ws://... Left out, it is read where Chrome"
        " publishes it for this user",
    )
    parser.add_argument(
        "--home", type=Path, default=None, help="the Seeress home. Default: SEERESS_HOME"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after so many seconds, for a trial. Default: until asked to stop",
    )
    args = parser.parse_args(argv)
    log.to_stderr()

    stop = threading.Event()
    before: dict[int, Handler] = {}
    try:
        for number in ENDING_SIGNALS:
            before[number] = signal.signal(number, lambda *_: stop.set())
    except ValueError:  # not in the main thread, where signals cannot be caught
        pass
    try:
        try:
            return run(args.address, home=args.home, seconds=args.seconds, stop=stop)
        except (SeeressError, OSError) as error:
            logger.error("the capture cannot run: %s", error)
            return 1
    finally:
        # The handlers there were are put back.
        for number, handler in before.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
