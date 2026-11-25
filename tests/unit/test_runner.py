"""The browser runner: what Seeress and the model see from one step.

Each test replaces the call that starts the browser tool. Nothing here starts a process.
"""

import subprocess
from typing import Any

import pytest

from seeress import config, runner
from seeress.errors import BrowserError


class Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def use(
    monkeypatch: pytest.MonkeyPatch,
    result: Proc | subprocess.TimeoutExpired | OSError,
) -> dict[str, object]:
    seen: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> Proc:
        seen["args"] = args
        seen.update(kwargs)
        if isinstance(result, OSError):
            raise result
        if isinstance(result, subprocess.TimeoutExpired):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


# ------------------------------------------------------- strict command ---


def test_a_clean_step_returns_exit_code_output_and_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, Proc(0, "  the answer\n", "a warning on stderr"))

    result = runner.run_browser_command(
        "print(1)", profile="client", timeout=5, ws="ws://127.0.0.1:9/devtools/browser/x"
    )

    assert result.ok
    assert (result.returncode, result.stdout, result.stderr, result.timed_out) == (
        0,
        "  the answer\n",
        "a warning on stderr",
        False,
    )


def test_the_tool_is_called_by_its_full_path_with_the_code_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = use(monkeypatch, Proc())
    monkeypatch.setenv("SOME_SECRET", "must not reach the tool")

    runner.run_browser_command("print(1)", profile="client", timeout=7, ws="ws://127.0.0.1:9/x")

    assert seen["args"] == [config.BU_CLI]
    assert seen["input"] == "print(1)"
    assert seen["timeout"] == 7
    assert seen["text"] is True
    assert seen["capture_output"] is True
    child = seen["env"]
    assert isinstance(child, dict)
    assert "SOME_SECRET" not in child
    assert child["BU_CDP_WS"] == "ws://127.0.0.1:9/x"
    assert child["BU_NAME"] == "seeress-client"
    assert child["ANONYMIZED_TELEMETRY"] == "false"
    assert "BH_HOME" in child


def test_the_browser_address_is_always_given_and_never_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that could forget it would let the tool find a browser of its own."""
    import inspect

    for call in (runner.run_browser_attempt, runner.run_browser_command, runner.run_browser_tool):
        parameter = inspect.signature(call).parameters["ws"]
        assert parameter.default is inspect.Parameter.empty


def test_any_other_call_of_the_tool_gets_the_same_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = use(monkeypatch, Proc())
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")

    runner.run_browser_tool(["--reload"], profile="personal", ws=None, timeout=5)

    child = seen["env"]
    assert seen["args"] == [config.BU_CLI, "--reload"]
    assert isinstance(child, dict)
    assert "HTTPS_PROXY" not in child


def test_a_non_zero_exit_raises_with_the_end_of_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, Proc(1, "partial output", "browser-harness: daemon refused the connection"))

    with pytest.raises(BrowserError, match=r"exited with code 1: .*daemon refused the connection"):
        runner.run_browser_command(
            "print(1)", profile="personal", ws="ws://127.0.0.1:9/devtools/browser/x"
        )


def test_a_timeout_raises_and_says_how_long_it_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, subprocess.TimeoutExpired(cmd="browser-use", timeout=3, stderr="slow"))

    with pytest.raises(BrowserError, match="did not finish within 3 s"):
        runner.run_browser_command(
            "print(1)", profile="personal", timeout=3, ws="ws://127.0.0.1:9/devtools/browser/x"
        )


def test_a_tool_that_cannot_be_started_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, OSError("No such file or directory"))

    with pytest.raises(BrowserError, match="could not be started"):
        runner.run_browser_command(
            "print(1)", profile="personal", ws="ws://127.0.0.1:9/devtools/browser/x"
        )


def test_the_attempt_call_reports_a_failed_step_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, Proc(1, "partial output", "Traceback: gone wrong"))

    result = runner.run_browser_attempt(
        "print(1)", profile="personal", ws="ws://127.0.0.1:9/devtools/browser/x"
    )

    assert not result.ok
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "partial output",
        "Traceback: gone wrong",
    )


def test_the_attempt_call_reports_a_timeout_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, subprocess.TimeoutExpired(cmd="browser-use", timeout=3, stderr="slow"))

    result = runner.run_browser_attempt(
        "print(1)", profile="personal", timeout=3, ws="ws://127.0.0.1:9/devtools/browser/x"
    )

    assert result.timed_out
    assert (result.returncode, result.stderr) == (-1, "slow")


# ------------------------------------------------------------ feedback ---


def test_feedback_of_a_clean_step_is_its_output_stripped() -> None:
    assert runner.StepResult(0, "  line one\nline two\n", "", False).feedback() == (
        "line one\nline two"
    )


def test_feedback_of_a_failed_step_keeps_the_output_and_names_the_reason() -> None:
    result = runner.StepResult(1, "partial output\n", "Traceback: gone wrong\n", False)

    assert result.feedback() == (
        "partial output\n[step failed: exit code 1]\nTraceback: gone wrong"
    )


def test_feedback_of_a_failed_step_without_output_leads_with_the_reason() -> None:
    result = runner.StepResult(1, "", "Traceback: gone wrong\n", False)

    assert result.feedback() == "[step failed: exit code 1]\nTraceback: gone wrong"


def test_feedback_of_a_timed_out_step_says_so() -> None:
    assert runner.StepResult(-1, "", "", True).feedback() == "[step failed: timed out]"


def test_feedback_is_capped_so_a_page_cannot_flood_the_model() -> None:
    result = runner.StepResult(1, "x" * 9000, "e" * 9000, False)

    feedback = result.feedback()

    assert feedback.startswith("x" * runner.STDOUT_CAP)
    assert not feedback.startswith("x" * (runner.STDOUT_CAP + 1))
    assert feedback.endswith("e" * runner.STDERR_CAP)
    assert "[step failed: exit code 1]" in feedback


def test_step_result_ok_is_false_for_a_non_zero_exit_and_for_a_timeout() -> None:
    assert runner.StepResult(0, "", "", False).ok
    assert not runner.StepResult(2, "", "", False).ok
    assert not runner.StepResult(0, "", "", True).ok
