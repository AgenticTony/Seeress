"""Reading and judging the record that seeress.egress_hook writes.

The rules are plain, and they are what the egress check enforces:

- Seeress's own processes talk to loopback and to nobody else.
- A child is given only the environment Seeress allows, whatever Seeress itself was given.
- The library's telemetry, release checks and cloud-browser spawn reach each of its
  processes switched off, whatever the environment says.
- An instrument that saw nothing has proved nothing. The browser tool is on the record
  for each time it was started, and no child was started without the recording switch.
"""

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import env
from .egress_hook import LOCAL_SOCKET, LOG

# Where a browser of Seeress's own is given no way out, and where the check points every
# proxy setting. Nothing listens there.
DEAD_PROXY_PORT = env.DEAD_PROXY_PORT

# What counts as this machine: its loopback addresses, and a socket that is a file.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", LOCAL_SOCKET})

# What each kind of child may be given.
TOOL_ENVIRONMENT = (
    frozenset(env.PASSTHROUGH)
    | frozenset(env.FORCED)
    | {
        "BH_HOME",
        "BU_NAME",
        "BU_CDP_WS",
    }
)
BROWSER_ENVIRONMENT = frozenset(env.BROWSER_PASSTHROUGH)

# A browser Seeress starts is known by the profile it was given, and by its name otherwise.
# It is not a Python process, so it is never on the record: what it contacts is sampled.
BROWSER_OPTION = "--user-data-dir"
BROWSER_PROGRAMS = (
    "google chrome",
    "chrome",
    "chromium",
    "microsoft edge",
    "msedge",
    "google chrome canary",
)

# The library's processes, by what was started: the browser tool, and anything of its own.
TOOL_PROGRAMS = ("browser-use", "browser_use.cli")
LIBRARY_PROGRAMS = ("browser-use", "browser_use", "browser_harness")

# What each of the library's processes must be told, whatever Seeress itself was given.
SWITCHES_OFF = {
    "ANONYMIZED_TELEMETRY": "false",
    "BROWSER_USE_NO_UPDATE_CHECK": "1",
    "BH_UPDATE_CHECK": "0",
}
NEVER_GIVEN = ("BU_AUTOSPAWN",)

# What the browser tool adds when it starts its own daemon, and what macOS adds to every
# process. None of it is Seeress's to give: Seeress may hand a child none of these names.
# None of it is a key, a proxy or an address. Anything else the tool hands down is a finding.
LIBRARY_OWN = frozenset(
    {
        "BH_CLIENT",  # how the library names itself to its own telemetry
        "BH_CLIENT_VERSION",
        "BH_DEVICE_NAME",  # a fixed string, "browser-use", unless the caller sets one
        "BH_OPEN_LIVE_URL",  # set to off: a live-view URL grants control of the browser
        "PYDANTIC_AI_NO_BANNER",
        "__CF_USER_TEXT_ENCODING",  # macOS puts this in every process
    }
)


