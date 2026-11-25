"""Flows: saved automations that run on a door with no model in the loop.

A flow passes the same door check and the same name check as a task. The browser tool and
the model are replaced by fakes.
"""

import json
from pathlib import Path

import pytest

from seeress import activity, agent, bodies, browser_api, config, flows, proposals
from seeress.errors import BrowserError, FlowError, MiningError, UnsafeCodeError
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
        self.runs: list[tuple[str, str | None]] = []

    def chat(self, endpoint: object, history: list[Message]) -> str:
        self.calls.append("chat")
        self.chats.append([dict(turn) for turn in history])
        return self.replies.pop(0)

    def step(self, code: str, timeout: float = 120.0, profile: str = "personal") -> str:
        self.calls.append("step")
        self.steps.append(code)
        return "explored"

    def command(
        self,
        code: str,
        *,
        profile: str,
        ws: str | None,
        timeout: float = 120.0,
        what: str = "probe",
        name: str | None = None,
    ) -> StepResult:
        """Behaves like the real strict call: a failed exit is an error."""
        self.calls.append(f"command:{profile}:{ws}")
        self.steps.append(code)
        self.runs.append((what, name))
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


# ------------------------------------------------------- noting the use ---


def noted(file: str) -> list[dict[str, object]]:
    """The runs noted in a file of the record under the Seeress home of the test."""
    path = config.seeress_home() / "watch" / file
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_flow_that_ran_tells_the_tool_what_run_it_was(flows_dir: Path, wired: Wire) -> None:
    (flows_dir / "alpha.py").write_text("print('hello')\n")

    flows.run_flow("alpha", profile="client")

    assert wired.runs == [("flow", "alpha")]


def test_a_flow_that_ran_well_is_noted_as_such(flows_dir: Path, wired: Wire) -> None:
    (flows_dir / "alpha.py").write_text("print('hello')\n")

    flows.run_flow("alpha", profile="client")

    (run,) = noted("flow-runs.jsonl")
    assert list(run) == ["ts", "name", "exit"]
    assert (run["name"], run["exit"]) == ("alpha", 0)
    assert isinstance(run["ts"], str) and activity.TS_SHAPE.fullmatch(run["ts"])


def test_a_flow_that_failed_is_noted_as_failed_and_the_error_goes_on(
    flows_dir: Path, wired: Wire
) -> None:
    (flows_dir / "alpha.py").write_text("print('hello')\n")
    wired.exit_code = 1

    with pytest.raises(BrowserError, match="exited with code 1"):
        flows.run_flow("alpha", profile="client")

    assert noted("flow-runs.jsonl") == [
        {
            "ts": noted("flow-runs.jsonl")[0]["ts"],
            "name": "alpha",
            "exit": 1,
            "error": "BrowserError",
        }
    ]


def test_a_flow_that_is_not_allowed_is_noted_as_failed_before_it_ran(
    flows_dir: Path, wired: Wire
) -> None:
    (flows_dir / "alpha.py").write_text("import os\nprint(os.getcwd())\n")

    with pytest.raises(UnsafeCodeError):
        flows.run_flow("alpha", profile="client")

    (run,) = noted("flow-runs.jsonl")
    assert (run["name"], run["exit"], run["error"]) == ("alpha", 1, "UnsafeCodeError")
    assert wired.steps == []


def test_a_flow_that_is_not_there_is_no_run(flows_dir: Path, wired: Wire) -> None:
    with pytest.raises(FlowError, match="no flow named"):
        flows.run_flow("alpha", profile="client")

    assert noted("flow-runs.jsonl") == []


def test_when_the_note_cannot_be_written_the_flow_runs_and_its_result_stands(
    flows_dir: Path, wired: Wire
) -> None:
    """A note must never break the run it notes."""
    home = config.seeress_home()
    home.mkdir(parents=True)
    (home / "watch").write_text("not a folder")
    (flows_dir / "alpha.py").write_text("print('hello')\n")

    assert flows.run_flow("alpha", profile="client") == "a line of output"


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


# ---------------------------------------------------- compiling a proposal ---

NAME = proposals.name_for(["a.example", "b.example", "c.example"])


def a_proposal(**changes: object) -> proposals.Proposal:
    """A proposal with its evidence invented, named after the sites a, b and c."""
    fields: dict[str, object] = {
        "name": NAME,
        "goal": "",
        "sites": ("a.example", "b.example", "c.example"),
        "order": "fixed",
        "days": 3,
        "sessions": {"2026-11-16": 1, "2026-11-18": 1, "2026-11-20": 1},
        "first_seen": "2026-11-16T09:00:00",
        "last_seen": "2026-11-20T09:02:00",
        "hour": 9,
        "confidence": None,
        "status": "proposed",
    }
    fields.update(changes)
    return proposals.Proposal(**fields)  # type: ignore[arg-type]


