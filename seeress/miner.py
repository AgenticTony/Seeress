"""Finding what repeats in the activity record, and where the record has holes.

Three things are found here, and all three are counted. Nothing in this module reads a
file, reads the clock or asks a model: everything it needs comes in as an argument. What it
gives out is a site, a path with its identifiers hidden, the steps of a task, an hour and
counts.

- A session is one sitting: a run of events with no hole in it and no outage.
- A candidate is a sequence that turns up in sittings on different days: of two to six
  sites, or, for seeress.steps, of the steps of a task. A pattern repeated inside one
  sitting is not a workflow.
- A gap is time in the schedule for which the record holds nothing, or an outage of the
  logger.
"""

import json
import re
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from seeress.activity import (
    DEFAULT_SCHEDULE as DEFAULT_SCHEDULE,
)  # re-exported: the miner's schedule
from seeress.activity import TS_FORMAT, Event, Interval, epoch
from seeress.activity import Schedule as Schedule  # re-exported
from seeress.errors import MiningError
from seeress.proposals import Proposal, name_for

# The hole in the record that may pass between two events of one sitting, in seconds.
SESSION_GAP = 1800

# A sequence shorter than this says nothing about a task, and a longer one is several.
LEAST_SITES, MOST_SITES = 2, 6

# How many paths a step of a candidate shows.
PATHS_SHOWN = 3

# What a segment of a path that is an identifier is shown as.
HIDDEN = "{}"


def _placed(events: Sequence[Event]) -> list[tuple[float, Event]]:
    """The events in time order, each with when it happened.

    Events that share a second keep the order they arrived in. An event whose ts is no time
    cannot be placed, and is left out: the schema judges the shape of a ts, not whether it
    is one.
    """
    placed: list[tuple[float, Event]] = []
    for event in sorted(events, key=lambda event: event.ts):
        try:
            placed.append((epoch(event.ts), event))
        except (ValueError, OverflowError):
            continue
    return placed


# ------------------------------------------------------ the minutes of an agent ---


def without_runs(events: Sequence[Event], runs: Sequence[Interval]) -> list[Event]:
    """The events that fall in none of the runs, in the order they came.

    A run is an agent's, or a flow's. What happened in the browser while one ran was not
    the person's doing.
    """
    return [event for event in events if not _in_a_run(event, runs)]


def _in_a_run(event: Event, runs: Sequence[Interval]) -> bool:
    try:
        when = epoch(event.ts)
    except (ValueError, OverflowError):
        return False
    return any(run.holds(when) for run in runs)


# ------------------------------------------------------------------- sessions ---


@dataclass(frozen=True)
class Session:
    """One sitting: a run of events with no hole in it and no outage.

    day is the date of the first event, and hour the hour of the first event. start and end
    are the ts of the first event and of the last.
    """

    events: tuple[Event, ...]
    day: str
    hour: int
    start: str
    end: str


def sessions(
    events: Sequence[Event], gap: float = SESSION_GAP, *, outages: Sequence[Interval] = ()
) -> list[Session]:
    """The sittings in the events, oldest first.

    A new sitting starts at an event when the event before it is more than gap seconds
    earlier, or when an outage starts between the two. An outage cuts even when it is
    shorter than the gap: the logger was not recording, and what came after is not the same
    sitting.
    """
    cuts = sorted(outage.start for outage in outages if not outage.empty)
    groups: list[list[Event]] = []
    before: float | None = None
    for when, event in _placed(events):
        if before is None or when - before > gap or _outage_starts(before, when, cuts):
            groups.append([])
        groups[-1].append(event)
        before = when
    return [_session(group) for group in groups]


def _outage_starts(before: float, after: float, cuts: Sequence[float]) -> bool:
    """True when one of the sorted starts falls from before to after, both included."""
    next_start = bisect_left(cuts, before)
    return next_start < len(cuts) and cuts[next_start] <= after


def _session(events: list[Event]) -> Session:
    first, last = events[0], events[-1]
    return Session(tuple(events), first.ts[:10], int(first.ts[11:13]), first.ts, last.ts)


