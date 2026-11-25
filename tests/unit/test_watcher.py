"""Characterisation of seeress.watcher as it behaved at the prototype-0.1 tag.

The logger is a spike, to be replaced. Until then these tests protect the behaviour
its measurement depends on.
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from seeress import brain, watcher
from tests.conftest import Patcher

AGENT_DIR = Path("Library/LaunchAgents")

Event = dict[str, str]


def event(kind: str, url: str, minutes_ago: float = 0) -> Event:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - minutes_ago * 60))
    return {"ts": stamp, "kind": kind, "url": url, "title": "a page", "target": "ABCD"}


def write(directory: Path, name: str, events: list[Event]) -> None:
    (directory / name).write_text("".join(json.dumps(item) + "\n" for item in events))


@pytest.fixture
def watch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "watch"
    directory.mkdir()
    monkeypatch.setattr(watcher, "WATCH_DIR", directory)
    return directory


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Example.com/path?q=1", "example.com"),
        ("https://web.telegram.org/", "web.telegram.org"),
        ("https://wikipedia.org/wiki/X", "wikipedia.org"),
        ("https://workplace.example.com/", "workplace.example.com"),
        ("https://intranet.corp/", "intranet.corp"),
        ("", ""),
        ("about:blank", ""),
        ("chrome://settings/", ""),
        ("chrome-extension://abc/popup.html", ""),
        ("devtools://devtools/bundled/x.html", ""),
        ("data:text/html,hi", ""),
        ("not-a-url", ""),
    ],
)
def test_domain_rules(url: str, expected: str) -> None:
    assert watcher._domain(url) == expected


def test_events_are_read_from_every_spike_file_and_broken_lines_are_skipped(
    watch_dir: Path,
) -> None:
    recent = [event("nav", "https://a.example.com/"), event("tab_open", "https://b.example.com/")]
    write(watch_dir, "spike-20261121.jsonl", recent)
    (watch_dir / "spike-20261124.jsonl").write_text(
        "{not json}\n" + json.dumps(event("nav", "https://c.example.com/")) + "\n"
    )

    loaded = watcher._load_events(None)

    assert [item["url"] for item in loaded] == [
        "https://a.example.com/",
        "https://b.example.com/",
        "https://c.example.com/",
    ]


def test_events_can_be_limited_to_the_last_hours(watch_dir: Path) -> None:
    write(watch_dir, "spike-20261121.jsonl", [event("nav", "https://old.example.com/", 300)])
    write(watch_dir, "spike-20261124.jsonl", [event("nav", "https://new.example.com/", 30)])

    assert [item["url"] for item in watcher._load_events(1)] == ["https://new.example.com/"]


def test_analysis_without_data_says_so(watch_dir: Path) -> None:
    assert watcher.analyze(no_llm=True) == "no watcher data yet — run `seeress watch-start` first"


def test_analysis_without_the_brain_writes_a_report(watch_dir: Path) -> None:
    events: list[Event] = []
    for _ in range(3):
        events += [event("nav", "https://a.example.com/x"), event("nav", "https://b.example.com/y")]
    write(watch_dir, "spike-20261124.jsonl", events)

    report = watcher.analyze(no_llm=True)

    assert "## Most-visited domains (visit runs)" in report
    assert "  3×  a.example.com" in report
    assert "- **3×**  a.example.com → b.example.com" in report
    saved = list(watch_dir.glob("analysis-*.md"))
    assert len(saved) == 1
    assert report.endswith(f"\n\n[saved {saved[0]}]")
    assert saved[0].read_text() == report.removesuffix(f"\n\n[saved {saved[0]}]") + "\n"


def test_analysis_asks_only_the_client_brain_and_appends_its_proposals(
    watch_dir: Path, patch: Patcher, capsys: pytest.CaptureFixture[str]
) -> None:
    write(watch_dir, "spike-20261121.jsonl", [event("nav", "https://a.example.com/")])
    asked: list[brain.Chosen] = []

    def fake_chat(endpoint: brain.Chosen, messages: object, timeout: float = 300) -> str:
        asked.append(endpoint)
        return "1. A proposal"

    patch(brain, "chat", fake_chat)

    report = watcher.analyze()

    assert asked == [
        brain.Chosen("ollama-local", "http://localhost:11434/v1", "nemotron-3.5-lightning", "local")
    ]
    assert "## Flow proposals (local brain)\n\n1. A proposal" in report
    assert "asking local brain" in capsys.readouterr().out


def test_the_report_never_goes_to_a_model_that_is_not_on_this_machine(
    home: Path, watch_dir: Path, patch: Patcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is one person's activity, so only a model on this machine may read it."""
    write(watch_dir, "spike-20261121.jsonl", [event("nav", "https://a.example.com/")])
    settings = home / ".seeress" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[[endpoints]]\nname = "tenant"\nbase_url = "https://models.tenant.example/v1"\n'
        'api_key_env = "TENANT_KEY"\n'
    )
    monkeypatch.setenv("TENANT_KEY", "t")
    asked: list[brain.Chosen] = []

    def fake_chat(endpoint: brain.Chosen, messages: object, timeout: float = 300) -> str:
        asked.append(endpoint)
        return "1. A proposal"

    patch(brain, "chat", fake_chat)

    watcher.analyze()

    assert [endpoint.name for endpoint in asked] == ["ollama-local"]


