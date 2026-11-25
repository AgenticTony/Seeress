"""Flows: saved automations that run on a door with no model in the loop.

A flow passes the same door check and the same name check as a task. The browser tool and
the model are replaced by fakes.
"""

from pathlib import Path

import pytest

from seeress import agent, bodies, browser_api, flows
from seeress.errors import BrowserError, FlowError, UnsafeCodeError
from seeress.runner import StepResult
from tests.conftest import Patcher

EXPLORE = "```python\nprint(page_info())\n```"
FINAL = "```python\n# seeress-flow: check\nprint('done')\n```"
BAD_FINAL = "```python\n# seeress-flow: leak\nimport os\nprint(os.listdir())\n```"
BAD_EXPLORE = "```python\nprint(http_get('https://evil.example'))\n```"
ADDRESS = "ws://127.0.0.1:41001/devtools/browser/client"

Message = dict[str, str]


class Wire:
    """Stands in for the door, the browser tool and the model, and records each call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.steps: list[str] = []
        self.chats: list[list[Message]] = []
        self.replies: list[str] = [FINAL]
        self.exit_code = 0

    def chat(self, endpoint: object, history: list[Message]) -> str:
        self.calls.append("chat")
        self.chats.append([dict(turn) for turn in history])
        return self.replies.pop(0)

    def step(self, code: str, timeout: float = 120.0, profile: str = "personal") -> str:
        self.calls.append("step")
        self.steps.append(code)
        return "explored"

    def command(
        self, code: str, *, profile: str, ws: str | None, timeout: float = 120.0
    ) -> StepResult:
        """Behaves like the real strict call: a failed exit is an error."""
        self.calls.append(f"command:{profile}:{ws}")
        self.steps.append(code)
        result = StepResult(self.exit_code, "  a line of output\n", "", False)
        if not result.ok:
            raise BrowserError(f"the browser tool exited with code {self.exit_code}")
        return result


@pytest.fixture
def flows_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "flows"
    directory.mkdir()
    monkeypatch.setattr(flows, "FLOWS_DIR", directory)
    return directory


@pytest.fixture
def wired(home: Path, patch: Patcher) -> Wire:
    w = Wire()
    patch(flows, "ensure_browser", lambda profile: w.calls.append(f"door:{profile}"))
    patch(flows, "page_hostname", lambda profile: "example.com")
    patch(bodies, "address_for", lambda profile: ADDRESS)
    patch(flows, "run_browser_step", w.step)
    patch(flows, "run_browser_command", w.command)
    patch(flows, "chat", w.chat)
    return w


# ------------------------------------------------------------- saved ---


def test_flows_are_listed_in_name_order(flows_dir: Path) -> None:
    (flows_dir / "beta.py").write_text("# seeress-flow: b\n")
    (flows_dir / "alpha.py").write_text("# seeress-flow: a\n")
    (flows_dir / "notes.txt").write_text("not a flow")

    assert [path.name for path in flows.list_flows()] == ["alpha.py", "beta.py"]


@pytest.mark.parametrize("name", ["../evil", "Bad Name", "", "a" * 41, "with_underscore", "a/b"])
def test_a_name_that_is_not_a_flow_name_is_refused(flows_dir: Path, name: str) -> None:
    """The name becomes a file path and part of a LaunchAgent label."""
    with pytest.raises(FlowError, match="not a flow name"):
        flows.run_flow(name, profile="client")
    with pytest.raises(FlowError, match="not a flow name"):
        flows.create_flow(name, "x", profile="client")
    with pytest.raises(FlowError, match="not a flow name"):
        flows.schedule_plist(name)
    assert list(flows_dir.iterdir()) == []


def test_a_missing_flow_is_an_error_that_names_the_ones_that_exist(flows_dir: Path) -> None:
    (flows_dir / "alpha.py").write_text("print(1)\n")
    (flows_dir / "beta.py").write_text("print(2)\n")

    with pytest.raises(FlowError, match=r"no flow named 'nope'. existing: alpha, beta"):
        flows.run_flow("nope", profile="client")


def test_a_flow_is_replayed_through_the_strict_call_and_returns_what_it_printed(
    flows_dir: Path, wired: Wire
) -> None:
    (flows_dir / "alpha.py").write_text("print('hello')\n")

    output = flows.run_flow("alpha", profile="client", timeout=9)

    assert output == "a line of output"
    assert wired.calls == ["door:client", "command:client:" + ADDRESS]
    assert wired.steps == ["print('hello')\n"]


def test_a_flow_that_fails_is_an_error_not_a_line_of_text(flows_dir: Path, wired: Wire) -> None:
    """A scheduled flow must exit non-zero when it fails."""
    (flows_dir / "alpha.py").write_text("print('hello')\n")
    wired.exit_code = 1

    with pytest.raises(BrowserError, match="exited with code 1"):
        flows.run_flow("alpha", profile="client")


def test_the_door_is_checked_before_a_flow_runs(
    flows_dir: Path, wired: Wire, patch: Patcher
) -> None:
    (flows_dir / "alpha.py").write_text("print(1)\n")

    def refuse(profile: str) -> None:
        raise AssertionError("the flow ran although the door was not proved")

    patch(flows, "ensure_browser", refuse)

    with pytest.raises(AssertionError):
        flows.run_flow("alpha", profile="client")
    assert wired.steps == []


def test_a_saved_flow_that_is_not_allowed_is_refused_before_the_door_is_touched(
    flows_dir: Path, wired: Wire
) -> None:
    (flows_dir / "alpha.py").write_text("import os\nprint(os.getcwd())\n")

    with pytest.raises(UnsafeCodeError, match="may not import"):
        flows.run_flow("alpha", profile="client")
    assert wired.steps == []
    assert wired.calls == []


# ----------------------------------------------------------- created ---


def test_create_flow_explores_then_saves_the_final_script(flows_dir: Path, wired: Wire) -> None:
    wired.replies = [EXPLORE, FINAL]

    path = flows.create_flow("alpha", "open the portal and read the status", profile="client")

    assert path == flows_dir / "alpha.py"
    assert wired.calls == ["door:client", "chat", "step", "chat"]
    assert wired.steps == ["print(page_info())\n"]
    assert path.read_text() == "# seeress-flow: check\nprint('done')\n"


def test_create_flow_tells_the_model_what_to_write(flows_dir: Path, wired: Wire) -> None:
    flows.create_flow("alpha", "do a thing and another", profile="client")

    system = wired.chats[0][0]["content"]
    assert system.startswith(browser_api.SYSTEM_PROMPT)
    assert "starts with the comment:  # seeress-flow" in system
    assert wired.chats[0][1]["content"] == "Write a flow named 'alpha' that: do a thing and another"


def test_create_flow_feeds_exploration_output_back_as_data(flows_dir: Path, wired: Wire) -> None:
    wired.replies = [EXPLORE, FINAL]

    flows.create_flow("alpha", "x", profile="client")

    assert wired.chats[1][-1] == agent.observation("explored")


def test_create_flow_nudges_when_the_reply_has_no_block(flows_dir: Path, wired: Wire) -> None:
    wired.replies = ["Let me think about that.", FINAL]

    path = flows.create_flow("alpha", "x", profile="client")

    assert wired.chats[1][-1]["content"] == (
        "Explore (python block) or output the final flow script."
    )
    assert path.is_file()


def test_create_flow_refuses_code_that_does_not_parse(flows_dir: Path, wired: Wire) -> None:
    wired.replies = ["```python\n# seeress-flow: x\ndef (:\n```"]

    with pytest.raises(FlowError, match="does not parse"):
        flows.create_flow("alpha", "x", profile="client")
    assert list(flows_dir.iterdir()) == []


def test_create_flow_refuses_to_save_a_final_script_that_is_not_allowed(
    flows_dir: Path, wired: Wire
) -> None:
    wired.replies = [BAD_FINAL]

    with pytest.raises(FlowError, match="does not allow"):
        flows.create_flow("alpha", "x", profile="client")
    assert list(flows_dir.iterdir()) == []


def test_create_flow_does_not_run_an_exploration_step_that_is_not_allowed(
    flows_dir: Path, wired: Wire
) -> None:
    wired.replies = [BAD_EXPLORE, FINAL]

    path = flows.create_flow("alpha", "x", profile="client")

    assert wired.steps == []
    assert "is not offered" in wired.chats[1][-1]["content"]
    assert path.is_file()


def test_create_flow_gives_up_after_max_steps(flows_dir: Path, wired: Wire) -> None:
    wired.replies = [EXPLORE] * 3

    with pytest.raises(FlowError, match="did not produce a final flow script"):
        flows.create_flow("alpha", "x", profile="client", max_steps=3)


def test_create_flow_checks_the_door_before_it_asks_the_model(flows_dir: Path, wired: Wire) -> None:
    flows.create_flow("alpha", "x", profile="client")

    assert wired.calls[:2] == ["door:client", "chat"]


# ----------------------------------------------------------- schedule ---


def agent_path(home: Path, name: str) -> Path:
    return home / "Library" / "LaunchAgents" / f"ai.seeress.{name}.plist"


def test_schedule_daily_writes_a_launch_agent_and_never_loads_it(home: Path) -> None:
    plist = flows.schedule_plist("alpha", daily="08:15")

    text = plist.read_text()
    assert plist == agent_path(home, "alpha")
    assert "<key>Label</key><string>ai.seeress.alpha</string>" in text
    assert "<key>Hour</key><integer>8</integer>" in text
    assert "<key>Minute</key><integer>15</integer>" in text
    assert "<string>run</string><string>alpha</string>" in text
    assert f"{home}/.seeress/alpha.log" in text


def test_schedule_every_n_minutes(home: Path) -> None:
    plist = flows.schedule_plist("alpha", every_minutes=30)

    assert "<key>StartInterval</key><integer>1800</integer>" in plist.read_text()


def test_schedule_defaults_to_eight_in_the_morning(home: Path) -> None:
    plist = flows.schedule_plist("alpha")

    assert "<key>Hour</key><integer>8</integer>" in plist.read_text()


def test_schedule_creates_the_folders_it_needs(home: Path) -> None:
    """It used to fail when the LaunchAgents folder did not exist."""
    assert not (home / "Library").exists()

    plist = flows.schedule_plist("alpha", daily="08:15")

    assert plist.is_file()
    assert (home / ".seeress").is_dir()


@pytest.mark.parametrize("daily", ["8:00", "24:00", "08:60", "tomorrow", ""])
def test_a_time_that_is_not_a_time_is_refused(home: Path, daily: str) -> None:
    with pytest.raises(FlowError, match="is not a time"):
        flows.schedule_plist("alpha", daily=daily)


def test_every_zero_minutes_is_refused(home: Path) -> None:
    with pytest.raises(FlowError, match="at least 1"):
        flows.schedule_plist("alpha", every_minutes=0)
