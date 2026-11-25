"""The command line: thin, and it turns a SeeressError into a reason and exit status 1."""

import logging
from pathlib import Path

import pytest

from seeress import bodies, cli, config, flows, mine, watcher
from seeress.errors import BrowserError, FlowError, GuardViolation, IsolationError, UnsafeCodeError
from tests.conftest import Patcher

ERRORS = [
    IsolationError("the browser behind the client door answers to daemon 'default'"),
    GuardViolation("No permitted endpoint"),
    UnsafeCodeError("line 1: a step may not import anything"),
    BrowserError("the browser tool exited with code 1"),
    FlowError("no flow named 'nope'"),
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


def test_the_help_of_the_whole_tool_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert command in out


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
