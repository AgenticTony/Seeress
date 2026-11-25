"""Browser bodies: what a door accepts, what it refuses, and how it recovers.

The browser tool is replaced by a fake. These tests prove the rules; the end-to-end tests
prove that the real browser answers the probe the way the rules assume.
"""

import json
import os
import stat
import subprocess
import time
from collections.abc import Collection
from pathlib import Path

import pytest

from seeress import bodies, config, env, runner
from seeress.errors import BrowserError, ConfigError, IsolationError
from tests.conftest import Patcher

CHROME_WS = "ws://127.0.0.1:9222/devtools/browser/chrome"
CLIENT_WS = "ws://127.0.0.1:41001/devtools/browser/client"
ALIVE_WS = "ws://127.0.0.1:41001/devtools/browser/alive"

Result = str | BrowserError


def report(
    daemon: str = "seeress-client",
    kind: str = "cdp",
    marker: str | None = None,
    error: str | None = None,
) -> str:
    value = marker
    line = "SEERESS_PROBE " + json.dumps(
        {"daemon": daemon, "kind": kind, "marker": value, "error": error}
    )
    return "browser-use: something on its own line\n" + line


class Tool:
    """Stands in for the browser tool. Each probe takes the next prepared answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.reloads: list[str | None] = []
        self.planted: list[tuple[str | None, str]] = []
        self.answers: list[Result] = []

    def command(
        self, code: str, *, profile: str, ws: str | None, timeout: float = 120.0
    ) -> runner.StepResult:
        self.calls.append((code.splitlines()[0][:40], ws))
        if code == bodies.PROBE_CODE:
            if not self.answers:
                raise AssertionError("the test prepared too few probe answers")
            answer = self.answers.pop(0)
            if isinstance(answer, BrowserError):
                raise answer
            return runner.StepResult(0, answer + "\n", "", False)
        if code.startswith('print(cdp("Network.setCookie"'):
            self.planted.append((ws, code))
            return runner.StepResult(0, "{}", "", False)
        raise AssertionError(f"unexpected step: {code[:60]}")

    def tool(
        self,
        args: list[str],
        *,
        profile: str,
        ws: str | None,
        timeout: float,
        code: str | None = None,
    ) -> runner.StepResult:
        assert args == ["--reload"], args
        self.reloads.append(ws)
        return runner.StepResult(0, "", "", False)


def client_body() -> bodies.SeeressBody:
    body = bodies.body_for("client")
    assert isinstance(body, bodies.SeeressBody)
    return body


@pytest.fixture
def tool(patch: Patcher) -> Tool:
    fake = Tool()
    patch(bodies, "run_browser_command", fake.command)
    patch(bodies, "run_browser_tool", fake.tool)
    return fake


@pytest.fixture
def client(home: Path, monkeypatch: pytest.MonkeyPatch, patch: Patcher) -> bodies.SeeressBody:
    """The client door with a browser that is already running."""
    body = client_body()
    monkeypatch.setattr(body, "address", lambda: CLIENT_WS)
    patch(bodies, "launch", lambda profile_dir, headless=None: CLIENT_WS)
    return body


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> bodies.PersonalChrome:
    monkeypatch.setattr(config, "browser_env", lambda profile: {"BU_CDP_WS": CHROME_WS})
    return bodies.PersonalChrome()


# ---------------------------------------------------------- the probe ---


def test_the_probe_is_the_last_marked_line_of_the_output() -> None:
    probe = bodies.Probe.parse(report(marker="nonce-1"))

    assert probe == bodies.Probe("seeress-client", "cdp", "nonce-1", None)


def test_an_output_without_a_probe_is_an_error() -> None:
    with pytest.raises(ValueError, match="no probe"):
        bodies.Probe.parse("print('hello')\n")


def test_a_probe_that_could_not_read_the_marker_says_so() -> None:
    probe = bodies.Probe.parse(report(error="IPCError"))

    assert probe.marker is None
    assert probe.error == "IPCError"


def test_the_probe_code_reads_the_cookie_jar_of_a_reserved_origin_only() -> None:
    assert bodies.MARKER_URL == "http://seeress.invalid/"
    assert 'cdp("Network.getCookies", urls=["http://seeress.invalid/"])' in bodies.PROBE_CODE
    assert "daemon_browser_kind()" in bodies.PROBE_CODE
    assert "NAME" in bodies.PROBE_CODE


def test_the_marker_is_set_as_a_cookie_that_outlives_a_restart(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    tool.answers = [report(marker=None), report(marker=client.nonce())]
    bodies.ensure("client")
    ((_, code),) = tool.planted
    assert 'cdp("Network.setCookie"' in code
    assert 'name="seeress_body"' in code
    assert 'url="http://seeress.invalid/"' in code
    assert "expires=" in code


def test_nothing_can_resolve_the_marker_origin() -> None:
    """The .invalid domain is reserved and can never be delegated (RFC 6761).

    So the marker cannot be sent anywhere.
    """
    assert bodies.MARKER_URL == "http://seeress.invalid/"


# ------------------------------------------------------- the client door ---


def test_a_client_browser_that_shows_its_marker_is_accepted(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    tool.answers = [report(marker=client.nonce())]

    bodies.ensure("client")

    assert tool.reloads == []
    assert tool.planted == []
    assert tool.calls == [("import json", CLIENT_WS)]


def test_a_client_browser_without_a_marker_gets_one_and_is_accepted(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    tool.answers = [report(marker=None), report(marker=client.nonce())]

    bodies.ensure("client")

    assert tool.reloads == []
    ((ws, code),) = tool.planted
    assert ws == CLIENT_WS
    assert client.nonce() in code
    assert bodies.MARKER_URL in code


def test_a_stale_marker_on_seeress_s_own_body_is_renewed(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    """The nonce file can be lost while the browser keeps the old marker."""
    tool.answers = [report(marker="an-older-nonce"), report(marker=client.nonce())]

    bodies.ensure("client")

    assert tool.reloads == []
    assert len(tool.planted) == 1


def test_a_body_that_will_not_take_a_marker_is_refused(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    tool.answers = [report(marker=None)] * 3

    with pytest.raises(IsolationError, match="carries no marker"):
        bodies.ensure("client")
    assert len(tool.planted) == 1


def test_the_right_marker_behind_the_wrong_daemon_is_refused(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    """The marker alone is not enough: the door must also be speaking to its own daemon."""
    tool.answers = [report(daemon="default", marker=client.nonce())] * 3

    with pytest.raises(IsolationError, match="daemon 'default'"):
        bodies.ensure("client")
    assert tool.planted == []


def test_the_user_s_chrome_behind_the_client_door_is_refused(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    """The defect this task fixes. The daemon reports 'local' for a Chrome it discovered."""
    tool.answers = [report(daemon="default", kind="local", marker=None)] * 3

    with pytest.raises(IsolationError, match="never drives the user's own Chrome"):
        bodies.ensure("client")

    assert tool.planted == []


def test_a_cloud_browser_is_refused_behind_either_door(
    client: bodies.SeeressBody, chrome: bodies.PersonalChrome, tool: Tool
) -> None:
    tool.answers = [report(daemon="remote", kind="cloud", marker=None)] * 3
    with pytest.raises(IsolationError, match="cloud browser"):
        bodies.ensure("client")

    tool.answers = [report(daemon="remote", kind="cloud", marker=None)] * 3
    with pytest.raises(IsolationError, match="cloud browser"):
        bodies.ensure("personal")


def test_a_daemon_that_lost_its_browser_is_restarted_once(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    tool.answers = [report(error="IPCError"), report(marker=client.nonce())]

    bodies.ensure("client")

    assert tool.reloads == [CLIENT_WS]
    assert tool.planted == []


def test_a_tool_that_fails_entirely_is_retried_once_then_refused(
    client: bodies.SeeressBody, tool: Tool
) -> None:
    # the probe cannot be run at all, then it still cannot after the daemon was restarted
    tool.answers = [
        BrowserError("the browser tool exited with code 1: daemon refused the connection")
    ] * 4

    with pytest.raises(IsolationError, match="daemon refused the connection"):
        bodies.ensure("client")

    assert tool.reloads == [CLIENT_WS]


def test_an_unreadable_marker_is_never_accepted(client: bodies.SeeressBody, tool: Tool) -> None:
    tool.answers = [report(error="IPCError")] * 3

    with pytest.raises(IsolationError, match="IPCError"):
        bodies.ensure("client")


# ------------------------------------------------------ the personal door ---


@pytest.mark.parametrize("kind", ["local", "cdp"])
def test_the_user_s_chrome_with_no_marker_is_accepted(
    chrome: bodies.PersonalChrome, tool: Tool, kind: str
) -> None:
    tool.answers = [report(daemon="default", kind=kind, marker=None)]

    bodies.ensure("personal")

    assert tool.reloads == []


def test_the_personal_door_refuses_seeress_s_own_client_browser(
    chrome: bodies.PersonalChrome, tool: Tool
) -> None:
    """The personal door may use a cloud model, so it must never read the client browser."""
    tool.answers = [report(daemon="seeress-client", marker="a-nonce")] * 3

    with pytest.raises(IsolationError, match="client browser"):
        bodies.ensure("personal")


def test_the_personal_door_refuses_a_browser_it_could_not_check(
    chrome: bodies.PersonalChrome, tool: Tool
) -> None:
    tool.answers = [report(error="IPCError")] * 3

    with pytest.raises(IsolationError, match="IPCError"):
        bodies.ensure("personal")


def test_the_personal_door_asks_at_the_address_chrome_published(
    chrome: bodies.PersonalChrome, tool: Tool
) -> None:
    tool.answers = [report(daemon="default", kind="cdp", marker=None)]

    bodies.ensure("personal")

    assert tool.calls == [("import json", CHROME_WS)]


# ------------------------------------------------------ the body itself ---


def test_each_door_has_its_own_kind_of_body(home: Path) -> None:
    assert isinstance(bodies.body_for("personal"), bodies.PersonalChrome)
    assert isinstance(bodies.body_for("client"), bodies.SeeressBody)


def test_an_unknown_door_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError, match="unknown profile 'guest'"):
        bodies.body_for("guest")


def test_the_client_body_lives_in_the_seeress_home_and_names_its_own_daemon(home: Path) -> None:
    body = client_body()

    assert body.profile_dir == home / ".seeress" / "bodies" / "client" / "profile"
    assert body.daemon == "seeress-client"


def test_a_nonce_is_made_once_and_kept_private(home: Path) -> None:
    body = client_body()

    first = body.nonce()
    second = body.nonce()

    assert first == second
    assert len(first) >= 32
    mode = stat.S_IMODE(body.nonce_file.stat().st_mode)
    assert mode == 0o600
    assert body.nonce_file.read_text().strip() == first


def test_the_address_is_read_from_the_profile_and_checked_against_the_browser(
    home: Path, patch: Patcher
) -> None:
    body = client_body()
    body.profile_dir.mkdir(parents=True)
    port_file = body.profile_dir / "DevToolsActivePort"
    asked: list[str] = []

    def answers(ws: str) -> bool:
        asked.append(ws)
        return ws.endswith("alive")

    patch(bodies, "answers", answers)

    port_file.write_text("41001\n/devtools/browser/alive\n")
    assert body.address() == ALIVE_WS
    assert asked == [ALIVE_WS]

    port_file.write_text("41001\n/devtools/browser/gone\n")
    assert body.address() is None


def test_without_a_port_file_there_is_no_address(home: Path) -> None:
    assert client_body().address() is None


def test_a_missing_body_is_started_on_its_own_profile_never_found_somewhere_else(
    home: Path, monkeypatch: pytest.MonkeyPatch, patch: Patcher
) -> None:
    body = client_body()
    started: list[tuple[Path, bool | None]] = []

    def launch(profile_dir: Path, headless: bool | None = None) -> str:
        started.append((profile_dir, headless))
        return CLIENT_WS

    monkeypatch.setattr(body, "address", lambda: None)
    patch(bodies, "launch", launch)

    assert body.ensure_running() == CLIENT_WS
    assert started == [(body.profile_dir, None)]
    assert body.profile_dir.is_dir()


def test_a_running_body_is_not_started_again(client: bodies.SeeressBody) -> None:
    assert client.ensure_running() == CLIENT_WS


def test_stopping_a_body_that_is_not_running_does_nothing(home: Path) -> None:
    assert client_body().stop() is False


def test_stopping_a_body_closes_it_and_waits_until_it_has_gone(
    client: bodies.SeeressBody, patch: Patcher
) -> None:
    closed: list[str] = []

    def close(ws: str) -> bool:
        closed.append(ws)
        return True

    patch(bodies, "close_over_cdp", close)
    patch(bodies, "answers", lambda ws: False)

    assert client.stop() is True
    assert closed == [CLIENT_WS]


def test_stopping_waits_for_a_body_that_is_slow_to_go(
    client: bodies.SeeressBody, monkeypatch: pytest.MonkeyPatch, patch: Patcher
) -> None:
    """Browser.close is only a request: the browser exits a moment later."""
    patch(bodies, "close_over_cdp", lambda ws: True)
    alive = iter([True, True, False, False])

    def answers(ws: str) -> bool:
        return next(alive)

    patch(bodies, "answers", answers)
    # the deadline is set, then the clock is read once per pass of the loop
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    assert client.stop() is True


def test_stopping_reports_a_body_that_would_not_close(
    client: bodies.SeeressBody, monkeypatch: pytest.MonkeyPatch, patch: Patcher
) -> None:
    patch(bodies, "close_over_cdp", lambda ws: True)
    patch(bodies, "answers", lambda ws: True)
    clock = iter([0.0, 10_000.0])  # the deadline passes at once
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    assert client.stop() is False


def test_stopping_a_body_that_refused_the_request_changes_nothing(
    client: bodies.SeeressBody, patch: Patcher
) -> None:
    patch(bodies, "close_over_cdp", lambda ws: False)

    assert client.stop() is False


def test_a_client_door_without_its_browser_refuses_to_run_anything(
    home: Path, patch: Patcher
) -> None:
    """Without its own browser the tool would look for one, and find the user's."""
    started: list[str] = []
    patch(bodies, "launch", lambda profile_dir, headless=None: started.append("launched"))

    with pytest.raises(IsolationError, match="is not running"):
        bodies.address_for("client")

    assert started == []


