"""The work of `seeress mine`: the record read, mined and reported, with a model that is a
fake.

The record is invented and written for each test under a temporary home. The clock is never
read: whatever time the work needs comes in as `now`.
"""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from seeress import activity, brain, mine, miner, proposals
from seeress.errors import MiningError

MONDAY, TUESDAY, WEDNESDAY, FRIDAY, SATURDAY = (
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-20",
    "2026-11-21",
)
THREE_DAYS = (MONDAY, WEDNESDAY, FRIDAY)
FILE = "spike-20261116.jsonl"
ANOTHER_FILE = "spike-20261117.jsonl"
A_THIRD_FILE = "spike-20261118.jsonl"
TITLE = "A page title that must not be shown"
NOW = activity.epoch(f"{FRIDAY}T12:00:00")


def at(day: str, clock: str) -> str:
    """A timestamp of the logger's form: a day and a wall-clock time."""
    return f"{day}T{clock}"


def visit(site: str, ts: str, path: str = "/") -> dict[str, Any]:
    """A line the logger writes when a tab goes to a page of a site."""
    return {
        "ts": ts,
        "kind": "nav",
        "target": "1a2b3c4d",
        "url": f"https://{site}.example{path}",
        "title": TITLE,
    }


def ritual(day: str, sites: str, hour: int = 9) -> list[dict[str, Any]]:
    """A visit to each site in turn, a minute apart, from the hour."""
    return [
        visit(site, at(day, f"{hour:02d}:{minute:02d}:00")) for minute, site in enumerate(sites)
    ]


def write(home: Path, name: str, *lines: dict[str, Any]) -> None:
    """Write one file of the record under home."""
    watch = home / "watch"
    watch.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (watch / name).write_text(text, encoding="utf-8")


def week(home: Path, sites: str = "abc") -> None:
    """The same ritual at nine on Monday, Wednesday and Friday."""
    write(home, FILE, *[line for day in THREE_DAYS for line in ritual(day, sites)])


def an_outage(day: str, start: str, end: str) -> list[dict[str, Any]]:
    """The flood of chrome_down lines the logger writes while it cannot reach Chrome, and
    the connected that ends it."""
    return [
        {"ts": at(day, start), "kind": "chrome_down", "error": "FileNotFoundError"},
        {"ts": at(day, end), "kind": "chrome_down", "error": "FileNotFoundError"},
        {"ts": at(day, end), "kind": "connected", "port": 9222},
    ]


class Model:
    """A model that answers with one text, and remembers what it was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def said(*objects: dict[str, object]) -> str:
    return json.dumps(list(objects))


def checked(number: int) -> dict[str, object]:
    """A model's answer for one candidate: a sentence and a confidence."""
    return {"n": number, "sentence": "Checks.", "confidence": 3}


def files_of(home: Path) -> dict[str, list[proposals.Proposal]]:
    """The proposals remembered under home: per file, every line that reads."""
    remembered = {}
    for file in sorted((home / "proposals").iterdir()):
        text = file.read_text(encoding="utf-8")
        remembered[file.name] = [
            proposals.Proposal.parse(json.loads(line)) for line in text.splitlines()
        ]
    return remembered


def names(sites: Sequence[str]) -> str:
    return proposals.name_for([f"{site}.example" for site in sites])


# ----------------------------------------------------------------- no record ---


def test_nothing_recorded_is_an_error_that_names_the_folder_and_says_what_to_check(
    tmp_path: Path,
) -> None:
    with pytest.raises(MiningError) as caught:
        mine.mine(tmp_path, ask=None, now=0.0)

    reason = str(caught.value)

    assert str(tmp_path / "watch") in reason
    assert "activity logger" in reason
    assert "remote debugging" in reason


def test_nothing_in_the_last_hours_is_the_same_error(tmp_path: Path) -> None:
    week(tmp_path)

    with pytest.raises(MiningError, match="last 24 hours"):
        mine.mine(tmp_path, 24, ask=None, now=activity.epoch(f"{SATURDAY}T12:00:00"))


# ------------------------------------------------------------------ the report ---


def test_the_report_says_what_was_mined(tmp_path: Path) -> None:
    week(tmp_path)

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert report.startswith("Mined all recorded time: 9 events in 3 sittings.\n")


def test_the_last_hours_alone_are_mined(tmp_path: Path) -> None:
    week(tmp_path)

    report = mine.mine(tmp_path, 24, ask=None, now=NOW)

    assert report.startswith("Mined the last 24 hours: 3 events in 1 sitting.\n")


def test_the_gaps_come_before_the_proposals(tmp_path: Path) -> None:
    week(tmp_path)

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert report.index("Time with no data") < report.index("Proposals")


