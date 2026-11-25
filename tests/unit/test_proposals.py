"""Naming a proposal, and keeping it: the file a proposal is remembered in.

The contract tests in `tests/contract/` hold the shape of a proposal. These hold what is done
with one: the name it is known by, the line it is written as, and what a later run keeps of
it.
"""

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from seeress import flows, proposals
from seeress.errors import MiningError

SITES = ["mail.example.net", "intranet_server", "sap-01.corp.example"]
NAME = "check-invoices"


# ----------------------------------------------------------------- the name ---


def test_a_sequence_of_sites_always_has_the_same_name() -> None:
    assert proposals.name_for(SITES) == proposals.name_for(SITES)
    assert proposals.name_for(SITES) == proposals.name_for(tuple(SITES))


def test_two_sequences_that_differ_never_share_a_name() -> None:
    one_more = [*SITES, "one-more.example"]

    names = {
        proposals.name_for(sites) for sites in (SITES, SITES[:2], one_more, list(reversed(SITES)))
    }

    assert len(names) == 4


def test_two_long_sequences_that_differ_only_beyond_the_cut_have_different_names() -> None:
    first = [f"s{i}.example" for i in range(12)]
    second = [*first[:11], "other.example"]

    assert proposals.name_for(first)[:31] == proposals.name_for(second)[:31]
    assert proposals.name_for(first) != proposals.name_for(second)


def test_the_name_shows_the_sites_and_ends_in_eight_hexadecimal_digits() -> None:
    name = proposals.name_for(["mail.example.net", "dms.example"])

    _, _, digits = name.rpartition("-")

    assert name.startswith("mail-example-net-dms-example-")
    assert len(digits) == 8
    assert all(digit in "0123456789abcdef" for digit in digits)


@pytest.mark.parametrize(
    "sites",
    [
        pytest.param(["a"], id="one site of one letter"),
        pytest.param(
            ["-lead.example", ".dots.example", "__"],
            id="sites that begin with no letter or digit",
        ),
        pytest.param([f"s{i}.{'x' * 40}.example" for i in range(12)], id="twelve long sites"),
        pytest.param(["a" * 60], id="a site of sixty characters"),
        pytest.param(["-", ".", "_"], id="sites that are no letter and no digit at all"),
    ],
)
def test_the_name_is_always_a_name_a_flow_may_have(sites: list[str]) -> None:
    name = proposals.name_for(sites)

    assert flows.NAME.fullmatch(name)
    assert proposals.NAME_SHAPE.fullmatch(name)
    assert 1 <= len(name) <= 40


# -------------------------------------------------------------- keeping one ---


