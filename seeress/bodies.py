"""Browser bodies: which browser a door may drive, and how Seeress proves it.

A door is only as private as the browser behind it. The personal door drives the user's
own Chrome. The client door drives a Chromium on a profile Seeress owns, and nothing else.

The browser tool's daemon reports only its kind, and the library's own Chromium and the
user's Chrome report the same kind. So identity is proved rather than inferred. Seeress
sets a random marker cookie for a reserved origin in a browser it owns, and reads it back
through the browser tool before any model request.

The origin is http://seeress.invalid. The .invalid top-level domain can never be
delegated (RFC 6761), so the cookie is never sent to anyone. No other site can read it:
a cookie belongs to one host. It was checked against the real browser tool that the cookie
jar answers where a storage command does not exist.
"""

import contextlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import config, env
from .errors import BrowserError, ConfigError, IsolationError
from .runner import run_browser_command, run_browser_tool

MARKER_URL = config.MARKER_URL
MARKER_KEY = config.MARKER_KEY
# Asked for as 2100-01-01. The browser keeps a cookie for 400 days at most (found on a real
# browser on 2026-11-25), which outlasts many a restart of it.
MARKER_EXPIRES = 4102444800
PROBE_TAG = "SEERESS_PROBE "
PROBE_TIMEOUT = 60.0
RELOAD_TIMEOUT = 60.0
CLOSE_TIMEOUT = 10.0
START_TIMEOUT = 30.0
NONCE_BYTES = 32
# Set to "1" to start Seeress's own browser without a window, as on a machine with no display.
HEADLESS = "SEERESS_HEADLESS"

PROBE_CODE = f"""\
import json


def _seeress_probe():
    marker, error = None, None
    try:
        found = cdp("Network.getCookies", urls=[{json.dumps(MARKER_URL)}])
        if found:
            for cookie in found.get("cookies", []):
                if cookie.get("name") == {json.dumps(MARKER_KEY)}:
                    marker = cookie.get("value")
        else:
            error = "empty answer"
    except Exception as exc:
        error = type(exc).__name__
    answer = {{"daemon": NAME, "kind": daemon_browser_kind(), "marker": marker, "error": error}}
    print({json.dumps(PROBE_TAG)} + json.dumps(answer))


_seeress_probe()
"""


# ------------------------------------------------------------- the probe ---


@dataclass(frozen=True)
class Probe:
    """What the browser tool reported about the browser behind a door."""

    daemon: str
    kind: str
    marker: str | None
    error: str | None

    @classmethod
    def parse(cls, output: str) -> "Probe":
        """The probe in the last marked line of the browser tool's output."""
        for line in reversed(output.strip().splitlines()):
            if not line.startswith(PROBE_TAG):
                continue
            data = json.loads(line[len(PROBE_TAG) :])
            # The shape is contracts/browser-body-probe.schema.json; a contract test keeps
            # this reader and that schema in agreement.
            if not (
                isinstance(data, dict)
                and set(data) == {"daemon", "kind", "marker", "error"}
                and isinstance(data["daemon"], str)
                and data["daemon"]
                and data["kind"] in ("local", "cdp", "cloud")
                and (data["marker"] is None or isinstance(data["marker"], str))
                and (data["error"] is None or isinstance(data["error"], str))
            ):
                raise ValueError("the probe has an unexpected shape")
            return cls(data["daemon"], data["kind"], data["marker"], data["error"])
        raise ValueError("no probe in the browser tool's output")


@dataclass(frozen=True)
class Attempt:
    """One attempt to have a door's browser prove itself: a probe, or why it could not be asked."""

    probe: Probe | None
    failure: str | None = None

    @property
    def readable(self) -> bool:
        """True when the browser answered and its marker could be read."""
        return self.probe is not None and self.probe.error is None


