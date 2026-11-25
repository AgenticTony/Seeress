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

Nothing here looks for a browser. The capture takes an address, and whose browser is behind
it is for the caller to know. This module starts nothing on anyone's browser.
"""

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import activity
from .activity import Event

# What the listener reports through, and the world of the page it runs in. A world of its own
# keeps the binding and the listener out of the reach of the page's own scripts: a page can
# neither report in the listener's place nor change what it reads.
BINDING = "seeressEvent"
WORLD = "seeress"

# The most the browser may say in one message, as the spike logger allows.
MOST_AT_ONCE = 2**22

LISTENER = r"""\
(() => {
  "use strict";
  try {
    // Only what a person does in the page itself is theirs. A frame inside the page, an
    // advertisement for one, is not listened to.
    if (window.top !== window) return;
    // Once in a document. The flag lives in this world alone, where the page cannot see it.
    if (window.__seeressListening) return;
    const send = window.seeressEvent;
    if (typeof send !== "function") return;
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

    const report = (type, element, field) => {
      const said = {
        type: type,
        tag: element.localName,
        fingerprint: (naming(element) || place(element)).slice(0, 200),
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
    """What the capture holds for one tab: the address the browser last gave for it, and its
    session once it is attached."""

    address: str
    session: str | None = None


def _is_own_page(info: object) -> TypeGuard[Mapping[str, Any]]:
    """True for what the browser says of a tab of its default context, and of nothing else."""
    return (
        isinstance(info, Mapping)
        and info.get("type") == "page"
        and info.get("browserContextId") is None
        and isinstance(info.get("targetId"), str)
    )


class Sessions:
    """What one capture holds about the tabs of a browser, and how it answers the browser.

    The browser speaks in messages: events, which say what happened, and answers to the
    commands the capture sent. hears takes one message and returns the commands to send back.
    The connection is not here, so all of this can be tried without a browser.
    """

    def __init__(self, emit: Callable[[Event], None]) -> None:
        self._emit = emit
        self._tabs: dict[str, _Tab] = {}
        self._sessions: dict[str, str] = {}  # a session is one tab's
        self._attaching: dict[int, str] = {}  # the id of a command, and the tab it attaches
        self._last_id = 0

    @property
    def listened(self) -> frozenset[str]:
        """The tabs that are attached, and so listened to, by their identifiers."""
        return frozenset(target for target, tab in self._tabs.items() if tab.session is not None)

    def opens(self) -> list[dict[str, Any]]:
        """The first command: the browser is asked to tell of every target from now on."""
        return [self._command("Target.setDiscoverTargets", {"discover": True})]

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
        """A target the browser tells of: a tab of the default context is attached."""
        if not _is_own_page(info) or info["targetId"] in self._tabs:
            return []
        address = info["url"] if isinstance(info.get("url"), str) else ""
        self._tabs[info["targetId"]] = _Tab(address)
        command = self._command(
            "Target.attachToTarget", {"targetId": info["targetId"], "flatten": True}
        )
        self._attaching[command["id"]] = info["targetId"]
        return [command]

    def _answered(self, identifier: object, result: object, error: object) -> list[dict[str, Any]]:
        """The answer to a command: an attach that was granted is given its listener."""
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            return []
        target = self._attaching.pop(identifier, None)
        if target is None:
            return []
        session = result.get("sessionId") if error is None and isinstance(result, Mapping) else None
        tab = self._tabs.get(target)
        if tab is None or not isinstance(session, str):
            # The tab went away while it was being attached, or the browser refused.
            self._tabs.pop(target, None)
            return []
        tab.session = session
        self._sessions[session] = target
        return [
            self._command("Runtime.enable", {}, session),
            self._command(
                "Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD}, session
            ),
            self._command(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": LISTENER, "worldName": WORLD, "runImmediately": True},
                session,
            ),
        ]

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


# ----------------------------------------------------------------- the connection ---


async def watch(
    address: str, emit: Callable[[Event], None], *, stop: asyncio.Event | None = None
) -> None:
    """Listen to every tab of the browser at address until stop is set.

    Each event the listeners report is given to emit. One connection, for as long as it
    holds: when it ends, because the browser went away or refused it, watch ends with the
    error it got, and trying again is left to the caller. Only tabs of the browser's default
    context are listened to, so a private window is never captured. Nothing here is written:
    emit does the writing, and what it is given holds a site and a path, and no query.
    """
    sessions = Sessions(emit)
    ended = asyncio.Event() if stop is None else stop
    async with connect(address, max_size=MOST_AT_ONCE, proxy=None) as socket:
        for command in sessions.opens():
            await socket.send(json.dumps(command))
        stopping = asyncio.create_task(ended.wait())
        try:
            while not ended.is_set():
                receiving = asyncio.ensure_future(socket.recv())
                done, _ = await asyncio.wait(
                    {receiving, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiving not in done:
                    receiving.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receiving
                    return
                for command in sessions.hears(json.loads(receiving.result()), time.time()):
                    await socket.send(json.dumps(command))
        finally:
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping


class Recorder:
    """A capture for a caller that has no event loop of its own, such as a test or the logger.

    start() runs watch on a thread of its own and returns at once. stop() ends it, and
    returns only when the thread has ended. It may be called before start(), and more than
    once. When watch ends with an error, the thread ends and the error is kept in `error`,
    where the caller finds it: nothing is printed, and nothing is raised on a thread.
    """

    def __init__(self, address: str, emit: Callable[[Event], None]) -> None:
        self._address = address
        self._emit = emit
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
            loop.run_until_complete(watch(self._address, self._emit, stop=self._stop))
        except (OSError, WebSocketException) as error:  # the browser could not be reached
            self._error = error
        finally:
            loop.close()
