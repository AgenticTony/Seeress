"""The command line: thin, and it turns a SeeressError into a reason and exit status 1."""

import logging
import signal
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from seeress import bodies, cli, config, flows, mine, watcher
from seeress.errors import (
    BrowserError,
    CaptureError,
    FlowError,
    GuardViolation,
    IsolationError,
    UnsafeCodeError,
)
from tests.conftest import Patcher

ERRORS = [
    IsolationError("the browser behind the client door answers to daemon 'default'"),
    GuardViolation("No permitted endpoint"),
    UnsafeCodeError("line 1: a step may not import anything"),
    BrowserError("the browser tool exited with code 1"),
    FlowError("no flow named 'nope'"),
    CaptureError("This browser is Seeress's own: it carries the marker."),
]


def test_no_arguments_opens_chat_on_the_personal_door(patch: Patcher) -> None:
    seen: list[tuple[object, ...]] = []

    def session(
        profile: str, companion: object = None, read: object = None, write: object = None
    ) -> None:
        seen.append((profile, companion, callable(read), callable(write)))

    patch(cli, "chat_session", session)

    assert cli.main([]) == 0
    assert seen == [("personal", None, True, True)]


def test_chat_takes_a_companion_file(patch: Patcher) -> None:
    seen: list[object] = []
    patch(
        cli,
        "chat_session",
        lambda profile, companion=None, read=None, write=None: seen.append(companion),
    )

    cli.main(["chat", "--companion", "companions/researcher.md"])

    assert seen == [Path("companions/researcher.md")]


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "alpha", "--profile", "client"],
        ["run", "--profile", "client", "alpha"],
        ["--profile", "client", "run", "alpha"],
        ["--profile=client", "run", "alpha"],
    ],
)
def test_the_door_can_be_chosen_before_or_after_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Each of these used to fail or to run a task called "run"."""
    seen: list[tuple[str, str]] = []
    monkeypatch.delenv("SEERESS_PROFILE", raising=False)

    def run_flow(name: str, profile: str) -> str:
        seen.append((name, profile))
        return "ran"

    monkeypatch.setattr(flows, "run_flow", run_flow)

    assert cli.main(argv) == 0
    assert seen == [("alpha", "client")]
    assert capsys.readouterr().out == "ran\n"


def test_the_flag_beats_the_environment_and_the_environment_beats_the_default(
    monkeypatch: pytest.MonkeyPatch, patch: Patcher
) -> None:
    doors: list[str] = []
    patch(
        cli,
        "chat_session",
        lambda profile, companion=None, read=None, write=None: doors.append(profile),
    )

    monkeypatch.setenv("SEERESS_PROFILE", "client")
    cli.main(["chat"])
    cli.main(["chat", "--profile", "personal"])
    monkeypatch.delenv("SEERESS_PROFILE")
    cli.main(["chat"])

    assert doors == ["client", "personal", "personal"]


def test_a_task_can_be_given_in_plain_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[object, ...]] = []

    def run_task(task: str, profile: str, max_steps: int) -> str:
        seen.append((task, profile, max_steps))
        return "DONE: ok"

    monkeypatch.setattr(cli, "run_task", run_task)

    assert cli.main(["--profile", "client", "--max-steps", "5", "do", "the", "thing"]) == 0
    assert seen == [("do the thing", "client", 5)]
    assert capsys.readouterr().out == "DONE: ok\n"


def test_create_flow_prints_where_it_saved_the_flow_and_how_to_run_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = Path("/flows/alpha.py")

    def create_flow(name: str, description: str, profile: str) -> Path:
        assert (name, description, profile) == ("alpha", "open the portal", "client")
        return saved

    monkeypatch.setattr(flows, "create_flow", create_flow)

    assert cli.main(["create-flow", "alpha", "open", "the", "portal", "--profile", "client"]) == 0
    assert (
        capsys.readouterr().out == f"flow saved: {saved}\nrun it:  bin/seeress-client run alpha\n"
    )


def test_flows_lists_each_flow_with_its_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    alpha = tmp_path / "alpha.py"
    alpha.write_text("# seeress-flow: open the portal\nprint(1)\n")

    def list_flows() -> list[Path]:
        return [alpha]

    monkeypatch.setattr(flows, "list_flows", list_flows)

    assert cli.main(["flows"]) == 0
    assert capsys.readouterr().out == f"{'alpha':24} seeress-flow: open the portal\n"


def test_flows_says_so_when_there_are_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(flows, "list_flows", list)

    cli.main(["flows"])

    assert "no flows yet" in capsys.readouterr().out


def test_schedule_prints_the_path_and_says_it_is_not_loaded_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plist = tmp_path / "ai.seeress.alpha.plist"
    seen: list[tuple[object, ...]] = []

    def schedule_plist(name: str, every_minutes: int | None, daily: str | None) -> Path:
        seen.append((name, every_minutes, daily))
        return plist

    monkeypatch.setattr(flows, "schedule_plist", schedule_plist)

    assert cli.main(["schedule", "alpha", "--every", "30"]) == 0
    assert seen == [("alpha", 30, None)]
    out = capsys.readouterr().out
    assert f"saved {plist}" in out
    assert "not loaded" in out


def test_reload_restarts_the_daemon_of_the_door(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reloaded: list[tuple[str, object]] = []

    def reload_daemon(profile: str, address: str | None) -> bool:
        reloaded.append((profile, address))
        return True

    monkeypatch.setattr(bodies, "reload_daemon", reload_daemon)

    assert cli.main(["reload", "--profile", "client"]) == 0
    assert reloaded == [("client", None)]
    assert "client door was stopped" in capsys.readouterr().out


def test_a_reload_that_fails_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bodies, "reload_daemon", lambda profile, address: False)

    assert cli.main(["reload"]) == 1
    assert "could not be stopped" in capsys.readouterr().err


def test_the_watch_commands_call_the_logger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[object] = []

    def start() -> str:
        seen.append("start")
        return "started"

    def stop() -> str:
        seen.append("stop")
        return "stopped"

    def analyze(hours: float | None = None, no_llm: bool = False) -> str:
        seen.append((hours, no_llm))
        return "report"

    monkeypatch.setattr(watcher, "start", start)
    monkeypatch.setattr(watcher, "stop", stop)
    monkeypatch.setattr(watcher, "analyze", analyze)

    assert cli.main(["watch-start"]) == 0
    assert cli.main(["watch-stop"]) == 0
    assert cli.main(["watch-analyze", "--hours", "6"]) == 0
    assert cli.main(["watch-analyze", "--no-llm"]) == 0

    assert seen == ["start", "stop", (6.0, False), (None, True)]
    assert capsys.readouterr().out == "started\nstopped\nreport\nreport\n"


def test_mine_exits_one_and_names_the_folder_when_nothing_is_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No record under the throwaway home of the test, and so no model is ever asked."""
    assert cli.main(["mine", "--no-llm"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "seeress error: " in captured.err
    assert "watch" in captured.err


def test_mine_passes_on_its_hours_and_whether_to_ask_a_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[float | None, bool, Path | None]] = []

    def run(hours: float | None = None, *, no_llm: bool = False, home: Path | None = None) -> str:
        seen.append((hours, no_llm, home))
        return "report"

    monkeypatch.setattr(mine, "run", run)

    assert cli.main(["mine", "--hours", "6"]) == 0
    assert cli.main(["mine", "--no-llm"]) == 0

    assert [(hours, no_llm) for hours, no_llm, _ in seen] == [(6.0, False), (None, True)]
    assert all(home is None for _, _, home in seen)
    assert capsys.readouterr().out == "report\nreport\n"


def test_the_capture_commands_reach_the_work_and_print_what_it_returns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from seeress import capture

    seen: list[tuple[object, ...]] = []

    def review(home: Path | None = None) -> str:
        seen.append(("review", home))
        return "what the record holds"

    def forget(home: Path | None, before: str, *, yes: bool = False) -> str:
        seen.append(("forget", home, before, yes))
        return "what was forgotten"

    def status(home: Path | None = None) -> str:
        seen.append(("status", home))
        return "whether it is recording"

    monkeypatch.setattr(capture, "review", review)
    monkeypatch.setattr(capture, "forget", forget)
    monkeypatch.setattr(capture, "status", status)

    assert cli.main(["capture-review"]) == 0
    assert cli.main(["capture-forget", "--before", "2026-10-10"]) == 0
    assert cli.main(["capture-forget", "--yes", "--before", "2026-10-10"]) == 0
    assert cli.main(["capture-status"]) == 0

    assert seen == [
        ("review", None),
        ("forget", None, "2026-10-10", False),
        ("forget", None, "2026-10-10", True),
        ("status", None),
    ]
    assert capsys.readouterr().out == (
        "what the record holds\nwhat was forgotten\nwhat was forgotten\nwhether it is recording\n"
    )


def test_a_day_that_is_no_day_is_refused_by_the_command_line_with_the_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The throwaway home of the test is read, and nothing is deleted: the day is no day."""

    assert cli.main(["capture-forget", "--before", "10/10/2026", "--yes"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("seeress error: ")


def test_capture_forget_needs_a_day(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["capture-forget"])

    assert exit_info.value.code == 2
    assert "--before" in capsys.readouterr().err


def test_the_help_of_the_whole_tool_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert command in out


def test_the_proposal_commands_and_progress_reach_the_work_and_print_what_it_returns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[object, ...]] = []

    def show(name: str, home: Path | None = None) -> str:
        seen.append(("show", name, home))
        return "shown"

    def set_goal(name: str, goal: str, home: Path | None = None) -> str:
        seen.append(("goal", name, goal, home))
        return "saved"

    def dismiss(name: str, home: Path | None = None) -> str:
        seen.append(("dismiss", name, home))
        return "dismissed"

    def progress(home: Path | None = None) -> str:
        seen.append(("progress", home))
        return "the progress"

    def compile_proposal(home: Path | None, name: str) -> Path:
        seen.append(("compile", home, name))
        return Path("/flows/check.py")

    monkeypatch.setattr(mine, "show", show)
    monkeypatch.setattr(mine, "set_goal", set_goal)
    monkeypatch.setattr(mine, "dismiss", dismiss)
    monkeypatch.setattr(mine, "progress", progress)
    monkeypatch.setattr(flows, "compile_proposal", compile_proposal)

    assert cli.main(["proposal", "show", "check"]) == 0
    assert cli.main(["proposal", "goal", "check", "in your own words"]) == 0
    assert cli.main(["proposal", "dismiss", "check"]) == 0
    assert cli.main(["proposal", "compile", "check"]) == 0
    assert cli.main(["progress"]) == 0

    assert seen == [
        ("show", "check", None),
        ("goal", "check", "in your own words", None),
        ("dismiss", "check", None),
        ("compile", None, "check"),
        ("progress", None),
    ]
    assert capsys.readouterr().out == (
        "shown\nsaved\ndismissed\n/flows/check.py\nRun it with: seeress run check\nthe progress\n"
    )


def test_a_proposal_command_that_refuses_exits_one_with_the_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing is remembered under the throwaway home of the test."""
    assert cli.main(["proposal", "show", "nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "seeress error: " in captured.err
    assert "no proposal" in captured.err


def test_a_goal_with_no_words_on_the_command_line_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["proposal", "goal", "check"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "goal" in captured.err


def test_an_action_of_proposal_that_does_not_exist_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["proposal", "approve", "check"])

    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("error", ERRORS, ids=lambda error: type(error).__name__)
@pytest.mark.parametrize(
    "argv", [["run", "alpha"], ["create-flow", "a", "b"], ["schedule", "a"], ["do", "a", "thing"]]
)
def test_every_command_exits_one_and_prints_the_reason_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    argv: list[str],
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise error

    for name in ("run_flow", "create_flow", "schedule_plist"):
        monkeypatch.setattr(flows, name, boom)
    monkeypatch.setattr(cli, "run_task", boom)

    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"seeress error: {error}\n"


def test_a_chat_that_cannot_prove_its_door_exits_one(
    patch: Patcher, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(
        profile: str, companion: object = None, read: object = None, write: object = None
    ) -> None:
        raise IsolationError("the browser carries a marker Seeress did not issue")

    patch(cli, "chat_session", refuse)

    assert cli.main(["chat"]) == 1
    assert "did not issue" in capsys.readouterr().err


def test_an_unknown_door_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--profile", "guest", "do", "a", "thing"])

    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_a_command_without_its_argument_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run"])

    assert exit_info.value.code == 2


def test_help_works_for_the_whole_tool_and_for_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "--profile" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--help"])
    assert exit_info.value.code == 0
    assert "--profile" in capsys.readouterr().out


def test_progress_is_logged_to_stderr_not_mixed_into_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # main() configures logging once per process; give it a fresh stream for this test
    logging.getLogger("seeress").handlers.clear()

    def run_task(task: str, profile: str, max_steps: int) -> str:
        logging.getLogger("seeress.agent").info("step 1: print(page_info())")
        return "DONE: ok"

    monkeypatch.setattr(cli, "run_task", run_task)

    assert cli.main(["do", "a", "thing"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "DONE: ok\n"
    assert "step 1: print(page_info())" in captured.err


def test_the_default_door_is_personal_and_there_are_two() -> None:
    assert config.DEFAULT_PROFILE == "personal"
    assert tuple(config.PROFILES) == ("personal", "client")


@pytest.mark.parametrize("launcher", ["bin/seeress-personal", "bin/seeress-client"])
def test_each_door_has_a_launcher_script(launcher: str) -> None:
    assert (Path(__file__).resolve().parents[2] / launcher).is_file()


def test_a_signal_to_end_the_process_ends_it_cleanly(
    patch: Patcher, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run in hand is closed and noted, as when an error ends it. Closing the terminal
    sends such a signal, and before this it ended the process without a note."""
    history: list[tuple[int, int | Callable[..., object]]] = []
    undone: list[str] = []

    def fake_set(signum: int, handler: int | Callable[..., object]) -> int | Callable[..., object]:
        history.append((signum, handler))
        return signal.SIG_DFL

    def in_hand(argv: Sequence[str]) -> int:
        try:
            installed = dict(history)[signal.SIGTERM]
            assert callable(installed)
            installed(signal.SIGTERM, None)
        finally:
            undone.append("closed")
        return 0

    patch(signal, "signal", fake_set)
    patch(cli, "COMMANDS", {"in-hand": in_hand})

    assert cli.main(["in-hand"]) == 1

    assert undone == ["closed"]
    assert "stopped" in capsys.readouterr().err
    assert history[-2:] == [
        (signal.SIGTERM, signal.SIG_DFL),
        (signal.SIGHUP, signal.SIG_DFL),
    ]


def test_the_command_line_is_installed_as_seeress() -> None:
    """The help and every hint name `seeress`. Until 2026-11-25 there was no such command
    anywhere: the only ways in were the two doors and `python -m seeress.cli`."""
    pyproject = Path(cli.__file__).resolve().parents[1] / "pyproject.toml"

    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    assert project.get("scripts") == {"seeress": "seeress.cli:main"}