def remembered() -> list[proposals.Proposal]:
    """Every line of the proposal's file that reads."""
    file = config.seeress_home() / "proposals" / f"{NAME}.jsonl"
    text = file.read_text(encoding="utf-8")
    return [proposals.Proposal.parse(json.loads(line)) for line in text.splitlines()]


def asked(wired: Wire) -> str:
    """The one thing the compiler was asked."""
    (turn,) = [turn for turn in wired.chats[0] if turn["role"] == "user"]
    return turn["content"]


def test_compile_refuses_a_proposal_that_is_only_proposed_and_names_the_command(
    flows_dir: Path, wired: Wire
) -> None:
    """Nothing is compiled from a model's sentence."""
    proposals.remember(config.seeress_home(), a_proposal(goal="A model's sentence."))

    with pytest.raises(MiningError, match=f"seeress proposal goal {NAME}"):
        flows.compile_proposal(config.seeress_home(), NAME)

    assert wired.calls == []
    assert list(flows_dir.iterdir()) == []


def test_compile_refuses_a_dismissed_proposal_and_says_how_to_goal_it_again(
    flows_dir: Path, wired: Wire
) -> None:
    proposals.remember(
        config.seeress_home(), a_proposal(status="dismissed", goal="The user's words.")
    )

    with pytest.raises(MiningError, match="dismissed") as caught:
        flows.compile_proposal(config.seeress_home(), NAME)

    assert f"seeress proposal goal {NAME}" in str(caught.value)
    assert wired.calls == []


def test_compile_refuses_an_approved_proposal_whose_goal_is_empty(
    flows_dir: Path, wired: Wire
) -> None:
    proposals.remember(config.seeress_home(), a_proposal(status="approved", goal=""))

    with pytest.raises(MiningError, match="goal"):
        flows.compile_proposal(config.seeress_home(), NAME)

    assert wired.calls == []


def test_compile_of_a_proposal_that_is_not_there_refuses(flows_dir: Path, wired: Wire) -> None:
    with pytest.raises(MiningError, match="no proposal"):
        flows.compile_proposal(config.seeress_home(), NAME)

    assert wired.calls == []


def test_compile_writes_the_flow_on_the_personal_door_from_the_goal_and_the_sites(
    flows_dir: Path, wired: Wire
) -> None:
    proposals.remember(
        config.seeress_home(),
        a_proposal(status="approved", goal="Check the invoices every morning."),
    )
    wired.replies = [FINAL]

    path = flows.compile_proposal(config.seeress_home(), NAME)

    assert path == flows_dir / f"{NAME}.py"
    assert wired.calls[0] == "door:personal"
    assert asked(wired) == (
        f"Write a flow named {NAME!r} that: Check the invoices every morning. "
        "The task visits these sites, in this order: a.example → b.example → c.example."
    )
    assert remembered()[-1].status == "compiled"


def test_the_description_of_a_proposal_whose_order_varies_lists_its_sites(
    flows_dir: Path, wired: Wire
) -> None:
    proposals.remember(
        config.seeress_home(),
        a_proposal(status="approved", goal="Check.", order="varies"),
    )
    wired.replies = [FINAL]

    flows.compile_proposal(config.seeress_home(), NAME)

    assert asked(wired) == (
        f"Write a flow named {NAME!r} that: Check. "
        "The task uses these sites: a.example, b.example, c.example."
    )


def test_the_description_is_the_goal_and_one_sentence_and_nothing_else() -> None:
    description = flows.description_for(a_proposal(goal="Check."))

    assert description == (
        "Check. The task visits these sites, in this order: a.example → b.example → c.example."
    )
    assert "2026" not in description
    assert "hour" not in description


def test_a_compiled_proposal_is_compiled_again_when_asked(flows_dir: Path, wired: Wire) -> None:
    proposals.remember(config.seeress_home(), a_proposal(status="compiled", goal="Check."))
    wired.replies = [FINAL]

    flows.compile_proposal(config.seeress_home(), NAME)

    assert (flows_dir / f"{NAME}.py").is_file()
    assert remembered()[-1].status == "compiled"


def test_when_the_flow_cannot_be_written_the_proposal_stands_and_the_error_goes_on(
    flows_dir: Path, wired: Wire
) -> None:
    proposals.remember(config.seeress_home(), a_proposal(status="approved", goal="Check."))
    wired.replies = [BAD_FINAL]

    with pytest.raises(FlowError, match="does not allow"):
        flows.compile_proposal(config.seeress_home(), NAME)

    assert remembered()[-1].status == "approved"
    assert list(flows_dir.iterdir()) == []
