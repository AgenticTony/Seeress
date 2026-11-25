"""The flow and the proposal: each schema agrees with the code that reads it.

Whatever a schema accepts, the reader accepts, with the same fields. Whatever it refuses,
the reader refuses. One instance per rule of each shape, and each instance is named after
its rule.

A proposal has every field a flow has. So every rule that refuses a flow refuses a proposal
too, except that the goal of a proposal may be empty: the refused flows are laid over a
valid proposal, and refused as proposals once more.

Four things a reader written by hand gets wrong around here, so each has its instances:

- JSON has one kind of number, and 1.0 is the integer 1. True is no number in JSON, though
  Python counts it as one.
- A schema is written for every language that will read it. Its patterns are of the ECMA-262
  dialect, where `$` holds only at the very end. Python's `$` holds before a final newline
  too, so a pattern that must end at the end ends in `(?![\\s\\S])` instead, which means the
  same in both.
- Two objects can be equal with their fields in a different order. So the order a reader
  writes in is read off its keys.
- An instance can show only that a reader and a schema agree on that instance. So the
  reader's patterns and numbers are held to the schema's own text.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest

from seeress import flows, proposals

from .schemas import accepted, schema

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


def on_a_proposal(param: pytest.ParameterSet) -> pytest.ParameterSet:
    """A refused flow as a proposal. The fields the flow has are laid over a valid proposal,
    and a field it lacks is taken from that proposal too. An instance that is no object is
    left as it is."""
    (instance,) = param.values
    if isinstance(instance, dict):
        lacking = [field for field in ("name", "goal", "sites") if field not in instance]
        instance = without(proposal(**instance), *lacking)
    return pytest.param(instance, id=param.id)


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

FLOW_REFUSED: list[pytest.ParameterSet] = [
    pytest.param({}, id="no field at all"),
    pytest.param([], id="a list"),
    pytest.param(None, id="null"),
    pytest.param("a flow", id="a string"),
    pytest.param(without(flow(), "name"), id="no name"),
    pytest.param(without(flow(), "goal"), id="no goal"),
    pytest.param(without(flow(), "sites"), id="no sites"),
    pytest.param(flow(hour=9), id="a field the schema does not know"),
    pytest.param(flow(name=""), id="name empty"),
    pytest.param(flow(name="Check-invoices"), id="name with a capital letter"),
    pytest.param(flow(name="check_invoices"), id="name with an underscore"),
    pytest.param(flow(name="check invoices"), id="name with a space"),
    pytest.param(flow(name="-check"), id="name starting with a hyphen"),
    pytest.param(flow(name="check-invoices\n"), id="name ending in a newline"),
    pytest.param(flow(name="a" * 41), id="name of 41 characters"),
    pytest.param(flow(name=None), id="name of null"),
    pytest.param(flow(name=7), id="name of another type"),
    pytest.param(flow(goal=""), id="an empty goal"),
    pytest.param(flow(goal="g" * 1001), id="goal of 1001 characters"),
    pytest.param(flow(goal=None), id="goal of null"),
    pytest.param(flow(goal=7), id="goal of another type"),
    pytest.param(flow(sites=[]), id="sites empty"),
    pytest.param(flow(sites=A_SITE), id="sites not a list"),
    pytest.param(flow(sites=[f"s{i}.example" for i in range(51)]), id="51 sites"),
    pytest.param(flow(sites=[A_SITE, ANOTHER, A_SITE]), id="a site twice"),
    pytest.param(flow(sites=[A_SITE, 7]), id="a site of another type"),
    pytest.param(flow(sites=[A_SITE, "www.mail.example.net"]), id="a site starting with www."),
    pytest.param(flow(sites=[A_SITE, "Mail.Example.net"]), id="a site with a capital letter"),
    pytest.param(flow(sites=[A_SITE, "mail example.net"]), id="a site with a space"),
    pytest.param(flow(sites=[A_SITE, "https://mail.example.net"]), id="a site with a scheme"),
    pytest.param(flow(sites=[A_SITE, "mail.example.net/inbox"]), id="a site with a path"),
    pytest.param(flow(sites=[A_SITE, ""]), id="a site empty"),
    pytest.param(flow(sites=[A_SITE, "mail.example.net\n"]), id="a site ending in a newline"),
    pytest.param(flow(steps="open mail"), id="steps not a list"),
    pytest.param(flow(steps=None), id="steps of null"),
    pytest.param(flow(steps=[]), id="steps empty"),
    pytest.param(flow(steps=[A_STEP] * 51), id="51 steps"),
    pytest.param(flow(steps=[A_STEP, "open mail"]), id="a step not an object"),
    pytest.param(flow(steps=[without(A_STEP, "intent")]), id="a step with no intent"),
    pytest.param(flow(steps=[without(A_STEP, "site")]), id="a step with no site"),
    pytest.param(flow(steps=[{**A_STEP, "hour": 9}]), id="a step with a field it does not know"),
    pytest.param(flow(steps=[{**A_STEP, "intent": ""}]), id="an intent empty"),
    pytest.param(flow(steps=[{**A_STEP, "intent": "i" * 201}]), id="an intent of 201 characters"),
    pytest.param(flow(steps=[{**A_STEP, "intent": 7}]), id="an intent of another type"),
    pytest.param(
        flow(steps=[{**A_STEP, "site": "www.mail.example.net"}]),
        id="a step's site starting with www.",
    ),
    pytest.param(
        flow(steps=[{**A_STEP, "site": "mail.example.net\n"}]),
        id="a step's site ending in a newline",
    ),
    pytest.param(
        flow(steps=[{**A_STEP, "site": "mail example.net"}]),
        id="a step's site with a space",
    ),
]

# The two rules of a flow that a proposal does not have: its goal may be empty, and the
# field a flow does not know is one a proposal does.
FLOW_ONLY = {"an empty goal", "a field the schema does not know"}


@pytest.mark.parametrize("instance", FLOW_VALID, ids=lambda instance: json.dumps(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_accepted_flow(instance: dict[str, Any]) -> None:
    assert accepted(FLOW, instance)

    flow_read = proposals.Flow.parse(instance)

    assert flow_read.as_dict() == instance


@pytest.mark.parametrize("instance", FLOW_REFUSED)
def test_the_schema_and_the_reader_agree_on_every_refused_flow(instance: object) -> None:
    assert not accepted(FLOW, instance)

    refused(instance, proposals.Flow.parse, "the flow has an unexpected shape")


def test_a_flow_with_no_steps_holds_none_and_one_with_steps_holds_them() -> None:
    bare = proposals.Flow.parse(flow())
    with_steps = proposals.Flow.parse(flow(steps=[A_STEP]))

    assert bare.steps is None
    assert with_steps.steps == (proposals.Step(A_SITE, "Open the folder Invoices"),)
    assert "steps" not in bare.as_dict()


def test_a_flow_is_written_with_its_fields_in_the_schemas_order() -> None:
    """Equality cannot see an order. So the order is read off the keys."""
    with_steps = proposals.Flow.parse(flow(steps=[A_STEP])).as_dict()
    without_steps = proposals.Flow.parse(flow()).as_dict()

    assert list(with_steps) == list(FLOW["properties"])
    steps = with_steps["steps"]
    assert isinstance(steps, list)
    assert [list(step) for step in steps] == [["site", "intent"]]
    assert list(without_steps) == ["name", "goal", "sites"]


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

PROPOSAL_REFUSED: list[pytest.ParameterSet] = [
    *(on_a_proposal(param) for param in FLOW_REFUSED if param.id not in FLOW_ONLY),
    *(
        pytest.param(without(proposal(), field), id=f"no {field}")
        for field in (
            "order",
            "days",
            "sessions",
            "first_seen",
            "last_seen",
            "hour",
            "confidence",
            "status",
        )
    ),
    pytest.param(proposal(hour_of_day=9), id="a field the schema does not know"),
    pytest.param(proposal(steps=None), id="steps of null"),
    pytest.param(proposal(order="in order"), id="order outside its two words"),
    pytest.param(proposal(order="Fixed"), id="order with a capital letter"),
    pytest.param(proposal(order=None), id="order of null"),
    pytest.param(proposal(order=1), id="order of another type"),
    pytest.param(proposal(days=0), id="days of 0"),
    pytest.param(proposal(days=-1), id="days below 0"),
    pytest.param(proposal(days=1.5), id="days with a fraction"),
    pytest.param(proposal(days=True), id="days true"),
    pytest.param(proposal(days="3"), id="days of another type"),
    # Python reads these, though JSON cannot write them. They are no integer either.
    pytest.param(proposal(days=float("nan")), id="days not a number"),
    pytest.param(proposal(days=float("inf")), id="days infinite"),
    pytest.param(proposal(sessions={}), id="sessions empty"),
    pytest.param(proposal(sessions=[]), id="sessions not an object"),
    pytest.param(proposal(sessions={"20261120": 1}), id="a day of another shape"),
    pytest.param(proposal(sessions={"2026-11-2": 1}), id="a day of nine characters"),
    pytest.param(proposal(sessions={"2026-11-20\n": 1}), id="a day ending in a newline"),
    pytest.param(proposal(sessions={"": 1}), id="a day empty"),
    pytest.param(proposal(sessions={A_DAY: 0}), id="no session on a day"),
    pytest.param(proposal(sessions={A_DAY: -1}), id="a negative number of sessions"),
    pytest.param(proposal(sessions={A_DAY: 1.5}), id="a fraction of a session"),
    pytest.param(proposal(sessions={A_DAY: True}), id="sessions true"),
    pytest.param(proposal(sessions={A_DAY: "2"}), id="sessions of another type"),
    pytest.param(proposal(sessions={A_DAY: None}), id="sessions of null"),
    pytest.param(proposal(sessions=days(367)), id="367 days of sessions"),
    pytest.param(proposal(first_seen="2026-11-20"), id="first seen a date only"),
    pytest.param(proposal(first_seen="2026-11-20T09:15"), id="first seen to the minute"),
    pytest.param(proposal(first_seen="2026-11-20T09:15:02.5"), id="first seen to a fraction"),
    pytest.param(proposal(first_seen="2026-11-20T09:15:02+01:00"), id="first seen with an offset"),
    pytest.param(proposal(first_seen="2026-11-20T09:15:02\n"), id="first seen ending in a newline"),
    pytest.param(proposal(first_seen=None), id="first seen of null"),
    pytest.param(proposal(first_seen=7), id="first seen of another type"),
    pytest.param(proposal(last_seen="2026-11-25 08:40:10"), id="last seen of another shape"),
    pytest.param(proposal(hour=24), id="hour 24"),
    pytest.param(proposal(hour=-1), id="hour below 0"),
    pytest.param(proposal(hour=9.5), id="hour with a fraction"),
    pytest.param(proposal(hour=True), id="hour true"),
    pytest.param(proposal(hour="9"), id="hour of another type"),
    pytest.param(proposal(hour=None), id="hour of null"),
    pytest.param(proposal(confidence=0), id="confidence 0"),
    pytest.param(proposal(confidence=6), id="confidence 6"),
    pytest.param(proposal(confidence=4.5), id="confidence with a fraction"),
    pytest.param(proposal(confidence=True), id="confidence true"),
    pytest.param(proposal(confidence="4"), id="confidence of another type"),
    pytest.param(proposal(confidence=[]), id="confidence a list"),
    pytest.param(proposal(status="proposes"), id="status outside its four words"),
    pytest.param(proposal(status="Approved"), id="status with a capital letter"),
    pytest.param(proposal(status=None), id="status of null"),
    pytest.param(proposal(status=1), id="status of another type"),
]


@pytest.mark.parametrize("instance", PROPOSAL_VALID, ids=lambda instance: json.dumps(instance)[:70])
def test_the_schema_and_the_reader_agree_on_every_accepted_proposal(
    instance: dict[str, Any],
) -> None:
    assert accepted(PROPOSAL, instance)

    read = proposals.Proposal.parse(instance)

    assert read.as_dict() == instance


@pytest.mark.parametrize("instance", PROPOSAL_REFUSED)
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


def test_a_proposal_is_written_with_its_fields_in_the_schemas_order() -> None:
    with_steps = proposals.Proposal.parse(proposal(steps=[A_STEP])).as_dict()
    without_steps = proposals.Proposal.parse(proposal()).as_dict()

    assert list(with_steps) == list(PROPOSAL["properties"])
    assert list(without_steps) == [field for field in PROPOSAL["properties"] if field != "steps"]


def test_a_number_written_as_a_float_is_read_as_the_integer_it_is() -> None:
    read = proposals.Proposal.parse(
        proposal(days=3.0, hour=9.0, confidence=4.0, sessions={A_DAY: 2.0})
    )

    numbers = [read.days, read.hour, read.confidence, *read.sessions.values()]

    assert numbers == [3, 9, 4, 2]
    assert all(type(number) is int for number in numbers)


def test_the_reader_holds_the_schemas_own_patterns_and_numbers() -> None:
    """The reader searches with the patterns the schemas write, and bounds by their numbers.
    An instance can show only that the two agree on that instance."""
    flow_properties = FLOW["properties"]
    proposal_properties = PROPOSAL["properties"]
    flow_step = flow_properties["steps"]["items"]["properties"]
    proposal_step = proposal_properties["steps"]["items"]["properties"]
    day_names = proposal_properties["sessions"]["propertyNames"]
    name = proposals.NAME_SHAPE.pattern
    site = proposals.SITE_SHAPE.pattern

    assert name == flow_properties["name"]["pattern"]
    assert name == proposal_properties["name"]["pattern"]
    assert site == flow_properties["sites"]["items"]["pattern"]
    assert site == proposal_properties["sites"]["items"]["pattern"]
    assert site == flow_step["site"]["pattern"]
    assert site == proposal_step["site"]["pattern"]
    assert proposals.DAY_SHAPE.pattern == day_names["pattern"]
    assert proposals.TIME_SHAPE.pattern == proposal_properties["first_seen"]["pattern"]
    assert proposals.TIME_SHAPE.pattern == proposal_properties["last_seen"]["pattern"]

    # One row for every place a schema writes a bound.
    bounds = [
        (proposals.GOAL_AT_MOST, flow_properties["goal"]["maxLength"]),
        (proposals.GOAL_AT_MOST, proposal_properties["goal"]["maxLength"]),
        (proposals.SITES_AT_MOST, flow_properties["sites"]["maxItems"]),
        (proposals.SITES_AT_MOST, proposal_properties["sites"]["maxItems"]),
        (proposals.STEPS_AT_MOST, flow_properties["steps"]["maxItems"]),
        (proposals.STEPS_AT_MOST, proposal_properties["steps"]["maxItems"]),
        (proposals.INTENT_AT_MOST, flow_step["intent"]["maxLength"]),
        (proposals.INTENT_AT_MOST, proposal_step["intent"]["maxLength"]),
        (proposals.DAYS_AT_MOST, proposal_properties["sessions"]["maxProperties"]),
    ]

    assert all(reader == written for reader, written in bounds), bounds
    assert list(proposals.ORDERS) == proposal_properties["order"]["enum"]
    assert list(proposals.STATUSES) == proposal_properties["status"]["enum"]


def test_a_flows_name_is_under_the_rule_of_a_saved_flow() -> None:
    """The schema says so in words. Here is what holds the two together."""
    every_dialect_end = "(?![\\s\\S])"

    assert proposals.NAME_SHAPE.pattern == flows.NAME.pattern.removesuffix("$") + every_dialect_end


def test_a_site_and_a_second_are_the_same_in_every_message() -> None:
    """The flow and the proposal write a site and a second as the activity event does. The
    miner carries both from one to the others, and nothing translates them."""
    event = ACTIVITY_EVENT["properties"]

    assert proposals.SITE_SHAPE.pattern == event["site"]["pattern"]
    assert proposals.TIME_SHAPE.pattern == event["ts"]["pattern"]
