"""The capture: a listener in every page of a browser, and what it reports.

The capture is a second client of the browser's DevTools connection. It attaches to every
tab of the browser's default context, and so never to a private window, and in each it runs
a listener, in a world of the page that the page's own scripts cannot reach. The listener
reports a click, an input, a submit and a select. It reads an element's tag, four attributes
that name it and two that say what kind of a field it is, and nothing else: never what a
field holds, and never the text of a page.

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

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import activity
from .activity import DEFAULT_SCHEDULE, Event, Schedule, due, epoch, write_event
from .errors import CaptureError

# The cookie by which Seeress knows a browser for its own. A browser that carries it is one
# Seeress started for a door, and nothing in it is a person's work. The name is
# `MARKER_KEY` in seeress/bodies.py, and a test holds the two together.
MARKER = "seeress_body"

# What the listener reports through, and the world of the page it runs in. A world of its own
# keeps the binding and the listener out of the reach of the page's own scripts: a page can
# neither report in the listener's place nor change what it reads.
BINDING = "seeressEvent"
WORLD = "seeress"

# The most the browser may say in one message, as the spike logger allows.
MOST_AT_ONCE = 2**22

# What the capture says of itself, for capture-status to read.
STATUS_FILE = "capture-status.json"

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

    // One attribute that names an element, as tag[name="value"], or null when the element
    // has none. An id with a digit in it is not stable, so it does not name an element.
    const attribute = (element, name, text) => {
      if (!text) return null;
      if (name === "id" && /[0-9]/.test(text)) return null;
      return element.localName + "[" + name + '="' + stable(text) + '"]';
    };
    const naming = (element) =>
      attribute(element, "data-testid", element.getAttribute("data-testid")) ||
      attribute(element, "id", element.getAttribute("id")) ||
      attribute(element, "name", element.getAttribute("name")) ||
      attribute(element, "aria-label", element.getAttribute("aria-label"));

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
        tag: element.localName,
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

    // Each listener is wrapped, so that nothing of this listener's may reach the page.
    const on = (type, work) => {
      document.addEventListener(type, (event) => {
        try {
          work(event);
        } catch (error) {
          // Nothing of this listener's may reach the page.
        }
      }, true);
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
                "path": parts.path or "/",
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

    @property
    def listened(self) -> frozenset[str]:
        """The tabs that are attached, and so listened to, by their identifiers."""
        return frozenset(target for target, tab in self._tabs.items() if tab.session is not None)

    def opens(self) -> list[dict[str, Any]]:
        """The first commands, in this order: the browser is asked to tell of every target
        from now on, which context is its default one, which targets are open now, and which
        cookies it holds."""
        opening = [
            self._command("Target.setDiscoverTargets", {"discover": True}),
            self._command("Target.getBrowserContexts", {}),
            self._command("Target.getTargets", {}),
        ]
        cookies = self._command("Storage.getCookies", {})
        self._cookies_command = cookies["id"]
        return [*opening, cookies]

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
            self._moved(params.get("targetInfo"))
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
        """True for the context the browser names as its default one. A browser that names
        none has, for its own, a tab on which it names no context."""
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
        """The browser refused a command, or answered with nothing usable. An attach that was
        refused drops its tab. Anything else is left: the next document in the tab asks
        again."""
        waiting = self._pending.pop(identifier, None)
        if waiting is not None and waiting[0] == "attach":
            self._tabs.pop(waiting[1], None)
            return []
        if identifier == self._cookies_command:
            # A check that could not be made is no check: nothing is captured here.
            self._tabs.clear()
            raise CaptureError(
                "The browser could not be asked whether it is Seeress's own, so nothing in it"
                " is captured."
            )
        return []

    def _cookies_named(self, result: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The browser has said which cookies it holds. A cookie with the marker's name says
        that it is Seeress's own: nothing in it is attached, and the capture ends. Otherwise
        the tabs held until now may be attached. The names are looked at and nothing is kept."""
        if self._no_marker:
            return []  # it is asked of once, at the start
        held = result.get("cookies", [])
        if not isinstance(held, list):
            self._tabs.clear()
            raise CaptureError(
                "The browser's answer about its cookies could not be read, so nothing in it is"
                " captured."
            )
        if any(isinstance(cookie, Mapping) and cookie.get("name") == MARKER for cookie in held):
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
            self._tabs.pop(target, None)
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
        """The browser has said which context is its default one: the default answer names
        it. A browser that names none is taken to have no context id of its own, which is
        what it writes on a tab of that context."""
        if self._context_known:
            return []  # it is asked of once, at the start
        self._default_context = result.get("defaultBrowserContextId")
        self._context_known = True
        return [command for target in list(self._tabs) for command in self._attach(target)]

    def _moved(self, info: object) -> None:
        """What the browser says of a tab has changed. Only its address is kept."""
        if not isinstance(info, Mapping):
            return
        target = info.get("targetId")
        tab = self._tabs.get(target) if isinstance(target, str) else None
        if tab is not None and isinstance(info.get("url"), str):
            tab.address = info["url"]

    def _gone(self, target: object) -> None:
        tab = self._tabs.pop(target, None) if isinstance(target, str) else None
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
    deleted: list[Path] = []
    for path in sorted((home / "watch").glob("activity-*.jsonl")):
        found = HELD.fullmatch(path.name)
        if found is None:
            continue
        try:
            written = date(int(found[1][:4]), int(found[1][4:6]), int(found[1][6:]))
        except ValueError:  # eight digits that are no day: not the capture's to judge
            continue
        if written < oldest_kept:
            path.unlink()
            deleted.append(path)
    return deleted


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
    was there whole: a reader finds one report or the other, never half of one. It holds
    the process, the state, the reason if one is given, since when the state is what it is,
    how many events were written and when the last was. Since stays while the state stays
    the same in the same process: a report that changes nothing does not move it. A new
    process, or a new state, starts again from now, which is the machine's when none is
    given.

    Raises OSError when the file cannot be written.
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
    watch = home / "watch"
    watch.mkdir(mode=activity.PRIVATE, parents=True, exist_ok=True)
    path = watch / STATUS_FILE
    pending = path.with_name(f"{path.name}.new")
    opened = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, activity.PRIVATE_FILE)
    with os.fdopen(opened, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(said) + "\n")
    os.replace(pending, path)