def test_time_with_no_data_is_given_against_the_scheduled_time(tmp_path: Path) -> None:
    """From eight to six, with one hole of an hour at the start."""
    half_hours = (f"{hour:02d}:{minute:02d}:00" for hour in range(9, 18) for minute in (0, 30))
    clocks = ["08:00:00", *half_hours, "18:00:00"]
    write(tmp_path, FILE, *[visit("a", at(TUESDAY, clock)) for clock in clocks])

    report = mine.mine(tmp_path, ask=None, now=activity.epoch(at(TUESDAY, "18:00:00")))

    assert "Time with no data: 60 of 600 minutes in the schedule (10%)." in report
    assert f"  {at(TUESDAY, '08:00:01')} to 09:00:00   60 min  nothing was written" in report


def test_an_outage_is_listed_as_the_logger_being_down(tmp_path: Path) -> None:
    write(
        tmp_path,
        FILE,
        visit("a", at(TUESDAY, "10:00:00")),
        visit("a", at(TUESDAY, "10:10:00")),
        *an_outage(TUESDAY, "10:10:00", "10:47:00"),
        visit("a", at(TUESDAY, "10:47:00")),
    )

    report = mine.mine(tmp_path, ask=None, now=activity.epoch(at(TUESDAY, "10:47:00")))

    assert "Time with no data: 37 of 47 minutes in the schedule (79%)." in report
    assert f"  {at(TUESDAY, '10:10:00')} to 10:47:00   37 min  the logger was down" in report
    assert "nothing was written" not in report


def test_an_outage_cuts_a_sitting_short(tmp_path: Path) -> None:
    """Ten minutes lie on either side of an outage of one minute. Nothing spans the outage.
    The event recorded in the second the outage began was recorded before it, and so sits
    alone: three sittings."""
    write(
        tmp_path,
        FILE,
        visit("a", at(TUESDAY, "10:00:00")),
        visit("a", at(TUESDAY, "10:10:00")),
        *an_outage(TUESDAY, "10:10:00", "10:11:00"),
        visit("a", at(TUESDAY, "10:11:00")),
        visit("a", at(TUESDAY, "10:21:00")),
    )

    report = mine.mine(tmp_path, ask=None, now=activity.epoch(at(TUESDAY, "10:21:00")))

    assert report.startswith("Mined all recorded time: 4 events in 3 sittings.\n")


def test_scheduled_minutes_are_counted_inside_the_schedule_only(tmp_path: Path) -> None:
    """A hole from seven in the morning to half past six. An hour of it lies outside the
    schedule, and does not count."""
    write(
        tmp_path,
        FILE,
        visit("a", at(TUESDAY, "07:00:00")),
        visit("a", at(TUESDAY, "18:30:00")),
    )

    report = mine.mine(tmp_path, ask=None, now=activity.epoch(at(TUESDAY, "18:30:00")))

    assert "Time with no data: 600 of 600 minutes in the schedule (100%)." in report


def test_when_nothing_recurs_the_report_says_what_it_takes(tmp_path: Path) -> None:
    write(tmp_path, FILE, *ritual(TUESDAY, "abc"))

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert "Proposals: none." in report
    assert "at least 3 days" in report
    assert not (tmp_path / "proposals").exists()


# --------------------------------------------------------------- the proposals ---


def test_one_file_per_proposal_whose_line_is_what_was_proposed(tmp_path: Path) -> None:
    week(tmp_path)

    mine.mine(tmp_path, ask=None, now=NOW)

    remembered = files_of(tmp_path)
    assert list(remembered) == [f"{names('abc')}.jsonl"]
    (proposal,) = remembered[f"{names('abc')}.jsonl"]
    assert proposal.sites == ("a.example", "b.example", "c.example")
    assert (proposal.goal, proposal.confidence, proposal.status) == ("", None, "proposed")


def test_a_second_run_adds_a_line(tmp_path: Path) -> None:
    week(tmp_path)

    mine.mine(tmp_path, ask=None, now=NOW)
    mine.mine(tmp_path, ask=None, now=NOW)

    assert len(files_of(tmp_path)[f"{names('abc')}.jsonl"]) == 2


def test_a_proposal_the_user_approved_stays_approved_with_its_goal(tmp_path: Path) -> None:
    week(tmp_path)
    mine.mine(tmp_path, ask=None, now=NOW)
    file = tmp_path / "proposals" / f"{names('abc')}.jsonl"
    first = proposals.Proposal.parse(json.loads(file.read_text().splitlines()[-1]))
    approved = replace(first, status="approved", goal="The user's words.")
    file.write_text(json.dumps(approved.as_dict()) + "\n", encoding="utf-8")

    mine.mine(tmp_path, ask=None, now=NOW)

    last = files_of(tmp_path)[f"{names('abc')}.jsonl"][-1]
    assert (last.status, last.goal) == ("approved", "The user's words.")