def test_a_personal_door_without_an_address_may_let_the_tool_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "browser_env", lambda profile: {})

    assert bodies.address_for("personal") is None


def test_a_body_on_a_port_file_that_answers_is_the_door_s_address(
    home: Path, patch: Patcher
) -> None:
    port_file = home / ".seeress" / "bodies" / "client" / "profile" / "DevToolsActivePort"
    port_file.parent.mkdir(parents=True)
    port_file.write_text("41001\n/devtools/browser/client\n")
    patch(bodies, "answers", lambda ws: True)

    assert bodies.address_for("client") == CLIENT_WS


def test_the_nonce_file_is_not_readable_by_anyone_else(home: Path) -> None:
    body = client_body()
    body.nonce()

    assert stat.S_IMODE(body.nonce_file.stat().st_mode) == 0o600
    assert os.access(body.nonce_file, os.R_OK)


# ------------------------------------------------- starting a browser ---


class Toolkit:
    """Stands in for what Seeress must know to start a browser.

    Where a browser is, and whether there is a display.
    """

    def __init__(self, missing: bool = False, display: bool = True) -> None:
        self.missing = missing
        self.display = display

    def find_browser(self) -> str:
        if self.missing:
            raise BrowserError("no Chrome, Chromium or Edge was found on this machine.")
        return "/Applications/Example.app/Contents/MacOS/Example"

    def has_display(self) -> bool:
        return self.display


