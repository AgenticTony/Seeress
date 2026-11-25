"""The flow and the proposal: each schema agrees with the code that reads it.

Whatever a schema accepts, the reader accepts, with the same fields. Whatever it refuses,
the reader refuses. One instance per rule of each shape.

Two things a reader written by hand gets wrong around here, so each has its instances:

- JSON has one kind of number, and 1.0 is the integer 1. True is no number in JSON, though
  Python counts it as one.
- A schema is written for every language that will read it. Its patterns are of the ECMA-262
  dialect, where `$` holds only at the very end. Python's `$` holds before a final newline
  too, so a pattern that must end at the end ends in `(?![\\s\\S])` instead, which means the
  same in both.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest

from seeress import proposals

from .test_contracts import accepted, schema

FLOW = schema("flow.schema.json")
PROPOSAL = schema("flow-proposal.schema.json")
ACTIVITY_EVENT = schema("activity-event.schema.json")

A_SITE = "mail.example.net"
ANOTHER = "invoices.example.org"
A_DAY = "2026-11-20"
A_TIME = "2026-11-20T09:15:02"


def flow(*, goal: Any = "Check the open invoices.", **changes: Any) -> dict[str, Any]:
    """A flow the schema accepts, with the given fields in place of its own."""
    base: dict[str, Any] = {
        "name": "check-invoices",
        "goal": goal,
        "sites": [A_SITE, ANOTHER],
    }
    base.update(changes)
    return base


def proposal(**changes: Any) -> dict[str, Any]:
    """A proposal the schema accepts, with the given fields in place of its own."""
    base: dict[str, Any] = {
        **flow(goal="Checks mail, then works on the invoices."),
        "order": "fixed",
        "days": 3,
        "sessions": {A_DAY: 2, "2026-11-23": 1, "2026-11-25": 1},
        "first_seen": A_TIME,
        "last_seen": "2026-11-25T08:40:10",
        "hour": 9,
        "confidence": 4,
        "status": "proposed",
    }
    base.update(changes)
    return base


def without(instance: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {key: value for key, value in instance.items() if key not in fields}


def days(count: int) -> dict[str, int]:
    """`count` different dates, each naming one session."""
    dates = [f"2026-{month:02d}-{day:02d}" for month in range(1, 13) for day in range(1, 29)]
    dates += [f"2027-01-{day:02d}" for day in range(1, 32)]
    return dict.fromkeys(dates[:count], 1)


Step = dict[str, str]
Read = Callable[[object], Any]


def refused(instance: object, read: Read, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        read(instance)


# -------------------------------------------------------------------- the flow ---

A_STEP: Step = {"site": A_SITE, "intent": "Open the folder Invoices"}

FLOW_VALID = [
    flow(),
    flow(steps=[A_STEP, {"site": ANOTHER, "intent": "Export the month"}]),
    flow(name="a" * 40, goal="g" * 1000, sites=[f"s{i}.example" for i in range(50)]),
    flow(
        steps=[{"site": A_SITE, "intent": "i" * 200}],
        sites=["sap-01.corp.example", "intranet", "localhost", "x-y.z"],
    ),
    flow(name="a", sites=["a"]),
    flow(sites=[A_SITE, "intranet_server"]),
]

FLOW_REFUSED: list[object] = [
    {},
    [],
    None,
    "a flow",
    without(flow(), "name"),
    without(flow(), "goal"),
    without(flow(), "sites"),
    flow(hour=9),
    flow(name=""),
    flow(name="Check-invoices"),
    flow(name="check_invoices"),
    flow(name="check invoices"),
    flow(name="-check"),
    flow(name="check-invoices\n"),
    flow(name="a" * 41),
    flow(name=7),
    flow(goal=""),
    flow(goal="g" * 1001),
    flow(goal=None),
    flow(goal=7),
    flow(sites=[]),
    flow(sites=A_SITE),
    flow(sites=[f"s{i}.example" for i in range(51)]),
    flow(sites=[A_SITE, ANOTHER, A_SITE]),
    flow(sites=[A_SITE, 7]),
    flow(sites=[A_SITE, "www.mail.example.net"]),
    flow(sites=[A_SITE, "Mail.Example.net"]),
    flow(sites=[A_SITE, "mail example.net"]),
    flow(sites=[A_SITE, "https://mail.example.net"]),
    flow(sites=[A_SITE, "mail.example.net/inbox"]),
    flow(sites=[A_SITE, ""]),
    flow(sites=[A_SITE, "mail.example.net\n"]),
    flow(steps="open mail"),
    flow(steps=[]),
    flow(steps=[A_STEP] * 51),
    flow(steps=[A_STEP, "open mail"]),
    flow(steps=[without(A_STEP, "intent")]),
    flow(steps=[without(A_STEP, "site")]),
    flow(steps=[{**A_STEP, "hour": 9}]),
    flow(steps=[{**A_STEP, "intent": ""}]),
    flow(steps=[{**A_STEP, "intent": "i" * 201}]),
    flow(steps=[{**A_STEP, "intent": 7}]),
    flow(steps=[{**A_STEP, "site": "www.mail.example.net"}]),
]


@pytest.mark.parametrize("instance", FLOW_VALID, ids=lambda instance: json.dumps(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_accepted_flow(instance: dict[str, Any]) -> None:
    assert accepted(FLOW, instance)

    flow_read = proposals.Flow.parse(instance)

    assert flow_read.as_dict() == instance


@pytest.mark.parametrize("instance", FLOW_REFUSED, ids=lambda instance: repr(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_refused_flow(instance: object) -> None:
    assert not accepted(FLOW, instance)

    refused(instance, proposals.Flow.parse, "the flow has an unexpected shape")


def test_a_flow_with_no_steps_holds_none_and_one_with_steps_holds_them() -> None:
    bare = proposals.Flow.parse(flow())
    with_steps = proposals.Flow.parse(flow(steps=[A_STEP]))

    assert bare.steps is None
    assert with_steps.steps == (proposals.Step(A_SITE, "Open the folder Invoices"),)
    assert "steps" not in bare.as_dict()


# ---------------------------------------------------------------- the proposal ---

PROPOSAL_VALID = [
    proposal(),
    proposal(goal="", confidence=None),
    proposal(steps=[A_STEP], status="approved", goal="Check the invoices every morning."),
    proposal(steps=[A_STEP], status="compiled"),
    proposal(status="dismissed"),
    proposal(order="varies"),
    proposal(days=1, sessions={A_DAY: 1}),
    proposal(days=366, sessions=days(366)),
    proposal(hour=0),
    proposal(hour=23),
    proposal(confidence=1),
    proposal(confidence=5),
    proposal(days=3.0, hour=9.0, confidence=4.0, sessions={A_DAY: 2.0}),
    proposal(sessions={"2026-11-25": 1, A_DAY: 2, "2026-11-23": 1}),
]


PROPOSAL_REFUSED = [
    *[
        pytest.param(without(proposal(), field), id=f"without {field}")
        for field in (
            "name",
            "goal",
            "sites",
            "order",
            "days",
            "sessions",
            "first_seen",
            "last_seen",
            "hour",
            "confidence",
            "status",
        )
    ],
    proposal(hour_of_day=9),
    proposal(goal="g" * 1001),
    proposal(goal=7),
    proposal(name="Check-invoices"),
    proposal(sites=[]),
    proposal(steps=[]),
    proposal(order="in order"),
    proposal(order="Fixed"),
    proposal(order=None),
    proposal(order=1),
    proposal(days=0),
    proposal(days=-1),
    proposal(days=1.5),
    proposal(days=True),
    proposal(days="3"),
    # Python reads these, though JSON cannot write them. They are no integer either.
    proposal(days=float("nan")),
    proposal(days=float("inf")),
    proposal(sessions={}),
    proposal(sessions=[]),
    proposal(sessions={"20261120": 1}),
    proposal(sessions={"2026-11-2": 1}),
    proposal(sessions={"2026-11-20\n": 1}),
    proposal(sessions={"": 1}),
    proposal(sessions={A_DAY: 0}),
    proposal(sessions={A_DAY: -1}),
    proposal(sessions={A_DAY: 1.5}),
    proposal(sessions={A_DAY: True}),
    proposal(sessions={A_DAY: "2"}),
    proposal(sessions={A_DAY: None}),
    proposal(sessions=days(367)),
    proposal(first_seen="2026-11-20"),
    proposal(first_seen="2026-11-20T09:15"),
    proposal(first_seen="2026-11-20T09:15:02.5"),
    proposal(first_seen="2026-11-20T09:15:02+01:00"),
    proposal(first_seen="2026-11-20T09:15:02\n"),
    proposal(first_seen=None),
    proposal(first_seen=7),
    proposal(last_seen="2026-11-25 08:40:10"),
    proposal(hour=24),
    proposal(hour=-1),
    proposal(hour=9.5),
    proposal(hour=True),
    proposal(hour="9"),
    proposal(hour=None),
    proposal(confidence=0),
    proposal(confidence=6),
    proposal(confidence=4.5),
    proposal(confidence=True),
    proposal(confidence="4"),
    proposal(confidence=[]),
    proposal(status="proposes"),
    proposal(status="Approved"),
    proposal(status=None),
    proposal(status=1),
]


@pytest.mark.parametrize("instance", PROPOSAL_VALID, ids=lambda instance: json.dumps(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_accepted_proposal(
    instance: dict[str, Any],
) -> None:
    assert accepted(PROPOSAL, instance)

    read = proposals.Proposal.parse(instance)

    assert read.as_dict() == instance


@pytest.mark.parametrize("instance", PROPOSAL_REFUSED, ids=lambda instance: repr(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_refused_proposal(instance: object) -> None:
    assert not accepted(PROPOSAL, instance)

    refused(instance, proposals.Proposal.parse, "the proposal has an unexpected shape")


def test_a_proposal_holds_its_sessions_by_day_and_writes_them_in_order() -> None:
    read = proposals.Proposal.parse(proposal(sessions={"2026-11-25": 1, A_DAY: 2}))

    assert read.days == 3
    assert read.sessions == {A_DAY: 2, "2026-11-25": 1}
    written = read.as_dict()["sessions"]
    assert isinstance(written, dict)
    assert list(written) == [A_DAY, "2026-11-25"]


def test_a_number_written_as_a_float_is_read_as_the_integer_it_is() -> None:
    read = proposals.Proposal.parse(proposal(days=3.0, hour=9.0, confidence=4.0))

    assert (read.days, read.hour, read.confidence) == (3, 9, 4)
    assert isinstance(read.days, int) and not isinstance(read.days, bool)


def test_the_reader_holds_the_schemas_own_patterns_and_words() -> None:
    """The reader searches with the patterns the schemas write, and with their lists of
    words. An instance can show only that the two agree on that instance."""
    flow_properties = FLOW["properties"]
    proposal_properties = PROPOSAL["properties"]
    step_properties = proposal_properties["steps"]["items"]["properties"]
    day_names = proposal_properties["sessions"]["propertyNames"]
    site = proposals.SITE_SHAPE.pattern

    assert proposals.NAME_SHAPE.pattern == flow_properties["name"]["pattern"]
    assert site == flow_properties["sites"]["items"]["pattern"]
    assert site == step_properties["site"]["pattern"]
    assert proposals.DAY_SHAPE.pattern == day_names["pattern"]
    assert proposals.TIME_SHAPE.pattern == proposal_properties["first_seen"]["pattern"]
    assert proposals.TIME_SHAPE.pattern == proposal_properties["last_seen"]["pattern"]
    assert list(proposals.ORDERS) == proposal_properties["order"]["enum"]
    assert list(proposals.STATUSES) == proposal_properties["status"]["enum"]


def test_a_site_and_a_second_are_the_same_in_every_message() -> None:
    """The flow and the proposal write a site and a second as the activity event does. The
    miner carries both from one to the others, and nothing translates them."""
    event = ACTIVITY_EVENT["properties"]

    assert proposals.SITE_SHAPE.pattern == event["site"]["pattern"]
    assert proposals.TIME_SHAPE.pattern == event["ts"]["pattern"]
