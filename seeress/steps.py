"""A sitting as steps of a task, and the sequences of steps that recur across days.

The search on sites (seeress.miner) finds work that moves between sites. This finds work
that stays inside one: the same clicks, inputs and submits in the same order, day after day.
A person who copies from one place and pastes into another, in two tabs of one system,
leaves three alike events for every round.

Nothing in this module reads a file, reads the clock or asks a model. A step is shown to the
person as a page with its identifiers hidden and an element with every digit as #. A model is
told the kinds of the steps and nothing more of them. Nothing of a step is remembered with a
proposal.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from seeress import miner
from seeress.activity import Event
from seeress.miner import Candidate, Session, Step, Unit

# A sequence shorter than this says nothing about a task, and a longer one is several.
LEAST_STEPS, MOST_STEPS = 2, 12

# A round is from LEAST_ROUND to MOST_ROUND steps, one after the other, LEAST_ROUNDS times or
# more. Twice may be a coincidence. Three times is a rhythm.
LEAST_ROUND, MOST_ROUND = 2, 6
LEAST_ROUNDS = 3

# What tells the parts of a key apart. No site, no path and no name of an element holds it.
APART = "\x1f"

# The most of an element that is shown.
ELEMENT_AT_MOST = 80

_DIGIT = re.compile("[0-9]")


@dataclass
class _Made:
    """A step of a sitting while the steps are made: what was done, and every event of it."""

    kind: str
    site: str
    path: str
    element: str
    field: str | None
    events: list[Event]
    moved: int

    @property
    def key(self) -> str:
        """What must be alike in two sittings for two steps to be the same one."""
        return APART.join((self.site, self.path, self.kind, self.element, self.field or ""))


def of(sitting: Session) -> list[Unit]:
    """The sitting as its steps, in order.

    Every click, input, submit and select is a step. An event with no action, a navigation or
    a focus line of the older logger, is none. The same step twice in a row is one step, and
    a round of steps done three times or more in a row is one round: each of its steps holds
    the events of every time.
    """
    made: list[_Made] = []
    before: str | None = None
    for event in sitting.events:
        step = _of(event)
        moved = before is not None and event.target != before
        before = event.target
        if step is None:
            continue
        if made and made[-1].key == step.key:
            made[-1].events.append(event)
            made[-1].moved += moved
        else:
            step.events = [event]
            step.moved = int(moved)
            made.append(step)
    return [_unit(one) for one in _once_a_round(made)]


def recurring(sittings: Sequence[Session], days: int = 3) -> list[Candidate]:
    """The sequences of two to twelve steps that occur in sittings on at least days different
    days, most days first, each with its steps as evidence.

    The search is the one on sites (seeress.miner.recurring), on the steps of each sitting.
    """
    return miner.recurring(sittings, days, units=of, sizes=(LEAST_STEPS, MOST_STEPS))


def beside(sites: Sequence[Candidate], on_steps: Sequence[Candidate]) -> list[Candidate]:
    """The candidates found on sites and those found on steps, strongest first.

    A sequence of sites is left out when a sequence of steps has its sites, in its order and
    with a site that comes again right after itself counted once, and was seen on as many
    days: the steps say all the sites say, and more. A ritual whose order varies is never
    left out. The steps say one order, and the ritual says there is more than one.
    """
    kept = [
        candidate
        for candidate in sites
        if candidate.order != "fixed"
        or not any(
            step.days >= candidate.days and miner.once(step.sites) == candidate.sites
            for step in on_steps
        )
    ]
    return miner.by_strength([*kept, *on_steps])


def lines(found: Sequence[Step]) -> list[str]:
    """The lines that show steps, the first of them after `steps:`.

    A line says what was done, the page, the element and at most ELEMENT_AT_MOST of it, the
    kind of a field, how many events of one sitting the step stood for when it was more than
    one, and `other tab` when in most sittings it was done in another tab than the step
    before it.
    """
    shown: list[str] = []
    for number, step in enumerate(found, start=1):
        said = f"{number}. {step.kind}  {step.path}  {step.element[:ELEMENT_AT_MOST]}"
        if step.field:
            said += f"  {step.field}"
        if step.repeats > 1:
            said += f"  ×{step.repeats}"  # noqa: RUF001 - the sign of times, not a letter x
        if step.other_tab:
            said += "  other tab"
        shown.append(("  steps:  " if number == 1 else "          ") + said)
    return shown


def _of(event: Event) -> _Made | None:
    """The event as a step not yet counted, or None for an event that is no step.

    What makes one step the same as another from one day to another: the site, the page with
    its identifiers hidden, what was done and the element with every digit as #.
    """
    action = event.action
    if action is None:
        return None
    return _Made(
        action.type,
        event.site,
        miner.masked(event.path),
        _DIGIT.sub("#", action.fingerprint),
        action.field,
        [],
        0,
    )


def _unit(one: _Made) -> Unit:
    """The step as a unit of the search, with what is shown of it.

    A step was done in another tab than the event before it in more of its events than not,
    or it was not."""
    return Unit(
        one.key,
        one.site,
        tuple(one.events),
        Step(
            one.kind,
            one.site,
            one.path,
            one.element,
            one.field,
            len(one.events),
            2 * one.moved > len(one.events),
        ),
    )


def _once_a_round(made: list[_Made]) -> list[_Made]:
    """The steps with every round written once.

    A round that starts at a step is the shortest of two to six steps that comes again right
    after itself, three times or more. Its steps are one set, and each holds the events of
    every time. What is no round is left as it is."""
    out: list[_Made] = []
    at = 0
    while at < len(made):
        size, times = _round_at(made, at)
        if times == 0:
            out.append(made[at])
            at += 1
            continue
        for offset in range(size):
            one = made[at + offset]
            for again in made[at + size + offset : at + size * times : size]:
                one.events.extend(again.events)
                one.moved += again.moved
            out.append(one)
        at += size * times
    return out


def _round_at(made: list[_Made], at: int) -> tuple[int, int]:
    """The round that starts at at: how many steps it has and how many times it comes. No
    round, and the length says which was looked for last."""
    for size in range(LEAST_ROUND, MOST_ROUND + 1):
        if at + size * LEAST_ROUNDS > len(made):
            return size, 0
        keys = [one.key for one in made[at : at + size]]
        times = 1
        while [one.key for one in made[at + times * size : at + (times + 1) * size]] == keys:
            times += 1
        if times >= LEAST_ROUNDS:
            return size, times
    return MOST_ROUND, 0
