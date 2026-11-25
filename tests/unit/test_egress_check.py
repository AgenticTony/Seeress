"""The egress check: its doors and its report, which need no process to run. The check
itself starts browsers and is run as a command, here and in CI."""

import sys

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