def attempt(profile: str, address: str | None) -> Attempt:
    """Ask the browser behind the door to prove itself."""
    try:
        result = run_browser_command(PROBE_CODE, profile=profile, ws=address, timeout=PROBE_TIMEOUT)
    except BrowserError as exc:
        return Attempt(None, str(exc))
    try:
        return Attempt(Probe.parse(result.stdout))
    except ValueError as exc:
        return Attempt(None, f"the browser tool's answer could not be read: {exc}")


def plant(profile: str, address: str | None, nonce: str) -> None:
    """Set the marker cookie in the browser at the door."""
    code = (
        f'print(cdp("Network.setCookie", name={json.dumps(MARKER_KEY)}, '
        f'value={json.dumps(nonce)}, url={json.dumps(MARKER_URL)}, path="/", '
        f"expires={MARKER_EXPIRES}))\n"
    )
    run_browser_command(code, profile=profile, ws=address, timeout=PROBE_TIMEOUT)


def reload_daemon(profile: str, address: str | None) -> bool:
    """Restart the door's daemon, so that it attaches to the browser's current address."""
    try:
        run_browser_tool(["--reload"], profile=profile, ws=address, timeout=RELOAD_TIMEOUT)
    except BrowserError:
        return False
    return True


# ------------------------------------------------------------- the bodies ---


class PersonalChrome:
    """The user's own Chrome.

    Accepted when the browser is not one of Seeress's and not a cloud browser. The
    personal door may use a cloud model, so it must never read the client browser.
    """

    def address(self) -> str | None:
        """The address Chrome published, or None to let the browser tool find Chrome."""
        return config.browser_env("personal").get("BU_CDP_WS")

    def ensure_running(self, headless: bool | None = None) -> str | None:
        """Chrome is never started by Seeress; it is only attached to.

        When Chrome published no address the browser tool looks for a browser of its own.
        That is allowed, as long as one is installed: none is ever downloaded.
        """
        address = self.address()
        if address is None:
            _require_a_browser()
        return address

    def accepts(self, asked: Attempt) -> bool:
        probe = asked.probe
        return (
            asked.readable
            and probe is not None
            and probe.marker is None
            and probe.kind in ("local", "cdp")
        )

    def heal(self, profile: str, address: str | None, asked: Attempt) -> bool:
        """Only a daemon that lost its browser can be helped: restart it."""
        if asked.readable:
            return False
        reload_daemon(profile, address)
        return True

    def refusal(self, asked: Attempt) -> str:
        probe = asked.probe
        if probe is None:
            return f"the browser behind the personal door could not be asked: {asked.failure}"
        if probe.marker is not None:
            return (
                "the browser behind the personal door is Seeress's own client browser. The "
                "personal door may use a cloud model, so it never drives the client browser. "
                "Check that Chrome is running with remote debugging allowed."
            )
        if probe.kind == "cloud":
            return (
                "the browser behind the personal door is a cloud browser. Page content would "
                "leave this machine, so Seeress does not drive it."
            )
        if probe.error is not None:
            return (
                f"the browser behind the personal door could not be checked: {probe.error}. "
                "Run `seeress reload` and try again."
            )
        return (
            f"the browser behind the personal door answered with daemon {probe.daemon!r} of "
            f"kind {probe.kind!r}, which the personal door does not allow."
        )


