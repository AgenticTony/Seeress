"""Finding what repeats in the activity record, and where the record has holes.

Three things are found here, and all three are counted. Nothing in this module reads a
file, reads the clock or asks a model: everything it needs comes in as an argument. What it
gives out is a site, a path with its identifiers hidden, an hour and counts.

- A session is one sitting: a run of events with no hole in it and no outage.
- A candidate is a sequence of two to six sites that turns up in sittings on different
  days. A pattern repeated inside one sitting is not a workflow.
- A gap is time in the schedule for which the record holds nothing, or an outage of the
  logger.
"""

import json
import re
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from seeress.activity import TS_FORMAT, Event, Interval, epoch
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
class _Visit:
    """One visit to a site inside a sitting: every event of it, in order."""

    site: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class Candidate:
    """A sequence of sites that recurs across days, with the evidence for it.

    paths holds one tuple per site: the paths seen at that step, most common first and at
    most three, with their identifier-like segments replaced by {}.

    order is "fixed" when the sites recur in this order, and "varies" when they recur as a
    set, in any order.
    """

    sites: tuple[str, ...]
    paths: tuple[tuple[str, ...], ...]
    days: int
    sessions: Mapping[str, int]
    first_seen: str
    last_seen: str
    hour: int
    order: str = "fixed"


def recurring(sittings: Sequence[Session], days: int = 3) -> list[Candidate]:
    """The sequences of two to six sites that occur in sittings on at least days different
    days, most days first.

    A sitting becomes its sequence of sites, one per visit, and every block of two to six
    sites in it is counted. A block occurs in a sitting when it appears anywhere in it,
    however many times. A block that another candidate holds as a contiguous block, and that
    was seen on at least as many days, is left out: a five-site sequence that recurs brings
    all its shorter parts with it.

    Where two candidates were seen on as many days, the one seen in more sittings comes
    first. Where those are equal too, the one whose sites come first alphabetically.
    """
    visits = [_visits(sitting) for sitting in sittings]
    # The record holds many more blocks than recur, so first only the days of each block
    # are counted, and the evidence is gathered for those on enough days alone.
    days_of: dict[tuple[str, ...], set[str]] = {}
    for sitting, sequence in zip(sittings, visits, strict=True):
        for _, block in _blocks(sequence):
            days_of.setdefault(block, set()).add(sitting.day)
    enough = {block for block, seen in days_of.items() if len(seen) >= days}
    occurrences: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for index, sequence in enumerate(visits):
        for position, block in _blocks(sequence):
            if block in enough:
                occurrences.setdefault(block, []).append((index, position))
    found = [_candidate(block, where, sittings, visits) for block, where in occurrences.items()]
    kept = [candidate for candidate in found if not _covered(candidate, found)]
    return by_strength(kept)


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


def _visits(sitting: Session) -> list[_Visit]:
    """The sitting as one visit per run of events on the same site, in order.

    Consecutive repeats are collapsed, so `A A B A B C` holds the block `A B C`.
    """
    visits: list[_Visit] = []
    for event in sitting.events:
        if visits and visits[-1].site == event.site:
            visits[-1] = _Visit(event.site, (*visits[-1].events, event))
        else:
            visits.append(_Visit(event.site, (event,)))
    return visits