# ------------------------------------------------------ recurrence across days ---


@dataclass(frozen=True)
class Step:
    """One step of a sequence of steps that recurs, as it is shown to the person.

    kind is click, input, submit or select. path has its identifiers hidden. element is how
    the element is told apart, with every digit as #. field is the kind of a field an input
    went into, and None for every other step. repeats is how many events of one sitting the
    step stood for, most often. other_tab says that in most sittings the step was done in
    another tab than the step before it. Nothing here says what a field held. Nothing here is
    sent to a model or remembered with a proposal.
    """

    kind: str
    site: str
    path: str
    element: str
    field: str | None
    repeats: int
    other_tab: bool


@dataclass(frozen=True)
class Unit:
    """One thing that the sequence of a sitting is made of: a visit to a site, or a step.

    key is what must be alike in two sittings for two units to be the same one. events are
    the events of the sitting that the unit stands for, in order. step is what is shown of a
    step (seeress.steps), and None for a visit.
    """

    key: str
    site: str
    events: tuple[Event, ...]
    step: Step | None = None


@dataclass(frozen=True)
class Candidate:
    """A sequence that recurs across days, with the evidence for it.

    sites holds the site of every unit of the sequence, in order. paths holds one tuple per
    unit: the paths seen there, most common first and at most three, with their
    identifier-like segments replaced by {}. steps holds one Step per unit when the sequence
    is one of steps, and None when it is one of sites. keys is what was found alike from day
    to day: the sites, or the keys of the steps.

    order is "fixed" when the sites recur in this order, and "varies" when they recur as a
    set, in any order. further says that a block of the most steps is followed, in some
    sitting, by another such block: the task goes on, and its later steps are not shown.
    """

    sites: tuple[str, ...]
    paths: tuple[tuple[str, ...], ...]
    days: int
    sessions: Mapping[str, int]
    first_seen: str
    last_seen: str
    hour: int
    order: str = "fixed"
    keys: tuple[str, ...] | None = None
    steps: tuple[Step, ...] | None = None
    further: bool = False


def visits(sitting: Session) -> list[Unit]:
    """The sitting as one visit per run of events on the same site, in order.

    Consecutive repeats are collapsed, so `A A B A B C` holds the block `A B C`.
    """
    found: list[Unit] = []
    for event in sitting.events:
        if found and found[-1].site == event.site:
            found[-1] = Unit(event.site, event.site, (*found[-1].events, event))
        else:
            found.append(Unit(event.site, event.site, (event,)))
    return found


def once(sites: Sequence[str]) -> tuple[str, ...]:
    """The sites with a site that comes again right after itself left out once. A site that
    comes back after another is a return, and is kept."""
    return tuple(site for at, site in enumerate(sites) if at == 0 or site != sites[at - 1])


def recurring(
    sittings: Sequence[Session],
    days: int = 3,
    *,
    units: Callable[[Session], Sequence[Unit]] = visits,
    sizes: tuple[int, int] = (LEAST_SITES, MOST_SITES),
    one_window_a_run: bool = False,
) -> list[Candidate]:
    """The sequences, of the lengths given, that occur in sittings on at least days different
    days, most days first.

    A sitting becomes its sequence of units, and every block of the lengths given in it is
    looked for. A block occurs in a sitting when it appears anywhere in it, however many
    times. A block that another candidate holds as a contiguous block, and that was seen on
    at least as many days, is left out: a five-site sequence that recurs brings all its
    shorter parts with it.

    A block is looked for only where the block one shorter, at the same place, recurs: a
    sequence recurs only where its beginning does. So a long sequence costs no more than a
    short one, and the search holds few blocks at once.

    Where two candidates were seen on as many days, the one seen in more sittings comes
    first. Where those are equal too, the one whose sites come first alphabetically.

    With one_window_a_run, a block of the most steps that comes one step after another, in
    a sitting, is held: it is the same run seen one step on, and without this a run longer
    than the most gives one block for every step past it. It is held when the one before
    recurs on as many days; seen on more days, it is kept.
    """
    sequences = [units(sitting) for sitting in sittings]
    enough = _recurring(sequences, [sitting.day for sitting in sittings], days, sizes)
    occurrences: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for index, sequence in enumerate(sequences):
        keys = [unit.key for unit in sequence]
        for size in range(sizes[0], sizes[1] + 1):
            for position in range(len(keys) - size + 1):
                block = tuple(keys[position : position + size])
                if block in enough:
                    occurrences.setdefault(block, []).append((index, position))
    # The evidence is gathered for the sequences kept alone. In a record where a long run
    # recurs, every window of the run is found, and all but the longest are held.
    windows = _windows(occurrences, sizes[1]) if one_window_a_run else {}
    held = _held(occurrences, sittings, windows)
    kept = [
        _candidate(
            block,
            occurrences[block],
            sittings,
            sequences,
            further=len(block) == sizes[1]
            and any((index, at + 1) in windows for index, at in occurrences[block]),
        )
        for block in occurrences
        if block not in held
    ]
    return by_strength(kept)