class SeeressBody:
    """A Chromium on a profile Seeress owns, carrying a marker only Seeress knows."""

    def __init__(self, home: Path, name: str) -> None:
        try:
            self.daemon = env.DOOR_DAEMON[name]
        except KeyError:
            raise ConfigError(
                f"no Seeress-owned browser is defined for the {name!r} door"
            ) from None
        self.name = name
        self.directory = home / "bodies" / name

    @property
    def profile_dir(self) -> Path:
        return self.directory / "profile"

    @property
    def nonce_file(self) -> Path:
        return self.directory / "nonce"

    def nonce(self) -> str:
        """The marker's value. Made once, kept in a file only the user may read."""
        try:
            value = self.nonce_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if len(value) >= NONCE_BYTES:
            return value
        value = secrets.token_urlsafe(NONCE_BYTES)
        self.nonce_file.parent.mkdir(parents=True, exist_ok=True)
        self.nonce_file.write_text(value + "\n", encoding="utf-8")
        self.nonce_file.chmod(0o600)
        return value

    def address(self) -> str | None:
        """The body's address while a browser answers on the profile, else None."""
        return _published_address(self.profile_dir)

    def ensure_running(self, headless: bool | None = None) -> str | None:
        """The body's address, starting the body when it is not running."""
        address = self.address()
        if address is None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            address = launch(self.profile_dir, headless)
        return address

    def stop(self) -> bool:
        """Close the body when it is running, and wait until it has gone.

        False when there was none to close, or when it did not close in time.
        """
        address = self.address()
        if address is None or not close_over_cdp(address):
            return False
        deadline = time.monotonic() + CLOSE_TIMEOUT
        while answers(address) and time.monotonic() < deadline:
            time.sleep(0.1)
        return not answers(address)

    def accepts(self, asked: Attempt) -> bool:
        probe = asked.probe
        return (
            asked.readable
            and probe is not None
            and probe.daemon == self.daemon
            and probe.kind == "cdp"
            and probe.marker == self.nonce()
        )

    def heal(self, profile: str, address: str | None, asked: Attempt) -> bool:
        """Restart a daemon that lost its browser; renew the marker on Seeress's own body.

        The marker is only ever written when the browser answers behind this door's own
        daemon. Whatever else answers, a cloud browser or the user's Chrome, is left alone.
        """
        if not asked.readable:
            reload_daemon(profile, address)
            return True
        probe = asked.probe
        if probe is not None and probe.daemon == self.daemon and probe.kind == "cdp":
            plant(profile, address, self.nonce())
            return True
        return False

    def refusal(self, asked: Attempt) -> str:
        probe = asked.probe
        if probe is None:
            return f"the browser behind the client door could not be asked: {asked.failure}"
        if probe.kind == "cloud":
            return (
                "the browser behind the client door is a cloud browser. Page content would "
                "leave this machine, so Seeress does not drive it."
            )
        if probe.error is not None:
            return (
                f"the browser behind the client door could not be checked: {probe.error}. "
                "Run `seeress reload` and try again."
            )
        if probe.daemon != self.daemon:
            return (
                f"the browser behind the client door answers to daemon {probe.daemon!r}, not "
                f"to {self.daemon!r}. The client door never drives the user's own Chrome. "
                "Run `seeress reload` and try again."
            )
        if probe.marker is None:
            return "the browser behind the client door carries no marker."
        return "the browser behind the client door carries a marker Seeress did not issue."


Body = PersonalChrome | SeeressBody


def body_for(profile: str) -> Body:
    try:
        config.PROFILES[profile]
    except KeyError:
        raise ConfigError(f"unknown profile {profile!r}") from None
    if profile == "personal":
        return PersonalChrome()
    return SeeressBody(config.seeress_home(), profile)


def address_for(profile: str) -> str | None:
    """The address every step on a door is run against.

    A body of Seeress's own that is not running is an error. Without its address the
    browser tool would look for a browser of its own, and find the user's. Only the
    personal door may have no address: when Chrome published none, the tool may look.
    """
    body = body_for(profile)
    address = body.address()
    if address is None and isinstance(body, SeeressBody):
        raise IsolationError(
            f"the browser Seeress owns for the {profile} door is not running. "
            "Start any task on that door first."
        )
    return address


def ensure(profile: str, headless: bool | None = None) -> None:
    """Make sure the browser behind the door is the one the door allows.

    Raises IsolationError when it is not, or when it cannot be proved. Called before any
    model request.
    """
    body = body_for(profile)
    address = body.ensure_running(headless)
    asked = attempt(profile, address)
    if body.accepts(asked):
        return
    if body.heal(profile, address, asked):
        asked = attempt(profile, address)
        if body.accepts(asked):
            return
    raise IsolationError(body.refusal(asked))


