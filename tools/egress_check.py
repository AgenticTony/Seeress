#!/usr/bin/env python3
"""Does Seeress contact anyone it should not?

A scripted task is run through each door, and every Python process in the tree records
what it contacts and what it starts. The check fails if a host other than loopback is
contacted, if a child is given an environment it should not have, or if the instrument
itself saw nothing.

The third door is not a door of the agent: it is the capture, run as the logger of the
record on a browser Seeress starts, which stands where the person's own Chrome would be. The
capture must reach that browser and nothing else, start nothing, and hold the one click the
check makes while it runs. What the stand-in browser itself contacts is reported, as under
the personal door, and never judged: a real Chrome talks to its maker.

Nothing is real except the browser. The model is a script served on loopback, and the
page the task opens is served from loopback. The personal door attaches to a throwaway
browser that stands where the user's Chrome would be; with --personal-chrome it attaches
to the real one.

To prove the controls rather than assume them, Seeress is started with a cloud key, a
library key, proxy settings and switches that turn telemetry on. None of that may reach a
child. A control process runs first: if the instrument cannot see a connection and a
child's environment that it made on purpose, the check fails.

    uv run python tools/egress_check.py [--doors personal,client,capture]
                                         [--personal-chrome] [--keep] [--json FILE]

Exit status 0 when every door is clean, 1 otherwise.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from seeress import bodies, config, egress
from seeress.egress import Event, Finding
from seeress.egress_hook import LOG
from seeress.errors import SeeressError

REPO = Path(__file__).resolve().parents[1]
HOOK_FILE = "zz-seeress-egress-check.pth"
HOOK_LINE = (
    "import os; os.environ.get('SEERESS_EGRESS_LOG') and "
    "__import__('seeress.egress_hook', fromlist=['install']).install()"
)
SHORT_TMP = "/tmp" if sys.platform != "win32" else tempfile.gettempdir()  # noqa: S108 - mkdtemp makes each folder private
RUN_TIMEOUT = 240.0
DOORS = (*config.PROFILES, "capture")
TASK = "open the local page and read its title"
# How long the capture runs in its door: long enough for the click, and no longer.
CAPTURE_SECONDS = "10"
# The capture's schedule in its door: all day, so that the hour of the run is nothing.
CAPTURE_SETTINGS = (
    '[capture]\ndays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]\n'
    'from = "00:00"\nto = "24:00"\n'
)
# 0.0.0.0 is this machine too, but it is not on a browser's exempt list the way 127.0.0.1
# is. So the same page shows both outcomes without anything leaving the machine.
TRY_ROUTE = "then try the same page on another address of this machine"
TASK_WITH_ROUTE = f"{TASK}, {TRY_ROUTE}"
OPEN = (
    "```python\n"
    'new_tab("http://127.0.0.1:{port}/page")\n'
    'wait_for_load()\nprint(js("document.title"))\n```'
)
TRY = (
    "```python\n"
    'print(goto_url("http://0.0.0.0:{port}/page"))\n'
    'wait_for_load(4)\nprint(js("document.title"))\n```'
)
DONE = "DONE: the local page is open"
REFUSED_MARKER = "ERR_PROXY_CONNECTION_FAILED"
REFUSED = f"{DONE}; the other address of this machine was refused"
LOADED = f"{DONE}; the other address of this machine LOADED, which it must not"

# Where the library looks for a local browser when it starts its daemon, to decide whether
# a cloud browser should be offered instead. It looks there before it reads the address
# Seeress gave it, so it asks whatever is listening: usually the user's own Chrome. It is
# a read of a version string on this machine, and it is reported under every door.
PROBE_ORIGINS = ("http://127.0.0.1:9222", "http://127.0.0.1:9223")

# What Seeress is started with, to prove that none of it reaches a child.
PLANTED: dict[str, str] = {
    "ZAI_API_KEY": "a-cloud-key-that-must-not-reach-a-child",
    "OPENAI_API_KEY": "a-cloud-key-that-must-not-reach-a-child",
    "ANTHROPIC_API_KEY": "a-cloud-key-that-must-not-reach-a-child",
    "BROWSER_USE_API_KEY": "a-library-key-that-must-not-reach-a-child",
    "BU_AUTOSPAWN": "1",
    "BU_BROWSER_ID": "a-cloud-browser",
    "BU_CDP_URL": "http://127.0.0.1:9/devtools/browser/planted",
    "BH_TELEMETRY": "on",
    "ANONYMIZED_TELEMETRY": "true",
    "BROWSER_USE_NO_UPDATE_CHECK": "0",
    "BH_UPDATE_CHECK": "1",
    "BH_HOME": "/a/home/of-someone-else",
    "BH_AGENT_WORKSPACE": "/a/workspace/of-someone-else",
    # The model on this machine is plain http, and urllib reads http_proxy for it: a request
    # to the model that follows a proxy would leave with what was said to the model.
    "HTTP_PROXY": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "http_proxy": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "HTTPS_PROXY": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "https_proxy": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "ALL_PROXY": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "all_proxy": f"http://127.0.0.1:{egress.DEAD_PROXY_PORT}/",
    "PYTHONPATH": "/a/path/of-someone-else",
}


def _answer(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ---------------------------------------------------------------- the site ---


class Site:
    """A model endpoint and a web page, both on loopback. Nothing leaves the machine."""

    def __init__(self) -> None:
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body: Any = json.loads(self.rfile.read(length) or b"{}")
                self._send(json.dumps(_answer(site.reply(body.get("messages", [])))).encode())

            def do_GET(self) -> None:
                if self.path == "/page":
                    self._send(
                        b"<!doctype html><title>seeress egress check</title><h1>A local page</h1>",
                        "text/html",
                    )
                elif self.path == "/v1/models":
                    self._send(b'{"data": []}')
                else:
                    self.send_error(404)

            def _send(self, content: bytes, kind: str = "application/json") -> None:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.port = 0
        self.requests = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        address = self._server.server_address
        self.port = int(address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def reply(self, messages: list[dict[str, str]]) -> str:
        """The scripted model: one step that opens the local page, then done.

        When the task asks for the route to be tried, a second step asks for the same page
        on another address of this machine, and the answer says whether it was refused.
        """
        self.requests += 1
        turns = [turn.get("content", "") for turn in messages if turn.get("role") == "user"]
        observations = [text for text in turns if "Execution output" in text]
        task = turns[0] if turns else ""
        if TRY_ROUTE not in task:
            return DONE if observations else OPEN.format(port=self.port)
        if not observations:
            return OPEN.format(port=self.port)
        if len(observations) == 1:
            return TRY.format(port=self.port)
        if REFUSED_MARKER in observations[-1]:
            return REFUSED
        return LOADED

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------- the browser's own ---


class BrowserSampler(threading.Thread):
    """Best effort: which remote addresses a browser of this run talks to.

    A browser is not a Python process, so the recording hook does not see it. This looks
    at it from outside, twice a second, so a short connection can be missed: the route
    is tried directly as well. For the browser Seeress owns, what this sees is a finding.
    The user's own browser is none of the check's business; what it contacts is reported
    and never judged.
    """

    def __init__(self, markers: Sequence[str]) -> None:
        super().__init__(daemon=True)
        self.markers = markers
        self.seen: set[str] = set()
        self.note = ""
        self._running = True

    def stop(self) -> None:
        self._running = False
        if self.ident is not None:
            self.join(timeout=15)

    def run(self) -> None:
        if not (shutil.which("lsof") and shutil.which("pgrep")):
            self.note = "lsof or pgrep is not installed: the browser's connections were not sampled"
            return
        while self._running:
            try:
                self._sample()
            except (OSError, subprocess.SubprocessError) as exc:
                self.note = f"sampling stopped: {type(exc).__name__}"
                return
            time.sleep(0.4)

    def _sample(self) -> None:
        for marker in self.markers:
            found = subprocess.run(
                ["pgrep", "-f", marker],  # noqa: S607 - found on PATH by design
                capture_output=True,
                text=True,
                check=False,
            )
            pids = [item for item in found.stdout.split() if item.isdigit()]
            if not pids:
                continue
            listing = subprocess.run(
                ["lsof", "-a", "-iTCP", "-n", "-P", "-p", ",".join(pids), "-Fn"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            for line in listing.stdout.splitlines():
                if line.startswith("n") and "->" in line:
                    remote = line[1:].split("->", 1)[1].split(" ", 1)[0]
                    if remote.rsplit(":", 1)[0] not in egress.LOOPBACK:
                        self.seen.add(remote)


# ------------------------------------------------------------ the workspace ---


def hook_path() -> Path:
    return Path(sysconfig.get_paths()["purelib"]) / HOOK_FILE


class Workspace:
    """A throwaway Seeress home, a site on loopback and the recording hook.

    While it exists, every Python process started from this environment with
    SEERESS_EGRESS_LOG set records what it does. Nothing is left running or lying behind.
    """

    def __init__(self, keep: bool) -> None:
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="sr-egress-", dir=SHORT_TMP)).resolve()
        self.home = self.root / "seeress"
        self.home.mkdir()
        self.stand_in_home = self.root / "home"
        (self.stand_in_home / config.CHROME_PORT_FILE).parent.mkdir(parents=True)
        self.site = Site()
        (self.home / config.CONFIG_FILE).write_text(
            "[[endpoints]]\n"
            'name = "scripted"\n'
            f'base_url = "http://127.0.0.1:{self.site.port}/v1"\n',
            encoding="utf-8",
        )
        self._saved_home = os.environ.get("SEERESS_HOME")
        # The tool's own calls into seeress.bodies act on the throwaway home.
        os.environ["SEERESS_HOME"] = str(self.home)

    def __enter__(self) -> "Workspace":
        if hook_path().exists():
            raise SystemExit("an egress check is already running: its hook file is present")
        hook_path().write_text(HOOK_LINE + "\n", encoding="utf-8")
        return self

    def __exit__(self, *exc_info: object) -> None:
        body = bodies.body_for("client")
        if isinstance(body, bodies.SeeressBody):
            body.stop()
        for door in ("client", "personal"):
            bodies.reload_daemon(door, None)
        self.site.close()
        hook_path().unlink(missing_ok=True)
        if self._saved_home is None:
            os.environ.pop("SEERESS_HOME", None)
        else:
            os.environ["SEERESS_HOME"] = self._saved_home
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------- the runs ---


@dataclass
class Run:
    door: str
    command: list[str]
    exit_status: int
    output: str
    model_requests: int
    events: list[Event] = field(default_factory=list)
    browser_contacts: list[str] = field(default_factory=list)
    browser_note: str = ""
    findings: list[Finding] = field(default_factory=list)


def _close(door: str, stand_in: str | None) -> None:
    try:
        if stand_in is not None:
            bodies.close_over_cdp(stand_in)
        if door == "client":
            body = bodies.body_for("client")
            if isinstance(body, bodies.SeeressBody):
                body.stop()
    finally:
        bodies.reload_daemon(door, None)


def run_door(workspace: Workspace, door: str, personal_chrome: bool) -> Run:
    """Run the scripted task through one door and judge what was recorded."""
    log = workspace.root / f"{door}.jsonl"
    log.write_text("", encoding="utf-8")
    child: dict[str, str] = {
        **os.environ,
        **PLANTED,
        "SEERESS_HOME": str(workspace.home),
        LOG: str(log),
        bodies.HEADLESS: "1",
    }
    child.pop("SEERESS_PROFILE", None)
    # The client door is also asked to try a destination that is not exempt from its route.
    task = TASK_WITH_ROUTE if door == "client" else TASK
    command = [sys.executable, "-m", "seeress.cli", "--profile", door, task]
    run = Run(door, command, -1, "", 0)
    sampler = BrowserSampler([])
    try:
        profiles: list[Path] = []
        stand_in: str | None = None
        if door == "personal" and not personal_chrome:
            profiles.append(workspace.root / "stand-in")
            stand_in = bodies.launch(profiles[0], headless=True, route=False)
            parts = urlsplit(stand_in)
            (workspace.stand_in_home / config.CHROME_PORT_FILE).write_text(
                f"{parts.port}\n{parts.path}\n", encoding="utf-8"
            )
            # Chrome, as far as this run is concerned, published that address.
            child["HOME"] = str(workspace.stand_in_home)
        if door == "client":
            body = bodies.body_for("client")
            if isinstance(body, bodies.SeeressBody):
                profiles.append(body.profile_dir)
        sampler = BrowserSampler([f"user-data-dir={profile}" for profile in profiles])
        sampler.start()
        before = workspace.site.requests
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT,
                env=child,
                cwd=REPO,
                check=False,
            )
        finally:
            sampler.stop()
            _close(door, stand_in)
        run.exit_status = proc.returncode
        run.output = (proc.stdout + proc.stderr).strip()
        run.model_requests = workspace.site.requests - before
    except subprocess.TimeoutExpired:
        sampler.stop()
        _close(door, None)
        run.findings.append(Finding("task", f"the task did not finish within {RUN_TIMEOUT:g} s"))
        return run
    except SeeressError as exc:
        sampler.stop()
        _close(door, None)
        run.findings.append(Finding("task", f"the door could not be run: {exc}"))
        return run
    run.events = egress.read_events(log)
    run.browser_contacts = sorted(sampler.seen)
    run.browser_note = sampler.note
    if run.exit_status != 0 or "DONE:" not in run.output:
        run.findings.append(
            Finding("task", f"the task did not end as intended (exit {run.exit_status})")
        )
    if door == "client":
        # The browser Seeress owns must have no way out. Both halves are checked: a
        # destination that is not exempt from the route is refused, and no connection of
        # the browser's own was seen leaving this machine.
        if "LOADED" in run.output:
            run.findings.append(
                Finding(
                    "browser",
                    "the browser Seeress owns loaded a page from an address that is not "
                    "exempt from its route",
                )
            )
        if run.browser_contacts:
            run.findings.append(
                Finding(
                    "browser",
                    "the browser Seeress owns was seen connected outside this machine: "
                    + ", ".join(run.browser_contacts),
                )
            )
    run.findings += egress.judge(run.events, endpoint_port=workspace.site.port)
    return run


def _click_on_the_local_page(address: str, site_port: int) -> None:
    """Open the local page in the browser at address and click its heading, as a mouse
    would. The harness is the one the tests drive a tab with: it is not installed with the
    package, so the repository is put on the path for it."""
    sys.path.insert(0, str(REPO))
    from tests.e2e.cdp import Browser, listened

    own = Browser(address)
    try:
        tab = own.tab(f"http://127.0.0.1:{site_port}/page")
        listened(tab)  # the capture listens in the page: the click will be heard
        tab.click("h1")
        tab.close()
    finally:
        own.close()


def run_capture(workspace: Workspace) -> Run:
    """Run the capture as the logger of the record, and judge what was recorded.

    The browser is one Seeress starts, to stand where the person's own Chrome would be: what
    it contacts is reported and not judged, as under the personal door. The capture is given
    its address, and a home of its own whose schedule says all day. While it runs, the local
    page is opened in the browser and its heading is clicked, as a mouse would. The capture
    must have reached the browser and nothing else, must have started nothing, and the record
    it wrote must hold that one click.
    """
    log = workspace.root / "capture.jsonl"
    log.write_text("", encoding="utf-8")
    home = workspace.root / "capture-home"
    home.mkdir()
    (home / config.CONFIG_FILE).write_text(CAPTURE_SETTINGS, encoding="utf-8")
    run = Run("capture", [], -1, "", 0)
    sampler = BrowserSampler([])
    stand_in: str | None = None
    port = 0
    proc: subprocess.Popen[str] | None = None
    try:
        stand_in = bodies.launch(workspace.root / "capture-browser", headless=True, route=False)
        port = int(urlsplit(stand_in).port or 0)
        run.command = [
            sys.executable,
            "-m",
            "seeress.capture",
            stand_in,
            "--home",
            str(home),
            "--seconds",
            CAPTURE_SECONDS,
        ]
        sampler = BrowserSampler([f"user-data-dir={workspace.root / 'capture-browser'}"])
        sampler.start()
        # The interpreter of this run, an address given, and a home of the capture's own.
        proc = subprocess.Popen(
            run.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                **PLANTED,
                "SEERESS_HOME": str(home),
                LOG: str(log),
            },
            cwd=REPO,
        )
        try:
            _click_on_the_local_page(stand_in, workspace.site.port)
        except AssertionError as exc:  # the harness says what it waited for and did not get
            run.findings.append(Finding("task", f"the capture did not listen in the page: {exc}"))
        out, err = proc.communicate(timeout=RUN_TIMEOUT)
        run.exit_status = proc.returncode
        run.output = (out + err).strip()
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            proc.communicate()
        run.findings.append(Finding("task", f"the capture did not stop within {RUN_TIMEOUT:g} s"))
    except (SeeressError, OSError) as exc:
        run.findings.append(Finding("task", f"the capture could not be run: {exc}"))
    finally:
        sampler.stop()
        if stand_in is not None:
            bodies.close_over_cdp(stand_in)
    run.events = egress.read_events(log)
    run.browser_contacts = sorted(sampler.seen)
    run.browser_note = sampler.note
    if not run.findings:
        if run.exit_status != 0:
            run.findings.append(
                Finding("task", f"the capture did not stop as it ran (exit {run.exit_status})")
            )
        if "1 events written; 0 outside the schedule; 0 over the cap" not in run.output:
            run.findings.append(
                Finding("task", "the capture did not record the click, and only the click")
            )
    run.findings += egress.judge(
        run.events,
        endpoint_port=port,
        seeress_process="seeress.capture",
        starts_browser_tool=False,
    )
    return run


def control(workspace: Workspace) -> list[Finding]:
    """Prove the instrument can see a connection, a child and what the child was given.

    A process makes a connection on purpose, starts a child and gives it a key on purpose.
    The connection, the child and the key must each be on the record, and the judge must
    condemn the key.
    """
    port = workspace.site.port
    log = workspace.root / "control.jsonl"
    log.write_text("", encoding="utf-8")
    code = (
        "import os, socket, subprocess, sys\n"
        f'socket.getaddrinfo("127.0.0.1", {port})\n'
        f'socket.socket().connect(("127.0.0.1", {port}))\n'
        "subprocess.run([sys.executable, '-c', 'pass'],\n"
        "             env={'PATH': '/usr/bin', 'ZAI_API_KEY': 'plant',\n"
        f"                  {LOG!r}: os.environ[{LOG!r}]}})\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, LOG: str(log)},
        check=True,
        timeout=120,
    )
    events = egress.read_events(log)
    findings: list[Finding] = []
    for kind in ("start", "resolve", "connect", "spawn"):
        if kind not in {event.kind for event in events}:
            findings.append(Finding("instrument", f"the instrument did not record a {kind}"))
    spawns = [event for event in events if event.kind == "spawn"]
    spawn = spawns[0] if spawns else None
    if spawn is None or not any(
        event.kind == "start" and event.parent == spawn.pid and event.pid != spawn.pid
        for event in events
    ):
        findings.append(
            Finding(
                "instrument",
                "the control started one child; it and the control itself should both be "
                "on the record",
            )
        )
    if spawn is None or not spawn.env or "ZAI_API_KEY" not in spawn.env:
        findings.append(
            Finding(
                "instrument",
                "the control gave a child a key on purpose; the record does not show it",
            )
        )
    judged = [
        finding
        for finding in egress.judge(events, endpoint_port=port, seeress_process="-c")
        if finding.kind != "instrument"  # a control runs no browser step
    ]
    condemned = [finding.detail for finding in judged if finding.kind == "environment"]
    if len(judged) != 1 or len(condemned) != 1 or "ZAI_API_KEY" not in condemned[0]:
        findings.append(
            Finding("instrument", f"the control record was judged unexpectedly: {judged}")
        )
    return findings


# ----------------------------------------------------------------- the report ---


def _processes(events: Sequence[Event]) -> list[tuple[str, int, int, list[str]]]:
    """Role, pid, parent and what each process started, in order of first appearance."""
    order: list[int] = []
    roles: dict[int, str] = {}
    parents: dict[int, int] = {}
    started: dict[int, list[str]] = {}
    for event in events:
        roles.setdefault(event.pid, event.role)
        parents.setdefault(event.pid, event.parent)
        started.setdefault(event.pid, [])
        if event.pid not in order:
            order.append(event.pid)
        if event.kind == "spawn" and event.program:
            started[event.pid].append(event.program)
    return [(roles[pid], pid, parents[pid], started[pid]) for pid in order]


def _environments(events: Sequence[Event]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        if event.kind != "spawn":
            continue
        name = event.program or "a child"
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "none"
    parts = []
    for name, count in counts.items():
        parts.append(f"{name} x{count}" if count > 1 else name)
    return "; ".join(parts)


def report(runs: Sequence[Run], control_findings: Sequence[Finding]) -> str:
    lines = [f"Seeress egress check, {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    lines.append("")
    if control_findings:
        lines.append("Instrument: NOT PROVEN")
        lines += [f"  {finding}" for finding in control_findings]
    else:
        lines.append(
            "Instrument: proven. A control process made a connection, started a child and "
            "gave it a key on purpose; all three were recorded, and the key was condemned."
        )
    for run in runs:
        lines.append("")
        lines.append(f"Door: {run.door}")
        lines.append(f"  command: {' '.join(run.command[2:])}")
        answered = [line for line in run.output.splitlines() if line.startswith("DONE:")]
        last = (
            answered[-1]
            if answered
            else (run.output.splitlines()[-1] if run.output else "(no output)")
        )
        lines.append(f"  exit status {run.exit_status}; {last[:160]}")
        if run.door == "capture":
            said = [line for line in run.output.splitlines() if line]
            lines.append("  model endpoint: none is asked by the capture")
            lines.append(f"  the capture says: {said[-2] if len(said) >= 2 else '(nothing)'}")
        else:
            lines.append(f"  model endpoint: {run.model_requests} requests, all to 127.0.0.1")
        lines.append("  Python processes observed:")
        for role, pid, parent, started in _processes(run.events):
            theirs = egress.contacts(event for event in run.events if event.pid == pid)
            where = ", ".join(
                f"{place} x{count}" if count > 1 else place for place, count in theirs.items()
            )
            lines.append(f"    {role} (pid {pid}, parent {parent})")
            if where:
                lines.append(f"      contacted: {where}")
            for program in started:
                lines.append(f"      started: {program}")
        lines.append(f"  children and their environments: {_environments(run.events)}")
        probed = [
            origin
            for origin in PROBE_ORIGINS
            if any(event.kind == "request" and event.origin == origin for event in run.events)
        ]
        if probed:
            lines.append(
                "  the browser tool asked "
                + ", ".join(origin.removeprefix("http://") for origin in probed)
                + " for a browser's version: the library's own look for a local browser when "
                "it starts its daemon. The answer only decides whether a cloud browser would "
                "be offered, and Seeress never allows one."
            )
        if run.door == "client":
            lines.append("  browser of Seeress's own: no route out except the client's own sites")
        if run.door == "capture":
            lines.append("  browser: a stand-in for the person's own, where the capture records")
        if run.browser_contacts:
            what = {
                "personal": "the user's browser's own, which is not Seeress's business",
                "capture": "the stand-in for the person's own browser, which is not Seeress's",
            }.get(run.door, "")
            lines.append(
                "  browser's own connections seen outside this machine"
                + (f" ({what})" if what else "")
                + ": "
                + ", ".join(run.browser_contacts)
            )
        elif run.browser_note:
            lines.append(f"  browser's own connections: {run.browser_note}")
        else:
            lines.append("  browser's own connections seen outside this machine: none")
        if run.findings:
            lines.append("  verdict: FINDINGS")
            lines += [f"    {finding}" for finding in run.findings]
        else:
            lines.append("  verdict: clean")
    return "\n".join(lines)


# --------------------------------------------------------------------- main ---


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Does Seeress contact anyone it should not?")
    parser.add_argument("--doors", default="personal,client,capture", help="which doors to run")
    parser.add_argument(
        "--personal-chrome",
        action="store_true",
        help="attach the personal door to the real Chrome instead of a throwaway browser",
    )
    parser.add_argument("--keep", action="store_true", help="keep the throwaway folder")
    parser.add_argument("--json", type=Path, default=None, help="also write the record here")
    args = parser.parse_args(argv)
    doors = [door.strip() for door in args.doors.split(",") if door.strip()]
    unknown = [door for door in doors if door not in DOORS]
    if unknown:
        parser.error(f"unknown door: {', '.join(unknown)}")
    if hook_path().exists():
        print("an egress check is already running: its hook file is present")
        return 1

    with Workspace(args.keep) as workspace:
        control_findings = control(workspace)
        runs = [
            run_capture(workspace)
            if door == "capture"
            else run_door(workspace, door, args.personal_chrome)
            for door in doors
        ]
        root = workspace.root if args.keep else None
    findings = [*control_findings, *(finding for run in runs for finding in run.findings)]
    print(report(runs, control_findings))
    print()
    if root is not None:
        print(f"Kept: {root}")
    if args.json:
        record = {
            "control": [asdict(finding) for finding in control_findings],
            "runs": [
                {
                    **asdict(run),
                    "events": [asdict(event) for event in run.events],
                }
                for run in runs
            ],
        }
        args.json.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"Record: {args.json}")
    print("RESULT: FINDINGS" if findings else "RESULT: clean on every door")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