class Started:
    """What was started, standing in for subprocess.Popen."""

    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.options: dict[str, object] = {}
        self.opens_its_port = True

    def __call__(self, arguments: list[str], **options: object) -> None:
        self.arguments = arguments
        self.options = options
        if self.opens_its_port:
            for argument in arguments:
                if argument.startswith("--user-data-dir="):
                    port_file = Path(argument.split("=", 1)[1]) / "DevToolsActivePort"
                    port_file.parent.mkdir(parents=True, exist_ok=True)
                    port_file.write_text("41001\n/devtools/browser/client\n")


class Launch:
    """What a launch was told: where a browser is, whether there is a display, what started."""

    def __init__(self, toolkit: Toolkit, started: Started, asked: list[bool]) -> None:
        self.toolkit = toolkit
        self.started = started
        self.asked = asked


@pytest.fixture
def starter(home: Path, monkeypatch: pytest.MonkeyPatch, patch: Patcher) -> Launch:
    toolkit, started, asked = Toolkit(), Started(), []
    real_arguments = bodies.browser_arguments

    def arguments(
        profile_dir: Path,
        *,
        headless: bool,
        allowed_hosts: Collection[str] = (),
        route: bool = True,
    ) -> list[str]:
        asked.append(headless)
        return real_arguments(
            profile_dir, headless=headless, allowed_hosts=allowed_hosts, route=route
        )

    patch(bodies, "find_browser", toolkit.find_browser)
    patch(bodies, "has_display", toolkit.has_display)
    patch(bodies, "browser_arguments", arguments)
    patch(bodies, "answers", lambda ws: True)
    monkeypatch.setattr(subprocess, "Popen", started)
    monkeypatch.delenv(bodies.HEADLESS, raising=False)
    return Launch(toolkit, started, asked)


def planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings in Seeress's own environment that no browser has any use for."""
    for name, value in {
        "ZAI_API_KEY": "a-cloud-key",
        "BROWSER_USE_API_KEY": "a-library-key",
        "HTTPS_PROXY": "http://127.0.0.1:9/",
        "ANONYMIZED_TELEMETRY": "true",
        "BH_HOME": "/somewhere/else",
        "SEERESS_EGRESS_LOG": "/a/log",
    }.items():
        monkeypatch.setenv(name, value)


def test_a_body_is_started_by_seeress_with_an_allow_listed_environment(
    monkeypatch: pytest.MonkeyPatch, starter: Launch
) -> None:
    """The browser tool's own launcher hands a browser the whole environment of this process."""
    planted(monkeypatch)
    profile = client_body().profile_dir

    address = bodies.launch(profile, headless=True)

    assert address == CLIENT_WS
    assert starter.started.arguments[0] == "/Applications/Example.app/Contents/MacOS/Example"
    assert f"--user-data-dir={profile}" in starter.started.arguments
    assert starter.asked == [True]
    child = starter.started.options["env"]
    assert isinstance(child, dict)
    assert set(child) == set(env.browser_env())
    for name in (
        "ZAI_API_KEY",
        "BROWSER_USE_API_KEY",
        "HTTPS_PROXY",
        "BH_HOME",
        "SEERESS_EGRESS_LOG",
        "SEERESS_HOME",
    ):
        assert name not in child
    assert starter.started.options["start_new_session"] is True
    assert starter.started.options["stdin"] == subprocess.DEVNULL
    assert starter.started.options["stdout"] == subprocess.DEVNULL
    assert starter.started.options["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    ("given", "setting", "display", "expected"),
    [
        (True, None, True, True),
        (False, "1", True, False),
        (None, "1", True, True),
        (None, None, False, True),
        (None, None, True, False),
    ],
)
def test_a_browser_is_headless_when_asked_for_or_when_there_is_no_display(
    monkeypatch: pytest.MonkeyPatch,
    starter: Launch,
    given: bool | None,
    setting: str | None,
    display: bool,
    expected: bool,
) -> None:
    starter.toolkit.display = display
    if setting is not None:
        monkeypatch.setenv(bodies.HEADLESS, setting)

    bodies.launch(client_body().profile_dir, headless=given)

    assert starter.asked == [expected]