def _recurring(
    sequences: Sequence[Sequence[Unit]],
    days_of: Sequence[str],
    days: int,
    sizes: tuple[int, int],
) -> set[tuple[str, ...]]:
    """Every block, of the lengths given, that occurs in sittings on at least days days.

    The blocks of the least length are all counted. A longer block is counted only where the
    block one shorter, at the same place, is one that recurs.
    """
    least, most = sizes
    keys = [[unit.key for unit in sequence] for sequence in sequences]
    enough: set[tuple[str, ...]] = set()
    for size in range(least, most + 1):
        seen_on: dict[tuple[str, ...], set[str]] = {}
        for index, sequence in enumerate(keys):
            for position in range(len(sequence) - size + 1):
                if size > least and tuple(sequence[position : position + size - 1]) not in enough:
                    continue
                block = tuple(sequence[position : position + size])
                seen_on.setdefault(block, set()).add(days_of[index])
        enough |= {block for block, seen in seen_on.items() if len(seen) >= days}
    return enough


def by_strength(candidates: Sequence[Candidate]) -> list[Candidate]:
    """The candidates most days first. Where two were seen on as many days, the one seen in
    more sittings. Where those are equal too, the one whose sites come first alphabetically.
    """
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.days,
            -sum(candidate.sessions.values()),
            candidate.sites,
        ),
    )


def _candidate(
    block: tuple[str, ...],
    where: Sequence[tuple[int, int]],
    sittings: Sequence[Session],
    sequences: Sequence[Sequence[Unit]],
    further: bool = False,
) -> Candidate:
    """The candidate for one block, with its evidence gathered from every occurrence.

    An occurrence starts at the first event of its first unit and ends at the last of its
    last. The evidence of a step is that of the first occurrence, with how many events the
    step stood for taken as it was most often, and with another tab when it was so in more
    sittings than not. A sitting says so when more of its occurrences than not do.
    """
    sittings_seen = {index for index, _ in where}
    per_day: Counter[str] = Counter(sittings[index].day for index in sittings_seen)
    starts: list[str] = []
    ends: list[str] = []
    hours: Counter[int] = Counter()
    paths: list[Counter[str]] = [Counter() for _ in block]
    lengths: list[Counter[int]] = [Counter() for _ in block]
    other_tabs: list[dict[int, list[bool]]] = [{} for _ in block]
    first = sequences[where[0][0]][where[0][1] : where[0][1] + len(block)]
    # A visit is shown by the paths seen in it. A step is one page already, and the path of
    # every event of it is not read again: in a month where one element is clicked and
    # clicked, that was most of the time of the search.
    on_steps = first[0].step is not None
    for index, position in where:
        chosen = sequences[index][position : position + len(block)]
        start, end = chosen[0].events[0].ts, chosen[-1].events[-1].ts
        starts.append(start)
        ends.append(end)
        hours[int(start[11:13])] += 1
        for at, unit in enumerate(chosen):
            if not on_steps:
                paths[at].update(masked(event.path) for event in unit.events)
            lengths[at][len(unit.events)] += 1
            if unit.step is not None:
                other_tabs[at].setdefault(index, []).append(unit.step.other_tab)
    # A sequence is all visits or all steps. When its first unit is a step, each is one.
    steps: list[Step] | None = None
    if first[0].step is not None:
        steps = []
        for at, unit in enumerate(first):
            step = unit.step
            if step is not None:
                steps.append(
                    replace(
                        step,
                        repeats=_least_often_tied(lengths[at]),
                        other_tab=_in_another_tab(other_tabs[at]),
                    )
                )
    return Candidate(
        tuple(unit.site for unit in first),
        tuple((unit.step.path,) for unit in first if unit.step is not None)
        if on_steps
        else tuple(_most_common(counted) for counted in paths),
        len(per_day),
        dict(sorted(per_day.items())),
        min(starts),
        max(ends),
        _most_common_hour(hours),
        "fixed",
        block,
        None if steps is None else tuple(steps),
        further,
    )