@dataclass(frozen=True)
class Event:
    """One thing a Python process did."""

    pid: int
    parent: int
    role: str
    kind: str
    host: str | None = None
    port: int | None = None
    origin: str | None = None
    program: str | None = None
    env: tuple[str, ...] | None = None
    switches: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Finding:
    """A rule the record breaks."""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def read_events(path: Path) -> list[Event]:
    """The record, in the order it was written. A line that cannot be read is skipped."""
    events: list[Event] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            switches = data.get("switches")
            events.append(
                Event(
                    pid=int(data["pid"]),
                    parent=int(data["parent"]),
                    role=str(data["role"]),
                    kind=str(data["event"]),
                    host=data.get("host"),
                    port=data.get("port"),
                    origin=data.get("origin"),
                    program=data.get("program"),
                    env=tuple(data["env"]) if data.get("env") is not None else None,
                    switches=dict(switches) if isinstance(switches, dict) else None,
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return events


def _host_of(event: Event) -> str | None:
    if event.kind == "request" and event.origin:
        try:
            return urlsplit(event.origin).hostname
        except ValueError:
            return event.origin
    return event.host


def contacts(events: Iterable[Event]) -> dict[str, int]:
    """Every destination contacted, with how many times."""
    seen: dict[str, int] = {}
    for event in events:
        if event.kind not in ("connect", "resolve", "request"):
            continue
        host = _host_of(event)
        if host is None:
            continue
        if event.kind == "request":
            name = str(event.origin)
        else:
            name = f"{host}:{event.port}" if event.port else str(host)
        seen[name] = seen.get(name, 0) + 1
    return seen


def _is_browser(program: str | None) -> bool:
    if not program:
        return False
    return BROWSER_OPTION in program or program.lower().startswith(BROWSER_PROGRAMS)


def _is_tool(program: str | None) -> bool:
    return any(name in (program or "") for name in TOOL_PROGRAMS)


def _is_library(program: str | None) -> bool:
    return any(name in (program or "") for name in LIBRARY_PROGRAMS)


def _judge_spawn(event: Event, who: str) -> list[Finding]:
    """A child is judged by what it was given.

    A child that inherits is not judged again: it was given what its parent was given.
    """
    if event.env is None:
        return []
    given = set(event.env)
    name = event.program or "a child"
    findings: list[Finding] = []
    planted = sorted(given & set(env.DENIED_EXAMPLES))
    if planted:
        findings.append(Finding("environment", f"{name} was given {', '.join(planted)}"))
    if _is_browser(event.program):
        # A browser is not a Python process: what it contacts is sampled, not recorded.
        allowed: frozenset[str] = BROWSER_ENVIRONMENT
    else:
        if LOG not in given:
            findings.append(
                Finding(
                    "instrument",
                    f"{who} started {name} without the recording switch. Had it been Python, "
                    "nothing it did would be on the record.",
                )
            )
        # A process of the library's adds names of its own for its children. Seeress does not.
        allowed = TOOL_ENVIRONMENT | LIBRARY_OWN if _is_library(event.role) else TOOL_ENVIRONMENT
    unknown = sorted(given - allowed - set(env.DENIED_EXAMPLES))  # planted: said once
    if unknown:
        findings.append(Finding("environment", f"{name} was given {', '.join(unknown)}"))
    if _is_library(event.program):
        switches = event.switches or {}
        for setting, expected in SWITCHES_OFF.items():
            if switches.get(setting) != expected:
                findings.append(
                    Finding(
                        "switch", f"{name} was told {setting}={switches.get(setting, 'nothing')}"
                    )
                )
        for setting in NEVER_GIVEN:
            if setting in given:
                findings.append(Finding("switch", f"{name} was given {setting}"))
    return findings


def judge(
    events: Collection[Event],
    *,
    endpoint_port: int,
    seeress_process: str = "seeress.cli",
    starts_browser_tool: bool = True,
) -> list[Finding]:
    """The rules the record breaks.

    endpoint_port is the port of the one destination the run must have reached on this
    machine: the model endpoint of a task, or the browser of a capture. A connection to it
    from Seeress's own process is required, for without it the run did not happen as
    intended, and an empty record would prove nothing.

    seeress_process names Seeress's own process in the record. A task starts the browser
    tool, and it must be on the record; starts_browser_tool says so. A capture starts
    nothing, and a record that shows its process starting the browser tool is a finding.
    """
    findings: list[Finding] = []
    for event in events:
        who = f"{event.role or 'a process'} (pid {event.pid})"
        if event.kind in ("connect", "resolve", "request"):
            host = _host_of(event)
            if host is not None and host not in LOOPBACK:
                findings.append(Finding("contact", f"{who} contacted {host}"))
            elif event.port == DEAD_PROXY_PORT:
                findings.append(
                    Finding(
                        "proxy",
                        f"{who} used the proxy setting it should not have been given",
                    )
                )
        elif event.kind == "spawn":
            findings += _judge_spawn(event, who)
    own = [event for event in events if seeress_process in (event.role or "")]
    if not own:
        return [
            *findings,
            Finding(
                "instrument",
                "no Python process of Seeress was observed. The record proves nothing.",
            ),
        ]
    if not any(
        event.kind == "connect" and event.port == endpoint_port
        for event in own
        if event.host in LOOPBACK
    ):
        return [
            *findings,
            Finding(
                "instrument",
                f"Seeress's own process was not seen reaching port {endpoint_port} on this "
                "machine: the model endpoint of a task, or the browser of a capture. The run "
                "did not happen as intended.",
            ),
        ]
    if not starts_browser_tool:
        started = [
            event
            for event in events
            if event.kind == "spawn" and event.pid == own[0].pid and _is_tool(event.program)
        ]
        if started:
            return [
                *findings,
                Finding(
                    "task",
                    "Seeress's own process started the browser tool, which a capture never "
                    "does: something else ran in its place",
                ),
            ]
        return findings
    return [*findings, *_incomplete(events, own[0].pid, seeress_process)]


def _incomplete(events: Collection[Event], seeress: int, seeress_process: str) -> list[Finding]:
    """Is the browser tool on the record for each time Seeress started it?

    Each process records that it exists when it starts. The browser tool is where the
    library's own code runs, so a run in which it was started but never seen has a record
    with a hole exactly where it matters. It happened: the recording switch was once left
    off the allow-list, and the tool's processes were silent through two runs.
    """
    started = [
        event
        for event in events
        if event.kind == "spawn"
        and event.pid == seeress
        and seeress_process in (event.role or "")
        and _is_tool(event.program)
    ]
    if not started:
        return [Finding("instrument", "the browser tool was never started: no browser step ran")]
    # Each run of the tool is a child of Seeress's own process; the process it became
    # recorded its start with that parent.
    seen = [
        event
        for event in events
        if event.kind == "start" and event.parent == seeress and _is_tool(event.role)
    ]
    if len(seen) < len(started):
        return [
            Finding(
                "instrument",
                f"the browser tool was started {len(started)} time(s); {len(seen)} of its "
                "processes were observed. The record has a hole.",
            )
        ]
    return []