# --------------------------------------------------- talking to a browser ---


def answers(address: str) -> bool:
    """True when a browser answers on this loopback address with exactly this WebSocket URL."""
    parts = urlsplit(address)
    if parts.hostname != "127.0.0.1" or parts.port is None:
        return False
    # a proxy must never see loopback
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{parts.port}/json/version", timeout=1.0) as response:
            reported: object = json.loads(response.read()).get("webSocketDebuggerUrl")
            return reported == address
    except (OSError, ValueError):
        return False


def _published_address(profile_dir: Path) -> str | None:
    """The address a browser has published in its profile, while it still answers there."""
    try:
        port, _, path = (profile_dir / "DevToolsActivePort").read_text().strip().partition("\n")
    except OSError:
        return None
    if not port.isdigit() or not path.startswith("/devtools/browser/"):
        return None
    address = f"ws://127.0.0.1:{port}{path}"
    return address if answers(address) else None


def find_browser() -> str:
    """A Chrome, Chromium or Edge installed on this machine.

    Raises BrowserError when there is none. Seeress looks where the browser tool looks,
    and no further: it never downloads a browser.

    Seeress finds and starts the browser itself, and never loads the browser tool's
    library into its own process. That library reads its settings from the environment of
    whoever imports it, makes its own folders, and loads any .env file it finds into that
    process's environment. In Seeress's own process none of that may happen.
    """
    candidates: list[Path] = []
    if sys.platform == "darwin":
        roots = (Path("/Applications"), Path.home() / "Applications")
        for app in ("Google Chrome", "Chromium", "Microsoft Edge", "Google Chrome Canary"):
            candidates += [root / f"{app}.app/Contents/MacOS/{app}" for root in roots]
    elif sys.platform == "win32":
        folders = [
            os.environ[name]
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if os.environ.get(name)
        ]
        candidates += [Path(folder) / "Google/Chrome/Application/chrome.exe" for folder in folders]
        candidates += [
            Path(folder) / relative
            for folder in folders
            for relative in (
                "Chromium/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe",
            )
        ]
    else:
        for name in (
            "google-chrome-stable",
            "google-chrome",
            "chromium",
            "chromium-browser",
            "microsoft-edge-stable",
            "microsoft-edge",
        ):
            if found := shutil.which(name):
                return found
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise BrowserError(
        "no Chrome, Chromium or Edge was found on this machine. Install one; "
        "Seeress does not download a browser."
    )


def has_display() -> bool:
    """True where a browser can open a window. False on a server without one."""
    if platform.system() in ("Darwin", "Windows"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# Switches that stop a browser's own background services. They cut what a fresh browser
# contacts on its own account roughly in half; the route below is what makes it nothing.
QUIET: tuple[str, ...] = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-breakpad",
    "--metrics-recording-only",
    "--disable-client-side-phishing-detection",
    "--no-service-autorun",
    "--dns-prefetch-disable",
)


def bypass_rules(hosts: Collection[str]) -> list[str]:
    """The proxy bypass rules that let a browser reach exactly these sites and their subdomains."""
    return [rule for host in hosts for rule in (host, f"*.{host}")]