def _in_another_tab(saids: Mapping[int, Sequence[bool]]) -> bool:
    """Whether a step was done in another tab in more sittings than not. What a sitting
    says is one vote for each of its occurrences, and it says so when more of them than
    not do."""
    saying = sum(1 for votes in saids.values() if 2 * sum(votes) > len(votes))
    return 2 * saying > len(saids)


def _least_often_tied(counted: Counter[int]) -> int:
    """The number seen most often. Where two were seen as often, the smaller: it claims less."""
    return min(key for key, count in counted.items() if count == max(counted.values()))


def _most_common(counted: Counter[str]) -> tuple[str, ...]:
    """What was counted, most common first, alphabetically where counts are equal, and at
    most three."""
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    return tuple(path for path, _ in ranked[:PATHS_SHOWN])


def _most_common_hour(hours: Counter[int]) -> int:
    """The hour most occurrences start at. Where two tie, the earlier one."""
    return min(hour for hour, count in hours.items() if count == max(hours.values()))


def _windows(
    occurrences: Mapping[tuple[str, ...], Sequence[tuple[int, int]]], most: int
) -> dict[tuple[int, int], tuple[str, ...]]:
    """Where a block of the most steps starts: in which sitting, and at what step."""
    return {
        (index, position): block
        for block, where in occurrences.items()
        if len(block) == most
        for index, position in where
    }


def _held(
    occurrences: Mapping[tuple[str, ...], Sequence[tuple[int, int]]],
    sittings: Sequence[Session],
    windows: Mapping[tuple[int, int], tuple[str, ...]],
) -> set[tuple[str, ...]]:
    """The blocks that another one found holds as a contiguous block on at least as many days.

    Every proper block of a candidate is marked with the most days of a candidate that holds
    it. A block is held when its own is marked with as many days as its own. So each block
    is looked at with its blocks alone, and not with every other one: a record in which
    thousands of short sequences recur by coincidence is searched in the time of the
    thousands, and not of their square.

    The longest are looked at first, and a block already held on as many days as its own
    marks nothing: whatever holds it holds every block of it, on at least as many days. So a
    long run that recurs is not marked block by block for every window of it.
    """
    days_of = {
        block: len({sittings[index].day for index, _ in where})
        for block, where in occurrences.items()
    }
    marked: dict[tuple[str, ...], int] = {}
    held: set[tuple[str, ...]] = set()
    for block in sorted(days_of, key=lambda keys: (-len(keys), keys)):
        if marked.get(block, 0) >= days_of[block]:
            held.add(block)
            continue
        for size in range(2, len(block)):
            for position in range(len(block) - size + 1):
                part = block[position : position + size]
                if marked.get(part, 0) < days_of[block]:
                    marked[part] = days_of[block]
    # A block of the most steps that comes one step after another, in a sitting, is the same
    # run seen one step on. It is held when the one before it recurs on as many days: seen
    # on more days than the one before, it is kept, as any part seen on more days is.
    for (index, at), block in windows.items():
        before = windows.get((index, at - 1))
        if before is not None and before != block and days_of[before] >= days_of[block]:
            held.add(block)
    return held