def test_a_dismissed_proposal_is_listed_last(tmp_path: Path) -> None:
    week(tmp_path)
    write(tmp_path, ANOTHER_FILE, *[line for day in THREE_DAYS for line in ritual(day, "xyz", 11)])
    mine.mine(tmp_path, ask=None, now=NOW)
    file = tmp_path / "proposals" / f"{names('abc')}.jsonl"
    first = proposals.Proposal.parse(json.loads(file.read_text().splitlines()[-1]))
    dismissed = replace(first, status="dismissed")
    file.write_text(json.dumps(dismissed.as_dict()) + "\n", encoding="utf-8")

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert report.index(names("xyz")) < report.index(names("abc"))
    assert files_of(tmp_path)[f"{names('abc')}.jsonl"][-1].status == "dismissed"


def test_with_no_model_nothing_is_asked_and_no_sentence_is_in_the_report(tmp_path: Path) -> None:
    week(tmp_path)

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert "confidence" not in report


def test_the_model_is_asked_once_and_shown_no_path_and_no_title(tmp_path: Path) -> None:
    write(
        tmp_path,
        FILE,
        *[
            line
            for day in THREE_DAYS
            for line in (
                visit("a", at(day, "09:00:00")),
                visit("b", at(day, "09:01:00"), "/invoices/7f3a9c1e/list"),
            )
        ],
    )
    model = Model(said({"n": 1, "sentence": "Checks a.example, then b.example.", "confidence": 4}))

    report = mine.mine(tmp_path, ask=model, now=NOW)

    (prompt,) = model.prompts
    assert "a.example" in prompt and "b.example" in prompt
    assert "invoices" not in prompt and "7f3a9c1e" not in prompt
    assert TITLE not in prompt
    assert "confidence 4: Checks a.example, then b.example." in report


def test_a_sentence_that_names_a_site_outside_the_evidence_is_not_in_the_report(
    tmp_path: Path,
) -> None:
    week(tmp_path)
    model = Model(said({"n": 1, "sentence": "Nowhere.example.org every day.", "confidence": 5}))

    report = mine.mine(tmp_path, ask=model, now=NOW)

    assert "Nowhere.example.org" not in report
    assert "confidence" not in report


def test_a_ritual_whose_order_varies_is_proposed_once_with_a_sequence_of_the_same_sites(
    tmp_path: Path,
) -> None:
    """abc on Monday and Wednesday, and abx on Friday. The sequence ab recurs on three days,
    and so does the set {a, b}. The sequence says more, so it alone is proposed."""
    write(tmp_path, FILE, *ritual(MONDAY, "abc"), *ritual(WEDNESDAY, "abc"), *ritual(FRIDAY, "abx"))

    mine.mine(tmp_path, ask=None, now=NOW)

    assert list(files_of(tmp_path)) == [f"{names('ab')}.jsonl"]


def test_an_event_inside_an_agents_run_is_not_mined(tmp_path: Path) -> None:
    week(tmp_path, "abcz")
    write(
        tmp_path,
        "agent-runs.jsonl",
        *[
            {"start": at(day, "09:03:00"), "end": at(day, "09:03:00"), "what": "task", "name": None}
            for day in THREE_DAYS
        ],
    )

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert report.startswith("Mined all recorded time: 9 events in 3 sittings.\n")
    (proposal,) = files_of(tmp_path)[f"{names('abc')}.jsonl"]
    assert proposal.sites == ("a.example", "b.example", "c.example")


def test_only_the_strongest_candidates_are_proposed(tmp_path: Path) -> None:
    week(tmp_path)
    write(tmp_path, A_THIRD_FILE, *[line for day in THREE_DAYS for line in ritual(day, "pqr", 13)])
    write(tmp_path, ANOTHER_FILE, *[line for day in THREE_DAYS for line in ritual(day, "xyz", 11)])
    model = Model(
        said(
            {"n": 1, "sentence": "a.", "confidence": 3}, {"n": 2, "sentence": "b.", "confidence": 3}
        )
    )

    mine.mine(tmp_path, ask=model, now=NOW, asked_at_most=2)

    # Three rituals on as many days and in as many sittings: the alphabet decides.
    assert list(files_of(tmp_path)) == [f"{names('abc')}.jsonl", f"{names('pqr')}.jsonl"]
    assert len(model.prompts) == 1


# ------------------------------------------------------ the proposal commands ---


def a_proposal(**changes: Any) -> proposals.Proposal:
    """A proposal with its evidence invented, named after the sites a, b and c."""
    fields: dict[str, Any] = {
        "name": names("abc"),
        "goal": "",
        "sites": ("a.example", "b.example", "c.example"),
        "order": "fixed",
        "days": 3,
        "sessions": {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1},
        "first_seen": at(MONDAY, "09:00:00"),
        "last_seen": at(FRIDAY, "09:02:00"),
        "hour": 9,
        "confidence": None,
        "status": "proposed",
    }
    fields.update(changes)
    return proposals.Proposal(**fields)