def _blocks(sequence: Sequence[_Visit]) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Every block of two to six sites in the sequence, with where it starts."""
    for size in range(LEAST_SITES, MOST_SITES + 1):
        for position in range(len(sequence) - size + 1):
            yield position, tuple(visit.site for visit in sequence[position : position + size])


def _candidate(
    block: tuple[str, ...],
    where: Sequence[tuple[int, int]],
    sittings: Sequence[Session],
    visits: Sequence[list[_Visit]],
) -> Candidate:
    """The candidate for one block, with its evidence gathered from every occurrence.

    An occurrence starts at the first event of its first visit and ends at the last event of
    its last.
    """
    sittings_seen = {index for index, _ in where}
    per_day: Counter[str] = Counter(sittings[index].day for index in sittings_seen)
    starts: list[str] = []
    ends: list[str] = []
    hours: Counter[int] = Counter()
    paths: list[Counter[str]] = [Counter() for _ in block]
    for index, position in where:
        chosen = visits[index][position : position + len(block)]
        start, end = chosen[0].events[0].ts, chosen[-1].events[-1].ts
        starts.append(start)
        ends.append(end)
        hours[int(start[11:13])] += 1
        for step, visit in enumerate(chosen):
            paths[step].update(_masked(event.path) for event in visit.events)
    return Candidate(
        block,
        tuple(_most_common(counted) for counted in paths),
        len(per_day),
        dict(sorted(per_day.items())),
        min(starts),
        max(ends),
        _most_common_hour(hours),
    )


def _most_common(counted: Counter[str]) -> tuple[str, ...]:
    """What was counted, most common first, alphabetically where counts are equal, and at
    most three."""
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    return tuple(path for path, _ in ranked[:PATHS_SHOWN])


def _most_common_hour(hours: Counter[int]) -> int:
    """The hour most occurrences start at. Where two tie, the earlier one."""
    return min(hour for hour, count in hours.items() if count == max(hours.values()))


def _covered(candidate: Candidate, found: Sequence[Candidate]) -> bool:
    """True when another candidate holds this one as a contiguous block, and was seen on at
    least as many days."""
    return any(
        other is not candidate
        and other.days >= candidate.days
        and _holds(other.sites, candidate.sites)
        for other in found
    )


def _holds(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    """True when shorter is a block of longer, and a proper part of it."""
    size = len(shorter)
    return len(longer) > size and any(
        longer[position : position + size] == shorter for position in range(len(longer) - size + 1)
    )


# What makes one segment of a path an identifier: a number, a UUID, or eight or more
# hexadecimal digits. The patterns are matched whole, so they need no anchor.
_NUMBER = re.compile(r"[0-9]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{8,}")


def _masked(path: str) -> str:
    """The path with every identifier-like segment replaced by {}.

    /invoices/7/top and /invoices/8/top are then one path, /invoices/{}/top. A candidate
    shows the shape of the work and not the record it was found in.
    """
    return "/".join(HIDDEN if _is_identifier(segment) else segment for segment in path.split("/"))


def _is_identifier(segment: str) -> bool:
    return any(shape.fullmatch(segment) for shape in (_NUMBER, _UUID, _HEX))


# ------------------------------------------------ rituals whose order varies ---


def clusters(sittings: Sequence[Session], days: int = 3) -> list[Candidate]:
    """The sets of two to six sites that turn up together in sittings on at least days
    different days, in any order, strongest first.

    A sitting holds a set when every site of the set is among its sites. A set that another
    found set holds whole is left out. The sites come by how many of all the sittings hold
    each one, and alphabetically where two tie. The evidence is counted over the sittings
    that hold the whole set.

    The sets are found level by level, and each level is built only from the sets of the
    level before it: a pair is looked for only when both of its sites are found on their
    own. That is what keeps a month of record from asking after every set there could be.
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

    # A set is a tuple of its sites in alphabetical order, with the bits of its sittings.
    every: list[tuple[tuple[str, ...], int]] = [
        ((site,), sitters)
        for site, sitters in sorted(holding.items())
        if found_often_enough(sitters)
    ]
    level = every
    while level and len(level[0][0]) < MOST_SITES:
        joined: list[tuple[tuple[str, ...], int]] = []
        # Two found sets that differ only in their last site make one candidate, a site
        # longer. So each pair is joined once.
        by_head: dict[tuple[str, ...], list[tuple[str, int]]] = {}
        for sites, sitters in level:
            by_head.setdefault(sites[:-1], []).append((sites[-1], sitters))
        for head, members in by_head.items():
            for position, (last, first_sitters) in enumerate(members):
                for other, second_sitters in members[position + 1 :]:
                    sitters = first_sitters & second_sitters
                    if found_often_enough(sitters):
                        joined.append(((*head, last, other), sitters))
        every.extend(joined)
        level = joined

    # Of the found sets, only the largest: the longest first, so that a part finds a whole
    # to be held by.
    largest: list[tuple[frozenset[str], int]] = []
    for sites, sitters in sorted(every, key=lambda item: (-len(item[0]), item[0])):
        whole = frozenset(sites)
        if all(not whole < found for found, _ in largest):
            largest.append((whole, sitters))
    found = [_cluster(whole, sitters, sittings, holding) for whole, sitters in largest]
    return by_strength(found)


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
                paths[event.site][_masked(event.path)] += 1
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


@dataclass(frozen=True)
class Schedule:
    """When a person is meant to be working.

    days holds the days of the week, 0 being Monday. start and end are the first and the
    last hour of the day, so 8 and 17 is 08:00 to 18:00.
    """

    days: frozenset[int]
    start: int
    end: int


# Monday to Friday, 08:00 to 18:00.
DEFAULT_SCHEDULE = Schedule(frozenset({0, 1, 2, 3, 4}), 8, 17)


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
    """
    down = sorted((outage for outage in outages if not outage.empty), key=lambda o: o.start)
    found = [_gap(outage.start, outage.end, "down") for outage in down]
    moments = sorted({when for when, _ in _placed(events)})
    for start, end in zip(moments, [*moments[1:], now], strict=False):
        if end - start > gap:
            holes = _without_outages(Interval(start, end), down)
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

    A model may see a site, an hour and counts, and never a path. The answer is JSON and
    nothing else: a list with one object for each number, holding its number, one sentence
    on what the task may be, and a confidence from 1 to 5.
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
        lines.append(
            f"{number}. sites: {' → '.join(candidate.sites)} ({order})"
            f" · usually starts at hour {candidate.hour}"
            f" · seen on {candidate.days} days, in {seen} sessions"
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
                name_for(candidate.sites),
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