# What makes one segment of a path an identifier: a number, a UUID, or eight or more
# hexadecimal digits. The patterns are matched whole, so they need no anchor.
_NUMBER = re.compile(r"[0-9]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{8,}")


def masked(path: str) -> str:
    """The path with every identifier-like segment replaced by {}.

    /invoices/7/top and /invoices/8/top are then one path, /invoices/{}/top. A candidate
    shows the shape of the work and not the record it was found in.
    """
    return "/".join(HIDDEN if _is_identifier(segment) else segment for segment in path.split("/"))


def _is_identifier(segment: str) -> bool:
    return any(shape.fullmatch(segment) for shape in (_NUMBER, _UUID, _HEX))


# ------------------------------------------------ rituals whose order varies ---


# The most sets of sites the search for clusters may hold at once. No record has come near
# it. It is there so that no record can keep the miner busy for good, and it says so.
CONSIDERED_AT_MOST = 2_000


def clusters(
    sittings: Sequence[Session], days: int = 3, *, considered_at_most: int = CONSIDERED_AT_MOST
) -> list[Candidate]:
    """The sets of two to six sites that turn up together in sittings on at least days
    different days, in any order, strongest first.

    A sitting holds a set when every site of the set is among its sites. Only a set that no
    larger found set holds whole is returned: a part of a ritual is not a ritual. So a group
    of more than six sites that always turn up together gives none. It is one thing, and too
    large to be one task, and it is not cut into pieces to make some. The sites come by how
    many of all the sittings hold each one, and alphabetically where two tie. The evidence is
    counted over the sittings that hold the whole set.

    How they are found. A found set is closed when every site that is in every sitting that
    holds the set belongs to it. The search starts from each site that recurs, closed. From
    a closed set it steps to a larger one by adding a site and closing again. A step that
    closes to more than six sites is dropped, because nothing above it can be a set of six
    or fewer that no larger set holds. A closed set that no site can be added to is one of
    the largest. Every set the search holds is closed, and two closed sets never hold the
    same sittings, which is what keeps a month of record from asking after every set there
    could be.

    Raises MiningError when the search holds more than considered_at_most sets at once.
    """
    if not sittings:
        return []
    # Which sittings hold each site, as one bit per sitting. The sittings that hold a set
    # are then those where the bits of all its sites agree, and a day counts when one of its
    # sittings is among them.
    holding: dict[str, int] = {}
    of_day: dict[str, int] = {}
    for index, sitting in enumerate(sittings):
        bit = 1 << index
        of_day[sitting.day] = of_day.get(sitting.day, 0) | bit
        for site in {visit.site for visit in sitting.events}:
            holding[site] = holding.get(site, 0) | bit
    day_bits = list(of_day.values())

    def found_often_enough(sitters: int) -> bool:
        return sum(1 for day in day_bits if day & sitters) >= days

    def closed(sitters: int) -> frozenset[str]:
        """The sites that every sitting among sitters holds. The sittings that hold all of
        them are exactly sitters."""
        return frozenset(site for site, holds in holding.items() if holds & sitters == sitters)

    # What is held: each closed set with the bits of the sittings that hold it, and the
    # largest so far.
    level: list[tuple[frozenset[str], int]] = []
    seen: set[frozenset[str]] = set()
    for sitters in sorted(holding.values()):
        if found_often_enough(sitters):
            whole = closed(sitters)
            if len(whole) <= MOST_SITES and whole not in seen:
                seen.add(whole)
                level.append((whole, sitters))
    largest: list[tuple[frozenset[str], int]] = []
    while level:
        following: list[tuple[frozenset[str], int]] = []
        for whole, sitters in level:
            added = False
            for site, holds in sorted(holding.items()):
                then = sitters & holds
                if site in whole or not found_often_enough(then):
                    continue
                added = True
                grown = closed(then)
                if len(grown) <= MOST_SITES and grown not in seen:
                    seen.add(grown)
                    following.append((grown, then))
            if not added:
                largest.append((whole, sitters))
        level = following
        if len(seen) > considered_at_most:
            raise MiningError(
                f"too many sets of sites come back together in this record: the search held"
                f" more than {considered_at_most}, and was stopped. Mine fewer hours:"
                " seeress mine --hours 168"
            )
    # A site on its own was only the first step towards a pair.
    return by_strength(
        [
            _cluster(whole, sitters, sittings, holding)
            for whole, sitters in largest
            if len(whole) >= LEAST_SITES
        ]
    )