def last_of(home: Path, name: str = names("abc")) -> proposals.Proposal:
    """The proposal remembered last under a name."""
    kept = files_of(home)[f"{name}.jsonl"]
    assert kept
    return kept[-1]


def test_show_prints_the_name_the_status_the_sites_the_evidence_and_the_sentence(
    tmp_path: Path,
) -> None:
    proposals.remember(tmp_path, a_proposal(goal="Checks mail.", confidence=4))

    shown = mine.show(names("abc"), tmp_path)

    assert shown.splitlines() == [
        f"{names('abc')}   proposed",
        "  sites:  a.example → b.example → c.example",
        "  seen:   on 3 days, in 3 sittings; first 2026-11-16T09:00:00, last"
        " 2026-11-20T09:02:00; usually at hour 9",
        "  model:  confidence 4: Checks mail.",
    ]


def test_show_of_a_proposal_that_is_not_there_refuses_and_names_the_ones_there_are(
    tmp_path: Path,
) -> None:
    proposals.remember(tmp_path, a_proposal())

    with pytest.raises(MiningError) as caught:
        mine.show("nope", tmp_path)

    reason = str(caught.value)
    assert "nope" in reason
    assert names("abc") in reason


def test_show_when_no_proposal_was_ever_remembered_says_so(tmp_path: Path) -> None:
    with pytest.raises(MiningError, match="none"):
        mine.show("nope", tmp_path)


def test_a_goal_in_the_users_own_words_approves_a_proposal(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal())

    answer = mine.set_goal(names("abc"), "Check the invoices every morning.", tmp_path)

    assert answer == f"Goal saved. To compile it: seeress proposal compile {names('abc')}"
    kept = last_of(tmp_path)
    assert (kept.goal, kept.status) == ("Check the invoices every morning.", "approved")
    assert len(files_of(tmp_path)[f"{names('abc')}.jsonl"]) == 2


@pytest.mark.parametrize("status", ["proposed", "dismissed"])
def test_a_goal_makes_a_proposal_approved_whatever_it_was(tmp_path: Path, status: str) -> None:
    proposals.remember(tmp_path, a_proposal(status=status))

    mine.set_goal(names("abc"), "Check.", tmp_path)

    assert last_of(tmp_path).status == "approved"


def test_a_goal_on_a_compiled_proposal_is_saved_and_the_proposal_stays_compiled(
    tmp_path: Path,
) -> None:
    proposals.remember(tmp_path, a_proposal(status="compiled", goal="The old words."))

    mine.set_goal(names("abc"), "The new words.", tmp_path)

    kept = last_of(tmp_path)
    assert (kept.goal, kept.status) == ("The new words.", "compiled")


def test_a_goal_changes_no_evidence(tmp_path: Path) -> None:
    before = a_proposal(days=4, sessions={MONDAY: 2, WEDNESDAY: 1, FRIDAY: 1}, hour=10)
    proposals.remember(tmp_path, before)

    mine.set_goal(names("abc"), "Check.", tmp_path)

    untouched = replace(before, goal="", status="proposed")
    assert replace(last_of(tmp_path), goal="", status="proposed") == untouched


@pytest.mark.parametrize(
    "goal",
    ["", "   ", "x" * 1001],
    ids=["empty", "whitespace only", "1001 characters"],
)
def test_a_goal_that_is_no_goal_is_refused_and_nothing_is_written(
    tmp_path: Path, goal: str
) -> None:
    proposals.remember(tmp_path, a_proposal())

    with pytest.raises(MiningError, match="goal"):
        mine.set_goal(names("abc"), goal, tmp_path)

    assert len(files_of(tmp_path)[f"{names('abc')}.jsonl"]) == 1


def test_a_goal_is_stripped_of_its_ends(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal())

    mine.set_goal(names("abc"), "  Check the invoices.\n", tmp_path)

    assert last_of(tmp_path).goal == "Check the invoices."


def test_a_goal_of_1000_characters_stands(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal())

    mine.set_goal(names("abc"), "x" * 1000, tmp_path)

    assert last_of(tmp_path).goal == "x" * 1000


def test_dismiss_makes_a_proposal_dismissed_and_keeps_its_goal(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(goal="A sentence."))

    answer = mine.dismiss(names("abc"), tmp_path)

    assert answer == f"Dismissed: {names('abc')}"
    kept = last_of(tmp_path)
    assert (kept.status, kept.goal) == ("dismissed", "A sentence.")


@pytest.mark.parametrize("status", ["approved", "compiled"])
def test_a_proposal_the_user_approved_or_compiled_can_be_dismissed_all_the_same(
    tmp_path: Path, status: str
) -> None:
    """A decision is the user's to take back."""
    proposals.remember(tmp_path, a_proposal(status=status, goal="The user's words."))

    mine.dismiss(names("abc"), tmp_path)

    assert last_of(tmp_path).status == "dismissed"


