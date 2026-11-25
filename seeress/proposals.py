"""A flow and a proposal: what the miner found, and what the compiler is given.

The miner counts. A proposal is what it counted, in the shape of a flow: the sites, in order,
and with them the evidence. On how many days and in how many sessions the pattern was seen,
when it was first and last seen, and at what hour it usually starts. Two fields of a proposal
are not counted. Its goal is a model's one sentence on what the task may be, until the user
writes the goal in their own words. Its confidence is the model's, from 1 to 5, or nothing
when no model was asked.

A flow is what the compiler is given: a name, the goal, and the sites. Nothing else of the
activity record goes into it.

The shapes are contracts/flow.schema.json and contracts/flow-proposal.schema.json. Flow.parse
and Proposal.parse are those schemas written by hand, and a contract test keeps them in
agreement.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The patterns of the schemas, as they write them, searched for as their validator searches
# them. A pattern that must end at the end says so with `(?![\s\S])`, which means the same in
# Python and in the dialect JSON Schema names. `$` does not: in Python it also holds before a
# final newline.
NAME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}(?![\s\S])")
SITE_SHAPE = re.compile(r"^(?!www\.)[a-z0-9._-]+(?![\s\S])")
DAY_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?![\s\S])")
TIME_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?![\s\S])")

ORDERS: tuple[str, ...] = ("fixed", "varies")
STATUSES: tuple[str, ...] = ("proposed", "approved", "compiled", "dismissed")

# The most a field holds, as the schemas say. Each is a number of its own there.
GOAL_AT_MOST = 1000
SITES_AT_MOST = 50
STEPS_AT_MOST = 50
INTENT_AT_MOST = 200
DAYS_AT_MOST = 366

# A name is 40 characters at the most. The last eight are digits of a digest, after a
# hyphen, and the rest show the sites.
DIGEST_DIGITS = 8
SHOWN_AT_MOST = 40 - 1 - DIGEST_DIGITS

FLOW_FIELDS = frozenset({"name", "goal", "sites"})
PROPOSAL_FIELDS = FLOW_FIELDS | {
    "order",
    "days",
    "sessions",
    "first_seen",
    "last_seen",
    "hour",
    "confidence",
    "status",
}
STEP_FIELDS = frozenset({"site", "intent"})

# The one field of a flow, and of a proposal, that may be left out.
OPTIONAL = frozenset({"steps"})


@dataclass(frozen=True)
class Step:
    """One site of a flow, and what is done on it."""

    site: str
    intent: str

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the step, with its two fields in the schema's order."""
        return {"site": self.site, "intent": self.intent}


def _matches(shape: re.Pattern[str], value: object) -> bool:
    """True when value is a string that holds the shape where the schema looks for it."""
    return isinstance(value, str) and shape.search(value) is not None


def _text(value: object, least: int, most: int) -> bool:
    """True for a string of least to most characters."""
    return isinstance(value, str) and least <= len(value) <= most


def _integer(value: object, least: int, most: int | None = None) -> bool:
    """True for a number that is an integer from least to most, as JSON counts.

    JSON has one kind of number, so 3.0 is the integer 3. True equals 1 in Python, and is no
    number in JSON.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return value >= least and (most is None or value <= most)


def _fields(
    data: object, required: frozenset[str], optional: frozenset[str]
) -> dict[str, Any] | None:
    """The fields of data, or None when data is not an object that holds every required field
    and no field beyond the optional ones."""
    if (
        not isinstance(data, dict)
        or not required <= set(data)
        or not set(data) <= required | optional
    ):
        return None
    return data


def _sites(value: object) -> tuple[str, ...] | None:
    """The sites of a flow or a proposal, or None when the schema would refuse them."""
    if not isinstance(value, list) or not 1 <= len(value) <= SITES_AT_MOST:
        return None
    if not all(_matches(SITE_SHAPE, site) for site in value):
        return None
    return None if len(set(value)) != len(value) else tuple(value)


def _steps(value: object) -> tuple[Step, ...] | None:
    """The steps of a flow or a proposal, or None when the schema would refuse them."""
    if not isinstance(value, list) or not 1 <= len(value) <= STEPS_AT_MOST:
        return None
    steps: list[Step] = []
    for step in value:
        fields = _fields(step, STEP_FIELDS, frozenset())
        if fields is None or not (
            _matches(SITE_SHAPE, fields["site"]) and _text(fields["intent"], 1, INTENT_AT_MOST)
        ):
            return None
        steps.append(Step(fields["site"], fields["intent"]))
    return tuple(steps)


def _sessions(value: object) -> dict[str, int] | None:
    """How many sessions a pattern was seen in on each day, or None when the schema would
    refuse them. The days come as they were written; as_dict writes them in order."""
    if not isinstance(value, dict) or not 1 <= len(value) <= DAYS_AT_MOST:
        return None
    if not all(_matches(DAY_SHAPE, day) and _integer(count, 1) for day, count in value.items()):
        return None
    return {day: int(count) for day, count in value.items()}


def _read_flow_fields(
    fields: Mapping[str, Any], goal_least: int
) -> tuple[str, str, tuple[str, ...], tuple[Step, ...] | None] | None:
    """The name, the goal, the sites and the steps in the fields of a flow, or None when the
    schema would refuse any of them. The goal of a proposal may be empty, so the least length
    it may have comes in."""
    name, goal = fields["name"], fields["goal"]
    sites = _sites(fields["sites"])
    steps = None if "steps" not in fields else _steps(fields["steps"])
    if sites is None or ("steps" in fields and steps is None):
        return None
    if not (_matches(NAME_SHAPE, name) and _text(goal, goal_least, GOAL_AT_MOST)):
        return None
    return name, goal, sites, steps


@dataclass(frozen=True)
class Flow:
    """What the compiler is given: a name, the goal, and the sites, in order.

    steps is None until someone has written what is done on each site.
    """

    name: str
    goal: str
    sites: tuple[str, ...]
    steps: tuple[Step, ...] | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the flow, with its fields in the schema's order."""
        flow: dict[str, object] = {
            "name": self.name,
            "goal": self.goal,
            "sites": list(self.sites),
        }
        if self.steps is not None:
            flow["steps"] = [step.as_dict() for step in self.steps]
        return flow

    @classmethod
    def parse(cls, data: object) -> "Flow":
        """The flow in data. Raises ValueError when the schema would refuse data."""
        # The shape is contracts/flow.schema.json; a contract test keeps this reader and that
        # schema in agreement.
        fields = _fields(data, FLOW_FIELDS, OPTIONAL)
        read = None if fields is None else _read_flow_fields(fields, 1)
        if fields is None or read is None:
            raise ValueError("the flow has an unexpected shape")
        name, goal, sites, steps = read
        return cls(name, goal, sites, steps)


