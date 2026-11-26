"""A sitting as steps of a task, and the sequences of steps that recur across days.

The search on sites (seeress.miner) finds work that moves between sites. This finds work
that stays inside one: the same clicks, inputs and submits in the same order, day after day.
A person who copies from one place and pastes into another, in two tabs of one system,
leaves three alike events for every round.

Nothing in this module reads a file, reads the clock or asks a model. A step is shown to the
person as a page with its identifiers hidden and an element with every digit as #, both as
data and never as a terminal reads them. A model is told the kinds of the steps and nothing
more of them. Nothing of a step is remembered with a proposal.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from seeress import miner
from seeress.activity import Event
from seeress.miner import Candidate, Session, Step, Unit

# A sequence shorter than this says nothing about a task. Fifty is the most a task may hold to
# be one proposal: a longer one is proposed in windows of fifty, each with a name of its own.
LEAST_STEPS, MOST_STEPS = 2, 50

# A round is from LEAST_ROUND to MOST_ROUND steps, one after the other, LEAST_ROUNDS times or
# more. Twice may be a coincidence. Three times is a rhythm.
LEAST_ROUND, MOST_ROUND = 2, 6
LEAST_ROUNDS = 3

# The most of an element or of a page that is shown.
ELEMENT_AT_MOST = 80

_DIGIT = re.compile("[0-9]")

# Four letters that show nothing: the fillers of Hangul. A reader would take them for a space.
INVISIBLE = frozenset("\u115f\u1160\u3164\uffa0")


@dataclass
class _Made:
    """A step of a sitting while the steps are made: what was done, and every event of it.

    key is what must be alike in two sittings for two steps to be the same one: the site,
    the page with its identifiers hidden, what was done, the element with every digit as #,
    and the kind of a field. It is written as JSON writes them: a page or a name may hold
    any character, and two different steps never share a key.
    """

    key: str
    kind: str
    site: str
    path: str
    element: str
    field: str | None
    events: list[Event]
    moved: int


def of(sitting: Session) -> list[Unit]:
    """The sitting as its steps, in order.

    Every click, input, submit and select is a step. An event with no action, a navigation or
    a focus line of the older logger, is none, and says nothing of where a step was done: a
    step is in another tab when its tab is not the tab of the step before it.

    The same step twice in a row is one step, and a round of steps done three times or more
    in a row is one round: each of its steps holds the events of every time.
    """
    made: list[_Made] = []
    before: str | None = None
    for event in sitting.events:
        step = _of(event)
        if step is None:
            continue
        moved = before is not None and event.target != before
        before = event.target
        if made and made[-1].key == step.key:
            made[-1].events.append(event)
            made[-1].moved += moved
        else:
            step.events = [event]
            step.moved = int(moved)
            made.append(step)
    return [_unit(one) for one in _once_a_round(made)]


def recurring(sittings: Sequence[Session], days: int = 3) -> list[Candidate]:
    """The sequences of two to fifty steps that occur in sittings on at least days different
    days, most days first, each with its steps as evidence.

    The search is the one on sites (seeress.miner.recurring), on the steps of each sitting.
    A task longer than fifty steps is one proposal, by its first fifty steps, and says that
    it goes on.
    """
    return miner.recurring(
        sittings, days, units=of, sizes=(LEAST_STEPS, MOST_STEPS), one_window_a_run=True
    )


def beside(site_candidates: Sequence[Candidate], on_steps: Sequence[Candidate]) -> list[Candidate]:
    """The candidates found on sites and those found on steps, strongest first.

    A sequence of sites is left out when a sequence of steps says all it says, and more. A
    ritual whose order varies is never left out. The steps say one order, and the ritual says
    there is more than one.
    """
    kept = [
        candidate
        for candidate in site_candidates
        if candidate.order != "fixed"
        or not any(_says_wholly(found, candidate) for found in on_steps)
    ]
    return miner.by_strength([*kept, *on_steps])


def _says_wholly(found: Candidate, of_sites: Candidate) -> bool:
    """Whether a sequence of steps says all a sequence of sites says, and more: the same sites
    in the same order, with a site that comes again right after itself counted once, and seen
    on as many days and in at least as many sittings. Seen on as many days and in fewer
    sittings, the sites say how often the work is done, and the steps do not."""
    return (
        found.days >= of_sites.days
        and sum(found.sessions.values()) >= sum(of_sites.sessions.values())
        and miner.once(found.sites) == of_sites.sites
    )


def lines(found: Sequence[Step]) -> list[str]:
    """The lines that show steps, the first of them after `steps:`.

    A line says what was done, the page, the element and at most ELEMENT_AT_MOST of it, the
    kind of a field, how many events of one sitting the step stood for when it was more than
    one, and `other tab` when in most sittings it was done in another tab than the step
    before it. The page and the element are what a page chose, and are shown as `shown`
    shows them: as data, and never as a terminal reads them.
    """
    made: list[str] = []
    for number, step in enumerate(found, start=1):
        said = f"{number}. {step.kind}  {shown(step.path)}  {shown(step.element)}"
        if step.field:
            said += f"  {step.field}"
        if step.repeats > 1:
            said += f"  ×{step.repeats}"  # noqa: RUF001 - the sign of times, not a letter x
        if step.other_tab:
            said += "  other tab"
        made.append(("  steps:  " if number == 1 else "          ") + said)
    return made


def shown(text: str) -> str:
    """What a page chose, as it may be shown.

    Every character that is neither printable ASCII nor a letter or a digit is written as its
    escape: a control, a format mark, a combining mark. So a name can add no line to a
    report and drive no terminal, and a letter is never shown as another sign. The four
    letters that show nothing are written as their escape too. Above U+FFFF an escape has
    eight hexadecimal digits, so that it reads as one character. At most ELEMENT_AT_MOST
    characters are shown, and an escape is never cut.

    A letter of another script may look like another letter, and is shown as it is: it is
    the name the page gave.
    """
    written = ""
    for character in text:
        plain = (" " <= character <= "~" or character.isalnum()) and character not in INVISIBLE
        if plain:
            part = character
        elif ord(character) <= 0xFFFF:
            part = f"\\u{ord(character):04x}"
        else:
            part = f"\\U{ord(character):08x}"
        if len(written) + len(part) > ELEMENT_AT_MOST:
            break
        written += part
    return written


def _of(event: Event) -> _Made | None:
    """The event as a step not yet counted, or None for an event that is no step.

    What makes one step the same as another from one day to another: the site, the page with
    its identifiers hidden, what was done and the element with every digit as #.
    """
    action = event.action
    if action is None:
        return None
    path, element = miner.masked(event.path), _DIGIT.sub("#", action.fingerprint)
    return _Made(
        json.dumps([event.site, path, action.type, element, action.field or ""], ensure_ascii=True),
        action.type,
        event.site,
        path,
        element,
        action.field,
        [],
        0,
    )


def _unit(one: _Made) -> Unit:
    """The step as a unit of the search, with what is shown of it.

    A step was done in another tab than the step before it in more of its events than not,
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