def test_dismiss_of_a_proposal_that_is_not_there_refuses(tmp_path: Path) -> None:
    with pytest.raises(MiningError, match="no proposal"):
        mine.dismiss("nope", tmp_path)


# ------------------------------------------------------------------ progress ---


def test_progress_counts_proposals_by_status(tmp_path: Path) -> None:
    for site, status in zip("abcd", ["dismissed", "proposed", "compiled", "approved"], strict=True):
        name = names(site)
        proposals.remember(
            tmp_path, a_proposal(name=name, sites=(f"{site}.example",), status=status)
        )

    assert "Proposals: 1 proposed, 1 approved, 1 compiled, 1 dismissed." in mine.progress(tmp_path)


def test_progress_prints_the_days_a_compiled_flow_ran_well_on(tmp_path: Path) -> None:
    """A day is counted once, however many good runs it had. A failed run is no day."""
    proposals.remember(tmp_path, a_proposal(status="compiled", goal="Check."))
    write(
        tmp_path,
        "flow-runs.jsonl",
        {"ts": at(MONDAY, "09:00:00"), "name": names("abc"), "exit": 0},
        {"ts": at(TUESDAY, "09:00:00"), "name": names("abc"), "exit": 0},
        {"ts": at(TUESDAY, "10:00:00"), "name": names("abc"), "exit": 0},
        {
            "ts": at(WEDNESDAY, "09:00:00"),
            "name": names("abc"),
            "exit": 1,
            "error": "BrowserError",
        },
        {"ts": at(SATURDAY, "09:00:00"), "name": names("xyz"), "exit": 0},
    )

    shown = mine.progress(tmp_path)

    assert (
        f"{names('abc')}   ran well on 2 days, from 2026-11-16 to 2026-11-17, 1 day apart" in shown
    )
    assert names("xyz") not in shown


def test_progress_counts_the_days_between_the_first_and_the_last(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(status="compiled", goal="Check."))
    write(
        tmp_path,
        "flow-runs.jsonl",
        {"ts": at(MONDAY, "09:00:00"), "name": names("abc"), "exit": 0},
        {"ts": at(SATURDAY, "09:00:00"), "name": names("abc"), "exit": 0},
    )

    shown = mine.progress(tmp_path)

    assert "from 2026-11-16 to 2026-11-21, 5 days apart" in shown


def test_progress_says_when_a_compiled_flow_has_never_run_well(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(status="compiled", goal="Check."))

    assert f"{names('abc')}   has never run well" in mine.progress(tmp_path)


def test_progress_says_when_nothing_is_compiled_and_how_to_compile(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(status="approved", goal="Check."))

    shown = mine.progress(tmp_path)

    assert "Nothing is compiled." in shown
    assert 'seeress proposal goal NAME "...", then seeress proposal compile NAME' in shown


def test_progress_when_no_proposal_was_ever_remembered(tmp_path: Path) -> None:
    shown = mine.progress(tmp_path)

    assert "Proposals: 0 proposed, 0 approved, 0 compiled, 0 dismissed." in shown
    assert "Nothing is compiled." in shown


def test_a_file_of_which_no_line_reads_is_not_counted(tmp_path: Path) -> None:
    file = proposals.folder(tmp_path) / f"{names('abc')}.jsonl"
    file.write_text("this line does not read\n", encoding="utf-8")

    assert "Proposals: 0 proposed, 0 approved, 0 compiled, 0 dismissed." in mine.progress(tmp_path)


# --------------------------------------------------------- which model is asked ---


