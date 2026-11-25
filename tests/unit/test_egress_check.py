"""The egress check: its doors, its report and its command, none of which needs a process
to run. The check itself starts browsers, against nothing here and for real in CI."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from seeress.egress import Finding
from tools import egress_check
from tools.egress_check import DOORS, Run, report

SAID = "1 events written; 0 outside the schedule; 0 over the cap"
STOPPED = "Capture is not running. It last reported stopped at 2026-11-25T21:36:39."


def a_run(door: str) -> Run:
    return Run(door, [sys.executable, "-m", f"seeress.{door}"], 0, f"{SAID}\n{STOPPED}", 0)


def test_the_third_door_is_the_capture_after_the_two_of_the_agent() -> None:
    assert DOORS == ("personal", "client", "capture")


def test_the_report_of_the_capture_door_says_what_the_capture_said_and_asks_no_model() -> None:
    text = report([a_run("capture")], [])

    assert "Door: capture" in text
    assert "  model endpoint: none is asked by the capture" in text
    assert f"  the capture says: {SAID}" in text
    assert "  browser: a stand-in for the person's own, where the capture records" in text
    assert "  verdict: clean" in text


def test_the_report_of_a_task_door_still_counts_its_requests_to_the_model() -> None:
    run = a_run("client")
    run.model_requests = 3

    text = report([run], [])

    assert "  model endpoint: 3 requests, all to 127.0.0.1" in text
    assert "none is asked" not in text
    assert "browser of Seeress's own: no route out except the client's own sites" in text


def test_the_report_says_of_what_the_stand_in_browser_contacted_and_does_not_judge_it() -> None:
    """A real Chrome talks to its maker. The browser of the capture's door stands where the
    person's own would be, and what it contacts is reported and never judged."""
    run = a_run("capture")
    run.browser_contacts = ["142.251.150.120:443"]

    text = report([run], [])

    assert "(the stand-in for the person's own browser, which is not Seeress's)" in text
    assert "  verdict: clean" in text


class AWorkspace:
    """A stand-in for the throwaway home, the site on loopback and the recording hook.
    Nothing is started, bound or written."""

    root = Path("/nowhere/sr-egress")

    def __init__(self, keep: bool) -> None:
        self.keep = keep

    def __enter__(self) -> "AWorkspace":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class Asked:
    """What the check asked for, with the findings each part is told to return. An empty
    door stands for the control."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool | None]] = []
        self.controlled = 0
        self.findings: dict[str, list[Finding]] = {}

    def control(self, workspace: AWorkspace) -> list[Finding]:
        self.controlled += 1
        return self.findings.get("", [])

    def task(self, workspace: AWorkspace, door: str, personal_chrome: bool) -> Run:
        self.calls.append(("task", door, personal_chrome))
        run = a_run(door)
        run.findings = self.findings.get(door, [])
        return run

    def capture(self, workspace: AWorkspace) -> Run:
        self.calls.append(("capture", None, None))
        run = a_run("capture")
        run.findings = self.findings.get("capture", [])
        return run


@pytest.fixture
def the_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Asked]:
    """The command runs against nothing: no browser, no site, no hook file, no home."""
    asked = Asked()
    monkeypatch.setattr(egress_check, "Workspace", AWorkspace)
    monkeypatch.setattr(egress_check, "hook_path", lambda: tmp_path / "no-hook.pth")
    monkeypatch.setattr(egress_check, "control", asked.control)
    monkeypatch.setattr(egress_check, "run_door", asked.task)
    monkeypatch.setattr(egress_check, "run_capture", asked.capture)
    yield asked


def test_the_flag_for_the_real_chrome_reaches_the_task_doors_and_never_the_capture(
    the_machine: Asked, capsys: pytest.CaptureFixture[str]
) -> None:
    """Written the night the third door was added, after the flag was dropped from the
    command by mistake and no test saw it go."""
    assert egress_check.main(["--personal-chrome"]) == 0
    assert egress_check.main([]) == 0
    capsys.readouterr()

    assert the_machine.calls == [
        ("task", "personal", True),
        ("task", "client", True),
        ("capture", None, None),
        ("task", "personal", False),
        ("task", "client", False),
        ("capture", None, None),
    ]


def test_only_the_doors_asked_for_run_in_the_order_asked(the_machine: Asked) -> None:
    assert egress_check.main(["--doors", " capture , client,"]) == 0

    assert the_machine.calls == [("capture", None, None), ("task", "client", False)]


def test_a_door_that_is_not_stops_the_check_before_anything_is_started(the_machine: Asked) -> None:
    with pytest.raises(SystemExit) as stopped:
        egress_check.main(["--doors", "personal,guest"])

    assert stopped.value.code == 2
    assert the_machine.controlled == 0
    assert the_machine.calls == []


def test_a_check_already_running_is_not_run_again_over_it(
    the_machine: Asked,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two at once share one hook file, and the first to end takes it away from the other."""
    present = tmp_path / "the-hook.pth"
    present.write_text("", encoding="utf-8")
    monkeypatch.setattr(egress_check, "hook_path", lambda: present)

    assert egress_check.main([]) == 1

    assert "an egress check is already running: its hook file is present" in capsys.readouterr().out
    assert the_machine.controlled == 0
    assert the_machine.calls == []


def test_the_exit_is_one_when_any_door_or_the_control_has_a_finding(
    the_machine: Asked, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit status is what the continuous integration reads."""
    the_machine.findings = {"client": [Finding("contact", "contacted 203.0.113.7")]}
    assert egress_check.main(["--doors", "client"]) == 1
    assert "RESULT: FINDINGS" in capsys.readouterr().out

    the_machine.findings = {"": [Finding("instrument", "the hook recorded nothing")]}
    assert egress_check.main(["--doors", "client"]) == 1  # and the door still ran
    assert "RESULT: FINDINGS" in capsys.readouterr().out

    the_machine.findings = {}
    assert egress_check.main(["--doors", "client"]) == 0
    assert "RESULT: clean on every door" in capsys.readouterr().out

    assert the_machine.calls == [("task", "client", False)] * 3


def test_the_record_is_written_when_asked_for(
    the_machine: Asked, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    the_machine.findings = {"": [Finding("instrument", "the hook recorded nothing")]}
    where = tmp_path / "record.json"

    assert egress_check.main(["--doors", "client,personal", "--json", str(where)]) == 1

    record = json.loads(where.read_text(encoding="utf-8"))
    assert [run["door"] for run in record["runs"]] == ["client", "personal"]
    assert record["control"] == [{"kind": "instrument", "detail": "the hook recorded nothing"}]
    assert f"Record: {where}" in capsys.readouterr().out


def test_nothing_is_written_unless_asked_for(the_machine: Asked, tmp_path: Path) -> None:
    assert egress_check.main(["--doors", "capture"]) == 0

    assert list(tmp_path.iterdir()) == []


def test_a_kept_folder_is_named_to_the_one_who_asked_to_keep_it(
    the_machine: Asked, capsys: pytest.CaptureFixture[str]
) -> None:
    egress_check.main(["--keep"])
    assert "Kept: /nowhere/sr-egress" in capsys.readouterr().out

    egress_check.main([])
    assert "Kept:" not in capsys.readouterr().out