@dataclass(frozen=True)
class Proposal:
    """A flow with the evidence for it, and with where it stands.

    The miner writes every field but two. The goal is a model's sentence until the user writes
    their own, which approves the proposal. The confidence is the model's, or None.
    """

    name: str
    goal: str
    sites: tuple[str, ...]
    order: str
    days: int
    sessions: Mapping[str, int]
    first_seen: str
    last_seen: str
    hour: int
    confidence: int | None
    status: str
    steps: tuple[Step, ...] | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the proposal, with its fields in the schema's order."""
        proposal: dict[str, object] = {
            "name": self.name,
            "goal": self.goal,
            "sites": list(self.sites),
        }
        if self.steps is not None:
            proposal["steps"] = [step.as_dict() for step in self.steps]
        proposal["order"] = self.order
        proposal["days"] = self.days
        proposal["sessions"] = dict(sorted(self.sessions.items()))
        proposal["first_seen"] = self.first_seen
        proposal["last_seen"] = self.last_seen
        proposal["hour"] = self.hour
        proposal["confidence"] = self.confidence
        proposal["status"] = self.status
        return proposal

    @classmethod
    def parse(cls, data: object) -> "Proposal":
        """The proposal in data. Raises ValueError when the schema would refuse data."""
        # The shape is contracts/flow-proposal.schema.json; a contract test keeps this reader
        # and that schema in agreement.
        fields = _fields(data, PROPOSAL_FIELDS, OPTIONAL)
        read = None if fields is None else _read_flow_fields(fields, 0)
        if fields is None or read is None:
            raise ValueError("the proposal has an unexpected shape")
        name, goal, sites, steps = read
        order, status = fields["order"], fields["status"]
        days, hour, confidence = fields["days"], fields["hour"], fields["confidence"]
        first_seen, last_seen = fields["first_seen"], fields["last_seen"]
        per_day = _sessions(fields["sessions"])
        if not (
            order in ORDERS
            and status in STATUSES
            and _integer(days, 1)
            and per_day is not None
            and _matches(TIME_SHAPE, first_seen)
            and _matches(TIME_SHAPE, last_seen)
            and _integer(hour, 0, 23)
            and (confidence is None or _integer(confidence, 1, 5))
        ):
            raise ValueError("the proposal has an unexpected shape")
        return cls(
            name,
            goal,
            sites,
            order,
            int(days),
            per_day,
            first_seen,
            last_seen,
            int(hour),
            None if confidence is None else int(confidence),
            status,
            steps,
        )


def name_for(sites: Sequence[str]) -> str:
    """The name of the proposal for a sequence of sites, which is the name of its flow.

    The same sites always have the same name, and two sequences that differ never share
    one. Each site is written with its dots and its underscores as hyphens, the sites are
    joined by a hyphen and cut, and eight hexadecimal digits of a digest of the sites
    follow. So the name is one a flow may have, whatever the sites.
    """
    shown = "-".join(site.replace(".", "-").replace("_", "-") for site in sites)
    shown = re.sub(r"[^a-z0-9-]", "", shown).lstrip("-")[:SHOWN_AT_MOST].rstrip("-")
    digest = hashlib.sha256("\n".join(sites).encode("utf-8")).hexdigest()[:DIGEST_DIGITS]
    return f"{shown or 'sites'}-{digest}"