def _cluster(
    whole: frozenset[str],
    sitters: int,
    sittings: Sequence[Session],
    holding: Mapping[str, int],
) -> Candidate:
    """The candidate for one found set, with its evidence counted over the sittings that
    hold it."""
    held = [sitting for index, sitting in enumerate(sittings) if sitters >> index & 1]
    per_day: Counter[str] = Counter(sitting.day for sitting in held)
    hours: Counter[int] = Counter()
    paths: dict[str, Counter[str]] = {site: Counter() for site in whole}
    for sitting in held:
        hours[_hour_of_first(sitting, whole)] += 1
        for event in sitting.events:
            if event.site in paths:
                paths[event.site][masked(event.path)] += 1
    ordered = sorted(whole, key=lambda site: (-holding[site].bit_count(), site))
    return Candidate(
        tuple(ordered),
        tuple(_most_common(paths[site]) for site in ordered),
        len(per_day),
        dict(sorted(per_day.items())),
        min(sitting.start for sitting in held),
        max(sitting.end for sitting in held),
        _most_common_hour(hours),
        "varies",
    )


def _hour_of_first(sitting: Session, whole: frozenset[str]) -> int:
    """The hour of the earliest event of one of the set's sites in the sitting."""
    return next(int(event.ts[11:13]) for event in sitting.events if event.site in whole)


# ------------------------------------------------------------------- the gaps ---


# The schedule is in seeress/activity.py: the capture records by it, the miner reads the
# record by it, and the settings name it.


@dataclass(frozen=True)
class Gap:
    """Time for which the record holds nothing.

    start and end are of the logger's form. minutes is rounded, so a gap of less than a
    minute has 0 and is still a gap. why is "down" when the logger wrote chrome_down, and
    "silent" when nothing was written: the logger was not running, the machine was off, or
    the person was away.
    """

    start: str
    end: str
    minutes: int
    why: str


def gaps(
    events: Sequence[Event],
    outages: Sequence[Interval],
    *,
    now: float,
    gap: float = SESSION_GAP,
    schedule: Schedule = DEFAULT_SCHEDULE,
) -> list[Gap]:
    """What the record is missing, oldest first.

    Every outage is returned whole, inside the schedule or not, as a gap of "down". A hole
    between two events longer than gap, and the time from the last event to now, are
    returned as gaps of "silent": one per day, with every outage taken out and only the
    part inside the schedule kept.

    The second of an event holds data. So a hole begins with the second after the last
    event, and ends with the second of the next. An outage ends with the second of the
    connected line, and the silence may resume there. An outage of no second is still
    returned, as a gap of 0 minutes, but it took no second from the silence around it.
    """
    down = sorted((outage for outage in outages if not outage.empty), key=lambda o: o.start)
    found = [_gap(outage.start, outage.end, "down") for outage in down]
    took = [outage for outage in down if outage.end > outage.start]
    moments = sorted({when for when, _ in _placed(events)})
    for start, end in zip(moments, [*moments[1:], now], strict=False):
        if end - start > gap:
            holes = _without_outages(Interval(start + 1, end), took)
            found.extend(
                _gap(part.start, part.end, "silent")
                for hole in holes
                for part in _in_schedule(hole, schedule)
            )
    return sorted(found, key=lambda found_gap: (found_gap.start, found_gap.end))