class Done:
    returncode = 0
    stderr = ""


def record_launchctl(monkeypatch: pytest.MonkeyPatch, commands: list[list[str]]) -> None:
    def fake_run(args: list[str], **kwargs: object) -> Done:
        commands.append(args)
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_start_writes_the_agent_and_loads_it(
    home: Path, watch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    record_launchctl(monkeypatch, commands)

    answer = watcher.start()

    plist = home / AGENT_DIR / "ai.seeress.spike-watch.plist"
    text = plist.read_text()
    assert "<string>-m</string><string>seeress.watcher</string>" in text
    assert "<key>KeepAlive</key><true/>" in text
    assert str(watch_dir / "logger.log") in text
    assert commands == [
        ["launchctl", "unload", str(plist)],
        ["launchctl", "load", str(plist)],
    ]
    assert answer.startswith("watcher running (LaunchAgent ai.seeress.spike-watch")


def test_a_failed_load_is_reported(
    home: Path, watch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home is asked for, though every test has one: this test runs `start()`, which
    writes a file under it. Once it did not, and rewrote the real logger's LaunchAgent file."""

    class Failed:
        returncode = 1
        stderr = "service already loaded"

    def fake_run(args: list[str], **kwargs: object) -> Failed:
        return Failed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert watcher.start() == "launchctl load failed: service already loaded"


def test_stop_unloads_and_removes_the_agent(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    plist = home / AGENT_DIR / "ai.seeress.spike-watch.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    record_launchctl(monkeypatch, commands)

    assert watcher.stop() == "watcher stopped"
    assert commands == [["launchctl", "unload", str(plist)]]
    assert not plist.exists()


def test_the_logger_s_connection_has_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy must never see what is said to this machine: every tab's address and its
    title pass on this connection. Tried on the library: with HTTP_PROXY named, a loopback
    address went to the proxy by default, and not with proxy=None."""
    import websockets

    asked: dict[str, object] = {}

    class Standing:
        """A connection to nowhere: it records how it was asked for and says nothing."""

        def __init__(self, address: str, **options: object) -> None:
            asked.update(options)

        async def __aenter__(self) -> "Standing":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send(self, message: str) -> None:
            raise RuntimeError("enough: the test asks only how the connection was asked for")

    class Ended(BaseException):
        """Ends the loop of the logger, as no error it catches would."""

    reads = iter([(9222, "/devtools/browser/one")])

    def read_port() -> tuple[int, str]:
        try:
            return next(reads)
        except StopIteration:
            raise Ended from None

    said: list[tuple[str, dict[str, object]]] = []

    class Log:
        def ev(self, kind: str, **fields: object) -> None:
            said.append((kind, fields))

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(websockets, "connect", Standing)
    monkeypatch.setattr(watcher, "_read_port", read_port)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(Ended):
        asyncio.run(watcher._ws_loop(Log()))  # type: ignore[arg-type]

    assert asked.get("proxy", "never asked") is None
    assert asked  # the connection was made, and this is how it was asked for