def test_no_browser_to_start_is_reported_and_none_is_downloaded(starter: Launch) -> None:
    starter.toolkit.missing = True

    with pytest.raises(BrowserError, match="no Chrome, Chromium or Edge was found"):
        bodies.launch(client_body().profile_dir, headless=True)
    assert starter.started.arguments == []


def test_a_browser_that_never_opens_its_port_is_reported(
    monkeypatch: pytest.MonkeyPatch, starter: Launch
) -> None:
    starter.started.opens_its_port = False
    clock = iter([0.0, 0.0, 10_000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    with pytest.raises(BrowserError, match="did not open its debugging port"):
        bodies.launch(client_body().profile_dir, headless=True)


def test_a_stale_port_file_is_removed_before_the_browser_starts(starter: Launch) -> None:
    profile = client_body().profile_dir
    profile.mkdir(parents=True)
    (profile / "DevToolsActivePort").write_text("1\n/devtools/browser/stale\n")

    assert bodies.launch(profile, headless=True) == CLIENT_WS


def test_the_personal_door_refuses_when_no_browser_is_installed_rather_than_downloading_one(
    monkeypatch: pytest.MonkeyPatch, starter: Launch
) -> None:
    """Left to itself the browser tool downloads a Chromium on a machine with no browser."""
    starter.toolkit.missing = True
    monkeypatch.setattr(config, "browser_env", lambda profile: {})

    with pytest.raises(IsolationError, match="so none is downloaded"):
        bodies.PersonalChrome().ensure_running()
    assert starter.started.arguments == []


def test_the_personal_door_with_no_address_is_allowed_when_a_browser_exists(
    monkeypatch: pytest.MonkeyPatch, starter: Launch
) -> None:
    monkeypatch.setattr(config, "browser_env", lambda profile: {})

    assert bodies.PersonalChrome().ensure_running() is None


# ------------------------------------------------------- the browser's route ---


def test_the_bypass_rules_cover_a_site_and_its_subdomains() -> None:
    assert bodies.bypass_rules(["corp.example.com", "portal.client.example"]) == [
        "corp.example.com",
        "*.corp.example.com",
        "portal.client.example",
        "*.portal.client.example",
    ]
    assert bodies.bypass_rules([]) == []


def test_a_browser_of_seeress_s_own_is_given_no_way_out() -> None:
    arguments = bodies.browser_arguments(
        Path("/p/profile"), headless=True, allowed_hosts=("corp.example.com",)
    )

    assert f"--proxy-server=127.0.0.1:{env.DEAD_PROXY_PORT}" in arguments
    assert "--proxy-bypass-list=corp.example.com;*.corp.example.com" in arguments
    for switch in ("--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"):
        assert switch in arguments
    for switch in bodies.QUIET:
        assert switch in arguments


def test_with_no_client_site_listed_nothing_is_exempt_from_the_route() -> None:
    arguments = bodies.browser_arguments(Path("/p/profile"), headless=True)

    assert not any(argument.startswith("--proxy-bypass-list") for argument in arguments)


def test_this_machine_is_always_reachable_and_never_bypassed_explicitly() -> None:
    """A browser never sends this machine through a proxy it is given. Seeress relies on
    that, and never adds a rule that would send it through."""
    arguments = bodies.browser_arguments(
        Path("/p/profile"), headless=True, allowed_hosts=("corp.example.com",)
    )

    assert "<-loopback>" not in " ".join(arguments)


def test_the_client_sites_come_from_the_settings_file(starter: Launch, home: Path) -> None:
    (home / ".seeress").mkdir(parents=True, exist_ok=True)
    (home / ".seeress" / "config.toml").write_text(
        '[routing]\nclient_domains = ["Portal.Corp.Example.com"]\n'
    )

    bodies.launch(client_body().profile_dir, headless=True)

    assert (
        "--proxy-bypass-list=portal.corp.example.com;*.portal.corp.example.com"
        in starter.started.arguments
    )


def test_a_settings_file_seeress_cannot_read_stops_the_browser_from_starting(
    starter: Launch, home: Path
) -> None:
    """A browser must never start without knowing its route."""
    (home / ".seeress").mkdir(parents=True, exist_ok=True)
    (home / ".seeress" / "config.toml").write_text("[routing\nclient_domains = [\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        bodies.launch(client_body().profile_dir, headless=True)
    assert starter.started.arguments == []


def test_a_caller_may_name_the_sites_itself(starter: Launch) -> None:
    bodies.launch(client_body().profile_dir, headless=True, allowed_hosts=("iana.org",))

    assert "--proxy-bypass-list=iana.org;*.iana.org" in starter.started.arguments


def test_the_connection_that_closes_the_browser_has_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser is asked to close over a loopback address, and a proxy must never stand
    between: the library's default would send even a loopback address to a proxy that is
    named."""
    from websockets.sync import client

    asked: dict[str, object] = {}

    class Standing:
        def __init__(self, address: str, **options: object) -> None:
            asked.update(options)

        def __enter__(self) -> "Standing":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def send(self, message: str) -> None:
            return None

        def recv(self, timeout: float | None = None) -> str:
            return json.dumps({"id": 1, "result": {}})

    monkeypatch.setattr(client, "connect", Standing)

    assert bodies.close_over_cdp("ws://127.0.0.1:9/devtools/browser/one") is True

    assert asked.get("proxy", "never asked") is None