def _gap(start: float, end: float, why: str) -> Gap:
    """The gap between two moments, in seconds since the epoch by this machine's clock."""
    return Gap(_clock(start), _clock(end), round((end - start) / 60), why)


def _clock(when: float) -> str:
    """A moment in seconds since the epoch, in the logger's form."""
    return time.strftime(TS_FORMAT, time.localtime(when))


def _midnight(when: float) -> float:
    """The midnight of the day a moment falls on."""
    t = time.localtime(when)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _next_midnight(midnight: float) -> float:
    """The midnight after one, by the calendar and not by the clock: a day on which the
    clock changes does not have twenty-four hours."""
    t = time.localtime(midnight)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _without_outages(hole: Interval, outages: Sequence[Interval]) -> list[Interval]:
    """The parts of the hole, from its start to its end, that no outage covers.

    The outages are sorted by start and none is empty. A part ends where an outage begins,
    or where the hole does.
    """
    parts: list[Interval] = []
    cursor = hole.start
    for outage in outages:
        if outage.end <= cursor:
            continue
        if outage.start > cursor:
            parts.append(Interval(cursor, min(outage.start, hole.end)))
        cursor = outage.end
        if cursor >= hole.end:
            return parts
    parts.append(Interval(cursor, hole.end))
    return parts


def _in_schedule(span: Interval, schedule: Schedule) -> list[Interval]:
    """The parts of the span that fall in the schedule, one per day.

    A part begins at an hour and ends at the next whole hour. On a day when the clock
    changes, the hours move with it by that hour.
    """
    parts: list[Interval] = []
    day = _midnight(span.start)
    while day <= span.end:
        if time.localtime(day).tm_wday in schedule.days:
            start = max(span.start, day + schedule.start * 3600)
            end = min(span.end, day + (schedule.end + 1) * 3600)
            if start < end:
                parts.append(Interval(start, end))
        day = _next_midnight(day)
    return parts


def scheduled(start: float, end: float, schedule: Schedule = DEFAULT_SCHEDULE) -> float:
    """How many seconds of the time from start to end fall in the schedule."""
    return sum(part.end - part.start for part in _in_schedule(Interval(start, end), schedule))


# ----------------------------------------------------------------- proposals ---

# A model, or whatever stands in for one: a prompt goes in, its answer comes out.
Ask = Callable[[str], str]

# The most of a model's sentence that is kept.
SENTENCE_AT_MOST = 300

# A run of text shaped like a host name. A site may hold an underscore, and so may it.
HOST = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)+")

# What a sentence of a model may not hold, whatever else it says.
UNWANTED = ("www.", "http", "@")


def prompt_for(candidates: Sequence[Candidate]) -> str:
    """One prompt for every candidate: what a model may see of them, and the answer asked
    for.

    A model may see a site, an hour, counts and the kinds of the steps of a task, and never
    a path or an element. The answer is JSON and nothing else: a list with one object for
    each number, holding its number, one sentence on what the task may be, and a confidence
    from 1 to 5.
    """
    lines = [
        "A person used a browser at work. Below are groups of sites that came up together",
        "again and again, on different days. For each group, say in one sentence what task",
        "it may be part of, and how sure you are that it is one repeated task and not",
        "coincidence.",
        "",
    ]
    for number, candidate in enumerate(candidates, start=1):
        order = "the order varies" if candidate.order == "varies" else "always in this order"
        seen = sum(candidate.sessions.values())
        said = f"{number}. sites: {' → '.join(once(candidate.sites))} ({order})"
        if candidate.steps is not None:
            said += f" · steps: {', '.join(step.kind for step in candidate.steps)}"
        lines.append(
            said
            + f" · usually starts at hour {candidate.hour}"
            + f" · seen on {candidate.days} days, in {seen} sessions"
        )
    lines += [
        "",
        "Answer with JSON and nothing else: a list with one object for each number,",
        '[{"n": 1, "sentence": "<one sentence>", "confidence": 3}, ...].',
        "A confidence of 1 means coincidence. 5 means surely one repeated task.",
    ]
    return "\n".join(lines)