def a_proposal(**changes: Any) -> proposals.Proposal:
    """A proposal with its evidence invented: three days, and one sitting on each."""
    fields: dict[str, Any] = {
        "name": NAME,
        "goal": "",
        "sites": ("mail.example.net", "invoices.example.org"),
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
    return proposals.Proposal(**fields)


def lines_of(home: Path) -> list[proposals.Proposal]:
    """Every line of the proposal's file that reads."""
    text = (home / "proposals" / f"{NAME}.jsonl").read_text(encoding="utf-8")
    return [proposals.Proposal.parse(json.loads(line)) for line in text.splitlines()]


def test_the_folder_of_proposals_is_made_when_it_is_missing(tmp_path: Path) -> None:
    place = proposals.folder(tmp_path)

    assert place == tmp_path / "proposals"
    assert place.is_dir()


def test_nothing_is_remembered_under_a_name_yet_and_looking_makes_nothing(tmp_path: Path) -> None:
    assert proposals.last(tmp_path, NAME) is None
    assert not (tmp_path / "proposals").exists()


def test_what_was_remembered_last_is_the_last_line_that_reads(tmp_path: Path) -> None:
    file = proposals.folder(tmp_path) / f"{NAME}.jsonl"
    file.write_text(
        "this line does not read\n"
        + json.dumps(a_proposal(days=3).as_dict())
        + "\n"
        + json.dumps(a_proposal(days=4).as_dict())
        + "\n",
        encoding="utf-8",
    )

    assert proposals.last(tmp_path, NAME) == a_proposal(days=4)


def test_a_remembered_proposal_is_one_line_that_reads_back(tmp_path: Path) -> None:
    written = proposals.remember(tmp_path, a_proposal())

    assert lines_of(tmp_path) == [a_proposal()] == [written]


def test_a_second_run_adds_a_line_and_leaves_the_first(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(days=3))

    proposals.remember(tmp_path, a_proposal(days=4))

    assert [kept.days for kept in lines_of(tmp_path)] == [3, 4]


def test_a_proposal_that_was_only_proposed_is_written_as_new(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal(goal="A first sentence.", days=3))

    written = proposals.remember(tmp_path, a_proposal(goal="A second sentence.", days=4))

    assert (written.status, written.goal, written.days) == ("proposed", "A second sentence.", 4)


@pytest.mark.parametrize("status", ["approved", "compiled"])
def test_an_approved_or_compiled_proposal_keeps_its_goal_and_its_status(
    tmp_path: Path, status: str
) -> None:
    """The goal is the user's words and the status the user's decision. The evidence is
    always the new one."""
    proposals.remember(tmp_path, a_proposal(status=status, goal="The user's words.", days=3))

    written = proposals.remember(tmp_path, a_proposal(goal="A model's sentence.", days=4))

    assert (written.status, written.goal, written.days) == (status, "The user's words.", 4)


def test_a_dismissed_proposal_stays_dismissed_with_the_goal_the_user_saw(
    tmp_path: Path,
) -> None:
    """The evidence is new. The status and the goal are as the user left them: a model's
    newer sentence does not follow a proposal the user set aside."""
    proposals.remember(tmp_path, a_proposal(status="dismissed", goal="As the user saw it.", days=3))

    written = proposals.remember(tmp_path, a_proposal(goal="A model's sentence.", days=4))

    assert (written.status, written.goal, written.days) == ("dismissed", "As the user saw it.", 4)


def test_a_proposal_no_reader_could_read_back_is_not_written(tmp_path: Path) -> None:
    """A proposal holds evidence on 366 days at the most, and a goal of 1000 characters. A
    line nothing can read back is no memory, so it is not written at all."""
    proposals.remember(tmp_path, a_proposal())
    days = {f"2026-{number // 28 + 1:02d}-{number % 28 + 1:02d}": 1 for number in range(367)}

    with pytest.raises(MiningError, match="367 days"):
        proposals.record(tmp_path, replace(a_proposal(), days=367, sessions=days))
    with pytest.raises(MiningError, match="cannot be written"):
        proposals.record(tmp_path, replace(a_proposal(), goal="x" * 1001))

    assert len(lines_of(tmp_path)) == 1


def test_whose_words_a_goal_is_is_read_from_what_was_written_under_the_name(
    tmp_path: Path,
) -> None:
    """The status alone does not say: a proposal set aside after its goal was written keeps
    the user's words under a status that says dismissed."""
    assert proposals.written_by_the_user(tmp_path, NAME) is False
    proposals.remember(tmp_path, a_proposal(goal="A model's sentence."))
    name = a_proposal().name

    assert proposals.written_by_the_user(tmp_path, name) is False
    proposals.record(tmp_path, replace(a_proposal(), goal="The user's words.", status="approved"))
    assert proposals.written_by_the_user(tmp_path, name) is True
    proposals.record(tmp_path, replace(a_proposal(), goal="The user's words.", status="dismissed"))
    assert proposals.written_by_the_user(tmp_path, name) is True
    proposals.record(tmp_path, replace(a_proposal(), goal="Other words.", status="dismissed"))
    assert proposals.written_by_the_user(tmp_path, name) is False


def test_two_sequences_whose_sites_are_split_differently_never_share_a_name() -> None:
    assert proposals.name_for(["ab.example", "c.example"]) != proposals.name_for(
        ["a.example", "bc.example"]
    )


@pytest.mark.parametrize("name", ["../elsewhere", "check_invoices", "Check-invoices", ""])
def test_a_name_that_no_flow_may_have_names_no_file(tmp_path: Path, name: str) -> None:
    with pytest.raises(MiningError, match="name"):
        proposals.remember(tmp_path, a_proposal(name=name))
    with pytest.raises(MiningError, match="name"):
        proposals.last(tmp_path, name)

    assert not (tmp_path / "proposals").exists()
    assert list(tmp_path.iterdir()) == []


def test_remembered_proposals_are_for_the_user_alone(tmp_path: Path) -> None:
    proposals.remember(tmp_path, a_proposal())

    file = tmp_path / "proposals" / f"{NAME}.jsonl"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    assert os.access(file, os.R_OK)


def test_two_sequences_whose_shown_part_is_the_same_are_told_apart_by_their_digest() -> None:
    """The shown part of a name is cut at thirty-one characters. Beyond the cut only the
    digest tells two sequences apart, and it does."""
    long = "site" * 6  # with its dot and its tail, the first site runs past the cut
    first = [f"{long}.example", "one.example"]
    second = [f"{long}.example", "two.example"]

    assert proposals.name_for(first)[:31] == proposals.name_for(second)[:31]
    assert proposals.name_for(first) != proposals.name_for(second)