def a_cloud_model_in_the_settings(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings file with a cloud endpoint in it, and its key in the environment."""
    settings = home / ".seeress" / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        '[[endpoints]]\nname = "tenant"\nbase_url = "https://models.tenant.example/v1"\n'
        'api_key_env = "TENANT_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANT_KEY", "t")


def test_the_model_is_one_on_this_machine_whatever_the_settings_say(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt is one person's activity. A cloud endpoint in the settings, and its key
    in the environment, change nothing: a model on this machine reads it, and no other."""
    a_cloud_model_in_the_settings(home, monkeypatch)
    asked: list[tuple[brain.Chosen, float]] = []

    def fake_chat(endpoint: brain.Chosen, messages: object, timeout: float = 300) -> str:
        asked.append((endpoint, timeout))
        return "[]"

    monkeypatch.setattr(mine, "chat", fake_chat)

    mine.local_model()("a prompt")

    assert [(endpoint.name, endpoint.base_url) for endpoint, _ in asked] == [
        ("ollama-local", "http://localhost:11434/v1")
    ]
    assert [timeout for _, timeout in asked] == [600]


def test_run_reads_the_record_under_the_seeress_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    week(home / ".seeress")
    made = Model(said({"n": 1, "sentence": "Checks the mail.", "confidence": 4}))
    monkeypatch.setattr(mine, "local_model", lambda: made)

    report = mine.run()

    assert "Mined all recorded time: 9 events in 3 sittings." in report
    assert "Checks the mail." in report
    assert len(made.prompts) == 1


def test_run_with_no_llm_asks_no_model(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    week(home / ".seeress")

    def the_model_was_asked_for() -> miner.Ask:
        raise AssertionError("the model was asked for, and no_llm was set")

    monkeypatch.setattr(mine, "local_model", the_model_was_asked_for)

    report = mine.run(no_llm=True)

    assert "Mined all recorded time: 9 events in 3 sittings." in report
    assert "model:" not in report


# ------------------------------------------------------ the words of the user ---


def test_the_words_of_the_user_are_shown_as_theirs_and_with_no_confidence(
    tmp_path: Path,
) -> None:
    """Once the goal is the user's, it is not shown as a model's sentence, nor with a
    confidence a model gave to another sentence."""
    week(tmp_path)
    mine.mine(
        tmp_path,
        ask=Model(said({"n": 1, "sentence": "Checks the mail.", "confidence": 4})),
        now=NOW,
    )
    name = names("abc")
    mine.set_goal(name, "Clear the invoices.", home=tmp_path)

    shown = mine.show(name, home=tmp_path)

    assert "  goal:   Clear the invoices.\n" in shown
    assert "model:" not in shown
    assert "confidence" not in shown
    assert "Checks the mail." not in shown
    kept = proposals.last(tmp_path, name)
    assert kept is not None and kept.confidence is None

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert "  goal:   Clear the invoices.\n" in report
    assert "model:" not in report


# ------------------------------------------------- what is proposed, and how ---


def test_a_dismissed_proposal_takes_no_place_among_those_asked_about(tmp_path: Path) -> None:
    """Four rituals recur, and three are asked about at the most. The three offered were
    dismissed: the fourth is offered next time, and none of the three is asked about again."""
    lines: list[dict[str, Any]] = []
    for hour, sites in enumerate(("abc", "def", "ghi", "jkl"), start=9):
        lines += [line for day in THREE_DAYS for line in ritual(day, sites, hour=hour)]
    write(tmp_path, FILE, *lines)
    first = Model(said(*(checked(n) for n in (1, 2, 3))))
    mine.mine(tmp_path, ask=first, now=NOW, asked_at_most=3)
    for sites in ("abc", "def", "ghi"):
        mine.dismiss(names(sites), home=tmp_path)
    second = Model(said({"n": 1, "sentence": "Checks.", "confidence": 3}))

    report = mine.mine(tmp_path, ask=second, now=NOW, asked_at_most=3)

    assert len(second.prompts) == 1
    assert "1. sites: j.example → k.example → l.example" in second.prompts[0]
    for sites in ("abc", "def", "ghi"):
        for site in sites:
            assert f"{site}.example" not in second.prompts[0]
        kept = proposals.last(tmp_path, names(sites))
        assert kept is not None and kept.status == "dismissed"
    assert f"{names('jkl')}   proposed" in report


def test_a_pattern_seen_on_more_than_a_year_of_days_is_left_out_and_said_so(
    tmp_path: Path,
) -> None:
    """A proposal holds evidence on 366 days at the most. One that could never be read back
    is not written."""
    first = date(2025, 1, 1)
    days = [(first + timedelta(days=number)).isoformat() for number in range(367)]
    write(tmp_path, FILE, *[line for day in days for line in ritual(day, "abc")])

    report = mine.mine(tmp_path, ask=None, now=activity.epoch(f"{days[-1]}T12:00:00"))

    assert "Proposals: none." in report
    assert "1 pattern recurs on more than 366 days" in report
    assert proposals.every(tmp_path) == []


def test_sequences_and_rituals_are_ordered_together_by_strength(tmp_path: Path) -> None:
    """A ritual seen on four days comes before a sequence seen on three."""
    lines: list[dict[str, Any]] = []
    for day in THREE_DAYS:
        lines += ritual(day, "abc")
    for number, day in enumerate((MONDAY, TUESDAY, WEDNESDAY, FRIDAY)):
        pair = ("d", "e") if number % 2 == 0 else ("e", "d")
        lines += [visit(site, at(day, f"10:0{minute}:00")) for minute, site in enumerate(pair)]
    write(tmp_path, FILE, *lines)
    made = Model(said(*(checked(n) for n in (1, 2))))

    mine.mine(tmp_path, ask=made, now=NOW)

    (prompt,) = made.prompts
    assert prompt.index("1. sites: d.example → e.example (the order varies)") < prompt.index(
        "2. sites: a.example → b.example → c.example (always in this order)"
    )


def test_nothing_is_left_once_the_agent_s_minutes_are_taken_out(tmp_path: Path) -> None:
    """Every event was written while the agent drove the browser. The report says so, and
    does not say that no event can be placed in the schedule."""
    week(tmp_path)
    write(
        tmp_path,
        "agent-runs.jsonl",
        {
            "start": at(MONDAY, "08:00:00"),
            "end": at(FRIDAY, "13:00:00"),
            "what": "task",
            "name": None,
        },
    )

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert "Mined all recorded time: 0 events in 0 sittings." in report
    assert "every one of the 9 events recorded was written while the agent" in report
    assert "no event can be placed in the schedule" not in report


def test_at_most_twenty_five_are_asked_about(tmp_path: Path) -> None:
    """With more candidates than that, the strongest 25 are asked about, in one prompt."""
    lines: list[dict[str, Any]] = []
    for number in range(26):
        hour, minute = divmod(number * 37, 60)
        for day in THREE_DAYS:
            lines += [
                visit(site, at(day, f"{6 + hour:02d}:{minute + index:02d}:00"))
                for index, site in enumerate((f"s{number}a", f"s{number}b"))
            ]
    write(tmp_path, FILE, *lines)
    made = Model(said(*(checked(n) for n in range(1, 27))))

    report = mine.mine(tmp_path, ask=made, now=NOW)

    assert "Proposals: 25." in report
    (prompt,) = made.prompts
    assert "25. sites:" in prompt
    assert "26." not in prompt


# ------------------------------------------------- the minutes with no data ---


def no_data(report: str) -> str:
    return next(line for line in report.splitlines() if line.startswith("Time with no data:"))


def test_an_outage_that_ended_before_the_first_event_is_no_data_of_the_period(
    tmp_path: Path,
) -> None:
    """The period runs from the first event to now. An outage from 08:00 to 08:20, before
    a first event at 08:45, is not in it, though it is in the schedule's hours."""
    week(tmp_path)
    write(tmp_path, "spike-20261115.jsonl", visit("early.example", at(MONDAY, "08:45:00")))
    before = no_data(mine.mine(tmp_path, ask=None, now=NOW))

    write(tmp_path, ANOTHER_FILE, *an_outage(MONDAY, "08:00:00", "08:20:00"))

    assert no_data(mine.mine(tmp_path, ask=None, now=NOW)) == before


def test_an_outage_outside_the_schedule_hours_is_no_data(tmp_path: Path) -> None:
    week(tmp_path)
    before = no_data(mine.mine(tmp_path, ask=None, now=NOW))

    write(tmp_path, ANOTHER_FILE, *an_outage(MONDAY, "18:00:00", "20:00:00"))

    assert no_data(mine.mine(tmp_path, ask=None, now=NOW)) == before


def test_a_proposal_set_aside_after_its_goal_was_written_still_shows_the_user_s_words(
    tmp_path: Path,
) -> None:
    week(tmp_path)
    mine.mine(
        tmp_path,
        ask=Model(said({"n": 1, "sentence": "Checks the mail.", "confidence": 4})),
        now=NOW,
    )
    name = names("abc")
    mine.set_goal(name, "Clear the invoices.", home=tmp_path)
    mine.dismiss(name, home=tmp_path)

    shown = mine.show(name, home=tmp_path)

    assert "  goal:   Clear the invoices.\n" in shown
    assert "model:" not in shown
    assert "confidence" not in shown


def test_a_dismissed_proposal_that_recurs_is_left_as_the_user_saw_it(tmp_path: Path) -> None:
    week(tmp_path)
    mine.mine(
        tmp_path, ask=Model(said({"n": 1, "sentence": "First sentence.", "confidence": 4})), now=NOW
    )
    name = names("abc")
    mine.dismiss(name, home=tmp_path)

    report = mine.mine(tmp_path, ask=Model(said({"n": 1, "sentence": "Second sentence."})), now=NOW)

    kept = proposals.last(tmp_path, name)
    assert kept is not None
    assert (kept.status, kept.goal, kept.confidence) == ("dismissed", "First sentence.", 4)
    assert "Second sentence." not in report


def test_the_gaps_follow_the_schedule_of_the_settings(tmp_path: Path) -> None:
    """The settings say Wednesday only, from nine to ten. Of that hour the three rituals at
    nine hold three minutes and two seconds. The hole begins the second after the last
    event, and is 57 minutes and 59 seconds, which is 58."""
    week(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[capture]\ndays = ["wed"]\nfrom = "09:00"\nto = "10:00"\n', encoding="utf-8"
    )

    report = mine.mine(tmp_path, ask=None, now=NOW)

    assert no_data(report) == "Time with no data: 58 of 60 minutes in the schedule (97%)."


# ------------------------------------------------------- a task inside one site ---


SOURCE, TARGET = "a1b2c3d4", "e5f60718"  # two tabs of one site
CELL = ("td", 'td[name="qty-#"]')
FIELD = ("input", 'input[name="line-#"]')
SAVE = ("button", 'button[data-testid="save"]')


def did(kind: str, ts: str, element: tuple[str, str], **where: Any) -> dict[str, Any]:
    """A line the capture writes for a click, an input, a submit or a select in a page."""
    line: dict[str, Any] = {
        "v": 1,
        "ts": ts,
        "kind": kind,
        "site": where.get("site", "erp.example"),
        "path": where.get("path", "/"),
        "target": where.get("tab", SOURCE),
        "action": {"type": kind, "tag": element[0], "fingerprint": element[1]},
    }
    if "field" in where:
        line["action"]["field"] = where["field"]
    return line


def job(day: str, rounds: int = 3) -> list[dict[str, Any]]:
    """A copy-and-paste job between two tabs of one site, then a save. A second apart."""
    lines: list[dict[str, Any]] = []
    second = 0
    for _ in range(rounds):
        for line in (
            did("click", "", CELL, path="/orders"),
            did("click", "", FIELD, path="/report", tab=TARGET),
            did("input", "", FIELD, path="/report", tab=TARGET, field="text"),
        ):
            second += 1
            line["ts"] = at(day, f"09:{second // 60:02d}:{second % 60:02d}")
            lines.append(line)
    second += 1
    lines.append(
        did(
            "click",
            at(day, f"09:{second // 60:02d}:{second % 60:02d}"),
            SAVE,
            path="/report",
            tab=TARGET,
        )
    )
    return lines


def three_days_of_job(home: Path) -> None:
    """The job on Monday, Wednesday and Friday, each in the file of its day."""
    for day in THREE_DAYS:
        write(home, f"activity-{day.replace('-', '')}.jsonl", *job(day))


def test_the_report_shows_the_steps_of_a_task_inside_one_site(tmp_path: Path) -> None:
    three_days_of_job(tmp_path)

    report = mine.mine(tmp_path, None, ask=None, now=NOW)

    assert "Proposals: 1." in report
    assert [
        line for line in report.splitlines() if "steps:" in line or line.startswith("          ")
    ] == [
        '  steps:  1. click  /orders  td[name="qty-#"]  ×3  other tab',
        '          2. click  /report  input[name="line-#"]  ×3  other tab',
        '          3. input  /report  input[name="line-#"]  text  ×3',
        '          4. click  /report  button[data-testid="save"]',
    ]
    assert "  sites:  erp.example" in report
    assert "  paths:" not in report


def test_nothing_of_a_step_is_remembered_with_a_proposal(tmp_path: Path) -> None:
    three_days_of_job(tmp_path)

    mine.mine(tmp_path, None, ask=None, now=NOW)

    (file,) = (tmp_path / "proposals").iterdir()
    text = file.read_text(encoding="utf-8")
    (proposal,) = files_of(tmp_path)[file.name]
    assert proposal.sites == ("erp.example",) * 4
    for not_kept in ("/orders", "/report", "td[", "input[", "button[", "qty", "steps"):
        assert not_kept not in text


def test_the_model_is_told_the_kinds_of_the_steps_and_no_page_and_no_element(
    tmp_path: Path,
) -> None:
    three_days_of_job(tmp_path)
    model = Model(said({"n": 1, "sentence": "Enters figures.", "confidence": 3}))

    report = mine.mine(tmp_path, None, ask=model, now=NOW)

    (prompt,) = model.prompts
    assert "sites: erp.example (always in this order) · steps: click, click, input, click" in prompt
    for not_told in ("/orders", "/report", "td[", "qty", "line-#"):
        assert not_told not in prompt
    assert "confidence 3: Enters figures." in report


def test_what_the_steps_say_wholly_of_the_sites_is_proposed_once(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []
    for day in THREE_DAYS:
        two = (
            did("click", "", SAVE, site="a.example"),
            did("input", "", FIELD, site="b.example", field="text"),
        )
        for clock, line in enumerate(two, start=1):
            line["ts"] = at(day, f"09:00:{clock:02d}")
            lines.append(line)
    write(tmp_path, "activity-20261116.jsonl", *lines)

    report = mine.mine(tmp_path, None, ask=None, now=NOW)

    assert "Proposals: 1." in report
    assert list(files_of(tmp_path)) != [f"{names('ab')}.jsonl"]
    (proposal,) = files_of(tmp_path)[next(iter(files_of(tmp_path)))]
    assert proposal.sites == ("a.example", "b.example")
    assert "  steps:" in report


def test_show_of_a_task_inside_one_site_says_its_sites_and_no_step(tmp_path: Path) -> None:
    three_days_of_job(tmp_path)
    mine.mine(tmp_path, None, ask=None, now=NOW)
    (name,) = [file.stem for file in (tmp_path / "proposals").iterdir()]

    shown = mine.show(name, tmp_path)

    assert "  sites:  erp.example" in shown
    assert "steps" not in shown and "/orders" not in shown