def sentences(reply: str, count: int) -> list[tuple[str, int | None]]:
    """What a model said of each of count candidates: its sentence, and its confidence.

    The first list in the reply that reads as JSON is taken. If there is none, every
    candidate gets an empty sentence and no confidence, and so does a candidate that no
    object of the list names. An object that repeats a number is ignored. A sentence is
    stripped, its whitespace is collapsed, and it is cut at 300 characters. A confidence
    is kept only as an integer from 1 to 5.
    """
    said: dict[int, tuple[str, int | None]] = {}
    for item in _objects(reply):
        if not isinstance(item, dict):
            continue
        number = _whole(item.get("n"))
        if number is None or not 1 <= number <= count or number in said:
            continue
        said[number] = (_sentence(item.get("sentence")), _confidence(item.get("confidence")))
    return [said.get(number, ("", None)) for number in range(1, count + 1)]


def _objects(reply: str) -> list[object]:
    """The objects in the first list of the reply that reads as JSON, or none. A bracket in
    prose before the list loses nothing."""
    decoder = json.JSONDecoder()
    position = reply.find("[")
    while position != -1:
        try:
            read, _ = decoder.raw_decode(reply, position)
        except ValueError:
            position = reply.find("[", position + 1)
            continue
        return list(read) if isinstance(read, list) else []
    return []


def _sentence(value: object) -> str:
    """The sentence of a model, on one line and no longer than 300 characters."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:SENTENCE_AT_MOST]


def _whole(value: object) -> int | None:
    """A number that is whole, as an integer. None for anything else: a fraction, text,
    null, and a boolean, which Python counts as 1 and JSON does not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _confidence(value: object) -> int | None:
    """The confidence a model gave, as an integer from 1 to 5, or None when it gave none."""
    whole = _whole(value)
    return whole if whole is not None and 1 <= whole <= 5 else None


def name_of(candidate: Candidate) -> str:
    """The name of the proposal for a candidate.

    A sequence of sites is named by its sites in their order. A ritual whose order varies is
    named by its sites in alphabetical order: the order it is seen in changes from one run to
    the next, and a proposal found again must be the same proposal, for a decision to follow it.
    A sequence of steps is named by its sites and by its steps: two tasks on one site must
    never share a name.
    """
    if candidate.steps is not None:
        return name_for(once(candidate.sites), keys=candidate.keys or ())
    return name_for(candidate.sites if candidate.order == "fixed" else sorted(candidate.sites))


def propose(candidates: Sequence[Candidate], ask: Ask | None) -> list[Proposal]:
    """One proposal per candidate, in the order given, each with the evidence of its
    candidate.

    With no model, every goal is empty and every confidence is None. With one, the answer
    to a single prompt is read for every candidate. A sentence that names a place outside
    the evidence is dropped, and its confidence with it: a model may not bring a site of
    its own into a proposal. Nothing a model raises is caught here.
    """
    if not candidates:
        return []
    if ask is None:
        said: list[tuple[str, int | None]] = [("", None)] * len(candidates)
    else:
        said = sentences(ask(prompt_for(candidates)), len(candidates))
    made = []
    for candidate, (sentence, confidence) in zip(candidates, said, strict=True):
        if sentence and not _names_only(sentence, candidate.sites):
            sentence, confidence = "", None
        made.append(
            Proposal(
                name_of(candidate),
                sentence,
                candidate.sites,
                candidate.order,
                candidate.days,
                dict(candidate.sessions),
                candidate.first_seen,
                candidate.last_seen,
                candidate.hour,
                confidence,
                "proposed",
            )
        )
    return made


def _names_only(sentence: str, sites: Sequence[str]) -> bool:
    """True when the sentence names no place that is not one of the sites.

    Every run of text in it shaped like a host name must be one of the sites. And a
    sentence with www., http or an at sign in it is not kept, whatever it names.
    """
    lowered = sentence.lower()
    if any(unwanted in lowered for unwanted in UNWANTED):
        return False
    return all(named in sites for named in HOST.findall(lowered))
