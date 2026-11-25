"""Driving a tab of a browser Seeress started, from a connection of the test's own.

The capture listens on a connection of its own. A test acts in the page through this one,
and sees what the capture made of it. There is no mouse and no keyboard here, so a test
calls the browser's own command to type, and evaluates in the page to click.
"""

import json
import time
from typing import Any

from websockets.sync.client import connect

from seeress import capture

MOST_AT_ONCE = 2**22


class Browser:
    """A connection of the test's own to the browser at address."""

    def __init__(self, address: str) -> None:
        # A proxy must never see loopback.
        self._socket = connect(
            address, open_timeout=5, close_timeout=1, max_size=MOST_AT_ONCE, proxy=None
        )
        self._last_id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one command to the browser, and return the result of its answer."""
        self._last_id += 1
        self._socket.send(
            json.dumps({"id": self._last_id, "method": method, "params": params or {}})
        )
        return _answer(self._socket, self._last_id, method)

    def tab(self, url: str, *, context: str | None = None) -> "Tab":
        """Open a tab on the page, in the default context of the browser or in another one."""
        params: dict[str, Any] = {"url": url}
        if context is not None:
            params["browserContextId"] = context
        target = self.call("Target.createTarget", params)
        return Tab(self, target["targetId"])

    def private_context(self) -> str:
        """A second context of the browser, as a private window has. Returns its identifier."""
        return str(self.call("Target.createBrowserContext")["browserContextId"])

    def close(self) -> None:
        self._socket.close()


class Tab:
    """A tab of the browser, driven through the connection of its test."""

    def __init__(self, browser: Browser, target_id: str) -> None:
        self.browser = browser
        self.id = target_id
        session = browser.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.session: str = session["sessionId"]

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one command in the tab, and return the result of its answer."""
        self.browser._last_id += 1
        message: dict[str, Any] = {
            "id": self.browser._last_id,
            "sessionId": self.session,
            "method": method,
            "params": params or {},
        }
        self.browser._socket.send(json.dumps(message))
        return _answer(self.browser._socket, self.browser._last_id, method)

    def run(self, expression: str) -> Any:
        """Evaluate an expression in the page, in the world of the page's own scripts, and
        return what it comes to."""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise AssertionError(
                f"the page could not run {expression!r}: {result['exceptionDetails']}"
            )
        return result["result"].get("value")

    def go(self, url: str) -> None:
        """Navigate the tab, and wait until the page has loaded."""
        self.call("Page.navigate", {"url": url})
        self.wait_loaded()

    def wait_loaded(self, timeout: float = 10.0) -> None:
        until(lambda: self.run("document.readyState") == "complete", "a loaded page", timeout)

    def listening(self) -> bool:
        """Whether the capture listens in the page, asked in the world its listener runs in.
        A page's own scripts cannot see that world. A test can, as the capture's equal."""
        frame = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        world = self.call(
            "Page.createIsolatedWorld", {"frameId": frame, "worldName": capture.WORLD}
        )
        found = self.call(
            "Runtime.evaluate",
            {
                "expression": "typeof window.__seeressListening",
                "contextId": world["executionContextId"],
                "returnByValue": True,
            },
        )
        return bool(found["result"].get("value") == "boolean")

    def front(self) -> None:
        """Bring the tab to the front, where what is typed goes."""
        self.call("Page.bringToFront")

    def middle(self, selector: str) -> tuple[float, float]:
        """Where the middle of the first element a selector names is, in the page."""
        element = f"document.querySelector({json.dumps(selector)})"
        x, y = self.run(
            f"(() => {{ const r = {element}.getBoundingClientRect();"
            " return [r.left + r.width / 2, r.top + r.height / 2]; })()"
        )
        return (x, y)

    def click(self, selector: str) -> None:
        """Click the first element a selector names, in the middle of it, as a mouse does.
        The click is the browser's own, which is what a person's is to the page."""
        element = self.run(f"document.querySelector({json.dumps(selector)}) !== null")
        assert element, f"no element for {selector}"
        x, y = self.middle(selector)
        self.click_at(x, y)

    def click_at(self, x: float, y: float) -> None:
        """Click at a point of the page, as a mouse does."""
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def click_by_script(self, selector: str) -> None:
        """Click the first element a selector names from a script of the page. To the page
        it is no person's click."""
        self.run(f"document.querySelector({json.dumps(selector)}).click()")

    def pick(self, selector: str, letter: str) -> None:
        """Choose in the select a selector names by the first letter of an option, as a
        keyboard does."""
        self.front()
        self.run(f"document.querySelector({json.dumps(selector)}).focus()")
        code = ord(letter.upper())
        for kind, text in (("keyDown", letter), ("keyUp", "")):
            self.call(
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": letter,
                    "code": f"Key{letter.upper()}",
                    "text": text,
                    "unmodifiedText": text,
                    "windowsVirtualKeyCode": code,
                    "nativeVirtualKeyCode": code,
                },
            )

    def type_text(self, text: str, selector: str) -> None:
        """Type into the field a selector names, as a keyboard does."""
        self.front()
        self.run(f"document.querySelector({json.dumps(selector)}).focus()")
        self.call("Input.insertText", {"text": text})

    def leave(self, selector: str) -> None:
        """Take the focus from the field a selector names."""
        self.run(f"document.querySelector({json.dumps(selector)}).blur()")

    def close(self) -> None:
        self.browser.call("Target.closeTarget", {"targetId": self.id})


def _answer(socket: Any, identifier: int, method: str) -> Any:
    """The answer to the command with this id, among the messages the browser sends."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        message = json.loads(socket.recv(timeout=deadline - time.monotonic()))
        if message.get("id") != identifier:
            continue
        if "error" in message:
            raise AssertionError(f"{method} was refused: {message['error']}")
        return message.get("result", {})
    raise AssertionError(f"no answer to {method} in 10 s")


def listened(tab: Tab, timeout: float = 10.0) -> None:
    """Wait until the capture listens in the tab: the page is loaded, and the listener is
    in it. Until then a click or a keystroke is the page's own alone, and a test that sends
    one is waiting for an event that cannot come."""
    tab.wait_loaded(timeout)
    until(tab.listening, "the capture to listen in the tab", timeout)


def until(condition: Any, what: str, timeout: float) -> Any:
    """Wait for condition to come back with something, and say what it did not get."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = condition()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError(f"waited {timeout:g} s, and did not get {what}")