def browser_arguments(
    profile_dir: Path,
    *,
    headless: bool,
    allowed_hosts: Collection[str] = (),
    route: bool = True,
) -> list[str]:
    """How a browser of Seeress's own is started.

    It starts with the flags the browser tool uses; a contract test keeps the two lists the
    same. On top of those, the browser has no way out. Its proxy is a port where nothing
    listens, and only the client's own sites are exempt from it. This machine is always
    exempt, as it is for any proxy a browser is given. So a browser of Seeress's own can
    reach the client's sites, and nothing else in the world.

    Without a route the browser is left as it would be without it. That is only for a
    browser that stands in for the user's own, as the egress check does.
    """
    # First the browser tool's own flags, unchanged, except for where its start page goes.
    arguments = [
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",  # a port of the browser's choosing, published in its profile
        "--no-first-run",
        "--no-default-browser-check",
        "--allow-pre-commit-input",  # keys and clicks reach a tab that has not painted yet
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ]
    if sys.platform == "darwin":
        arguments.append(
            "--use-mock-keychain"
        )  # a profile of Seeress's own never reads the keychain
    elif sys.platform.startswith("linux"):
        arguments.append("--password-store=basic")
    if headless:
        arguments += ["--headless=new", "--window-size=1440,900"]
    if sys.platform.startswith("linux") and (
        os.geteuid() == 0 or Path("/.dockerenv").exists() or Path("/run/containerenv").exists()
    ):
        arguments.append("--no-sandbox")  # Chrome refuses to start as root with the sandbox

    # Then Seeress's own.
    arguments += QUIET
    if not route:
        return [*arguments, "about:blank"]
    arguments.append(f"--proxy-server=127.0.0.1:{env.DEAD_PROXY_PORT}")
    rules = bypass_rules(allowed_hosts)
    if rules:
        arguments.append("--proxy-bypass-list=" + ";".join(rules))
    arguments += [
        "--disable-quic",  # QUIC does not use a proxy
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # nor would WebRTC
    ]
    return [*arguments, "about:blank"]


def launch(
    profile_dir: Path,
    headless: bool | None = None,
    allowed_hosts: Collection[str] | None = None,
    route: bool = True,
) -> str:
    """Start a browser on a profile Seeress owns, with an environment of Seeress's choosing.

    The browser outlives this process. Seeress starts it itself rather than through the
    browser tool's launcher: that one hands the browser the whole environment of this
    process, and on a machine without a browser it downloads one. Returns the address the
    browser published.
    """
    binary = find_browser()
    if headless is None:
        headless = os.environ.get(HEADLESS) == "1" or not has_display()
    if allowed_hosts is None:
        # Read here, at the last moment: a settings file Seeress cannot understand stops
        # the browser from starting, rather than starting it without its route.
        allowed_hosts = config.load_settings().client_domains
    (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)  # one left by an earlier run
    try:
        subprocess.Popen(
            [
                binary,
                *browser_arguments(
                    profile_dir,
                    headless=headless,
                    allowed_hosts=allowed_hosts,
                    route=route,
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
            env=env.browser_env(),
        )
    except OSError as exc:
        raise BrowserError(
            f"the browser for a door of Seeress's own could not be started: {exc}"
        ) from exc
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        address = _published_address(profile_dir)
        if address is not None:
            return address
        time.sleep(0.1)
    raise BrowserError(f"the browser did not open its debugging port within {START_TIMEOUT:g} s")


def _require_a_browser() -> None:
    """Refuse before the browser tool's own fallback could download a browser.

    Left to itself, the tool starts a Chromium of its own where the user's Chrome cannot be
    used, and on a machine with no browser and no display that means downloading one.
    """
    try:
        find_browser()
    except BrowserError as exc:
        raise IsolationError(
            f"no browser was found for the personal door, so none is downloaded: {exc}"
        ) from exc


def close_over_cdp(address: str) -> bool:
    """Ask the browser at this loopback address to close, as when its last window closes.

    It shuts down properly first, writing cookies and storage to its profile.
    """
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    failed = (WebSocketException, OSError, TimeoutError, ValueError)
    try:
        # No proxy: a named one would stand between Seeress and its own browser.
        with connect(address, open_timeout=3, close_timeout=1, max_size=None, proxy=None) as socket:
            socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
            with contextlib.suppress(*failed):  # it may close before it answers
                socket.recv(timeout=3)
        return True
    except failed:  # any of these means only that it did not close
        return False