def _report(home: Path) -> dict[str, Any] | None:
    """What the status file says, or None when it is no report.

    A report holds every field of its kind, and its times are times. A capture that is
    down says why. Anything else, a file that cannot be read included, says nothing.
    """
    try:
        said = json.loads((home / "watch" / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(said, dict):
        return None
    state, pid, reason = said.get("state"), said.get("pid"), said.get("reason")
    since, events, last = said.get("since"), said.get("events"), said.get("last_event")
    if not (
        isinstance(state, str)
        and isinstance(since, str)
        and (reason is None or isinstance(reason, str))
        and type(pid) is int  # not a bool: true is no process
        and pid > 0
        and type(events) is int
        and events >= 0
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
    return said


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


def status(home: Path, *, alive: Callable[[int], bool] = _alive) -> str:
    """What the capture says of itself under home, in a line for a person.

    With no report the capture has never run here. While its process lives, a capture that
    is recording says since when and how much, and one that is down says since when and
    why. Any other report, and any report whose process is gone, says the capture is not
    running, and what it last said.
    """
    said = _report(home)
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


# ----------------------------------------------------------------- the connection ---


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
        async with connect(address, max_size=MOST_AT_ONCE, proxy=None) as socket:
            if keep is not None:
                keeping = asyncio.create_task(hourly(keep, every, ended))
            for command in sessions.opens():
                await socket.send(json.dumps(command))
            stopping = asyncio.create_task(ended.wait())
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
                        receiving.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await receiving
                        break  # stopped, as asked
                    message = json.loads(receiving.result())
                    for command in sessions.hears(message, time.time()):
                        await socket.send(json.dumps(command))
                    named = isinstance(message, Mapping) and "id" in message
                    if status is not None and not answered and named:
                        answered = True  # the browser has answered its first command
                        status.state("connected")
            finally:
                stopping.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stopping
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
        if self.running:
            return
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
