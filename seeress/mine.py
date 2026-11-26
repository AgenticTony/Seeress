"""The work of `seeress mine` and of the commands around a proposal.

`mine` reads the record, finds what repeats, asks a model what a pattern may be, and
reports. `show` shows one proposal, `set_goal` writes the goal in the user's own words,
which approves it, and `dismiss` sets it aside. `progress` says what was proposed, what was
compiled and how much each flow ran. It judges nothing.

What reaches a model is a site, an hour and counts: never a path, a title or anything typed.
And the model is one on this machine, by the guard. So nothing of the record leaves it.
"""

import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import miner, steps
from .activity import (
    FlowRun,
    Interval,
    Schedule,
    epoch,
    read_agent_runs,
    read_events,
    read_flow_runs,
    read_outages,
)
from .brain import chat, endpoint_for, message
from .config import load_settings, seeress_home
from .errors import MiningError
from .miner import Ask, Candidate, Gap, Session, name_of, once
from .proposals import (
    APPROVED,
    DAYS_AT_MOST,
    GOAL_AT_MOST,
    Proposal,
    every,
    record,
    remember,
    remembered,
    written_by_the_user,
)

# How many candidates a model is asked about, and so how many are proposed. They are the
# strongest, by days and then by sittings. On a month of invented events with random sites
# the miner found 4,899 candidates, and a person reads no more than a screen of them.
ASKED_AT_MOST = 25

# How long a model on this machine may take to answer one prompt.
MODEL_TIMEOUT = 600

# The days a pattern must recur on before it is proposed. The miner holds the number.
DAYS_NEEDED = 3


def local_model() -> Ask:
    """A model on this machine, and no other.

    The prompt is one person's activity: sites, hours and counts. The profile is the client
    one with no page, which makes the guard allow a model on this machine and nothing else,
    whatever the settings say.
    """

    def ask(prompt: str) -> str:
        return chat(endpoint_for("client", None), [message("user", prompt)], timeout=MODEL_TIMEOUT)

    return ask


def run(hours: float | None = None, *, no_llm: bool = False, home: Path | None = None) -> str:
    """What the command calls: the record under the Seeress home, and the model on this
    machine unless no_llm is set."""
    return mine(
        home if home is not None else seeress_home(),
        hours,
        ask=None if no_llm else local_model(),
    )


def mine(
    home: Path,
    hours: float | None = None,
    *,
    ask: Ask | None,
    now: float | None = None,
    asked_at_most: int = ASKED_AT_MOST,
) -> str:
    """Read the record under home, find what repeats, ask the model if there is one, remember
    the proposals, and return the report.

    With hours, only the last hours of the record are read. now is the moment the record is
    taken to end, in seconds. The clock is read once, and only when now is not given: the
    readers and the gaps work from the same moment.

    At most asked_at_most candidates are asked about: the strongest that are not dismissed.
    A dismissed proposal takes no place among them and is not asked about again. One that
    recurs is remembered again, as the user left it, and listed last. A pattern seen on more
    days than a proposal holds is left out, and the report says how many.
    """
    moment = time.time() if now is None else now
    events = read_events(home, hours, now=moment)
    if not events:
        raise MiningError(_nothing_found(home, hours))
    outages = read_outages(home, hours, now=moment)
    runs = read_agent_runs(home, hours, now=moment)
    kept = miner.without_runs(events, runs)
    # A settings file Seeress cannot read stops the mine: never a default slipped in.
    schedule = load_settings(home).capture.schedule
    gaps = miner.gaps(kept, outages, now=moment, schedule=schedule)
    sittings = miner.sessions(kept, outages=outages)
    found = _candidates(sittings)
    too_long = [candidate for candidate in found if candidate.days > DAYS_AT_MOST]
    dismissed = {proposal.name for proposal in every(home) if proposal.status == "dismissed"}
    offered: list[Candidate] = []
    set_aside: list[Candidate] = []
    for candidate in found:
        if candidate.days > DAYS_AT_MOST:
            continue
        (set_aside if name_of(candidate) in dismissed else offered).append(candidate)
    asked = offered[:asked_at_most]
    made = miner.propose(asked, ask)
    found_again = [miner.propose([candidate], None)[0] for candidate in set_aside]
    pairs = [
        (candidate, remember(home, proposal))
        for candidate, proposal in [
            *zip(asked, made, strict=True),
            *zip(set_aside, found_again, strict=True),
        ]
    ]
    return _report(
        home,
        hours,
        len(events),
        len(kept),
        sittings,
        outages,
        gaps,
        moment,
        pairs,
        len(too_long),
        schedule,
    )


def _nothing_found(home: Path, hours: float | None) -> str:
    """Why nothing could be mined, and what to check."""
    when = "" if hours is None else f" in the last {hours:g} hours"
    return (
        f"no activity is recorded under {home / 'watch'}{when}. Check two things: is the"
        " activity logger running, and is remote debugging allowed in Chrome."
    )


def _candidates(sittings: Sequence[Session]) -> list[Candidate]:
    """Every candidate, strongest first: the sequences that recur, the rituals whose order
    varies, and beside them the sequences of steps that recur. A sequence of steps that
    says wholly what a sequence of sites says takes its place.

    A ritual whose sites are exactly those of a sequence is left out: the sequence says all
    the ritual says, and the order too.
    """
    fixed = miner.recurring(sittings)
    sequenced = {frozenset(candidate.sites) for candidate in fixed}
    varied = [
        candidate
        for candidate in miner.clusters(sittings)
        if frozenset(candidate.sites) not in sequenced
    ]
    return steps.beside([*fixed, *varied], steps.recurring(sittings))


def _report(
    home: Path,
    hours: float | None,
    recorded: int,
    mined: int,
    sittings: Sequence[Session],
    outages: Sequence[Interval],
    gaps: Sequence[Gap],
    now: float,
    pairs: Sequence[tuple[Candidate, Proposal]],
    too_long: int,
    schedule: Schedule,
) -> str:
    """What was mined, then where the record has no data, then the proposals.

    recorded is how many events the record holds; mined, how many are a person's once the
    agent's runs are taken out. Each pair is a candidate and the proposal remembered for it.
    """
    lines = [
        f"Mined {_window(hours)}: {_count(mined, 'event')} in {_count(len(sittings), 'sitting')}.",
        _no_data(recorded, sittings, outages, gaps, now, schedule),
    ]
    lines.extend(_gap_lines(gaps))
    if too_long:
        verb = "recurs" if too_long == 1 else "recur"
        lines.append(
            f"{_count(too_long, 'pattern')} {verb} on more than {DAYS_AT_MOST} days. A proposal"
            " holds evidence on that many at the most, so none of them is proposed."
        )
    if not pairs:
        lines.append("Proposals: none.")
        lines.append(f"  Something must recur on at least {DAYS_NEEDED} days to be proposed.")
    else:
        lines.append(f"Proposals: {len(pairs)}.")
        last = sorted(pairs, key=lambda pair: pair[1].status == "dismissed")
        for candidate, proposal in last:
            lines.extend(_proposal_lines(proposal, candidate, home=home))
    return "\n".join(lines) + "\n"


def _window(hours: float | None) -> str:
    return "all recorded time" if hours is None else f"the last {hours:g} hours"


def _count(number: int, thing: str) -> str:
    """The number and the thing, in the singular when there is one."""
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


def _no_data(
    recorded: int,
    sittings: Sequence[Session],
    outages: Sequence[Interval],
    gaps: Sequence[Gap],
    now: float,
    schedule: Schedule,
) -> str:
    """One line: the minutes in the schedule for which the record holds nothing, out of the
    scheduled minutes of the period. The period runs from the first sitting to now. An
    outage is counted for the part of it that is in the period and in the schedule. The
    schedule is the one in the settings."""
    if not sittings:
        if recorded:
            return (
                f"Time with no data: every one of the {_count(recorded, 'event')} recorded was"
                " written while the agent was driving the browser. Nothing of a person's is"
                " left to mine."
            )
        return "Time with no data: no event can be placed in the schedule."
    first = epoch(sittings[0].start)
    scheduled = miner.scheduled(first, now, schedule)
    silent = sum(epoch(gap.end) - epoch(gap.start) for gap in gaps if gap.why == "silent")
    down = sum(
        miner.scheduled(max(outage.start, first), min(outage.end, now), schedule)
        for outage in outages
        if not outage.empty
    )
    minutes, total = round((silent + down) / 60), round(scheduled / 60)
    share = f" ({minutes / total:.0%})" if total else ""
    return f"Time with no data: {minutes} of {total} minutes in the schedule{share}."


def _gap_lines(gaps: Sequence[Gap]) -> list[str]:
    """One line per gap, oldest first: when, for how long, and why."""
    if not gaps:
        return ["  No gap."]
    lines = []
    for gap in gaps:
        end = gap.end[11:] if gap.end[:10] == gap.start[:10] else gap.end
        why = "the logger was down" if gap.why == "down" else "nothing was written"
        lines.append(f"  {gap.start} to {end} {gap.minutes:>4} min  {why}")
    return lines


def _proposal_lines(proposal: Proposal, candidate: Candidate | None, *, home: Path) -> list[str]:
    """The lines of one proposal: its name and status, its sites, its evidence, and its goal.

    candidate is the one the proposal was made from in this run, and None when the proposal
    is only remembered. A site that comes again right after itself is shown once. The steps
    of a task are shown, and no paths beside them: each step says its page. A proposal on
    sites shows the paths seen there, as the miner found them. A proposal remembered from an
    earlier run has no evidence to show, for none is remembered with it.

    The goal is shown as the user's words once the user wrote it, with no confidence of a
    model beside it. Until then it is a model's sentence.
    """
    order = "   the order varies" if proposal.order == "varies" else ""
    seen = sum(proposal.sessions.values())
    lines = [
        f"{proposal.name}   {proposal.status}{order}",
        f"  sites:  {' → '.join(once(proposal.sites))}",
        f"  seen:   on {proposal.days} days, in {seen} sittings; first {proposal.first_seen},"
        f" last {proposal.last_seen}; usually at hour {proposal.hour}",
    ]
    if candidate is not None and candidate.steps is not None:
        lines.extend(steps.lines(candidate.steps))
    if candidate is not None and candidate.steps is None:
        shown = " · ".join(
            f"{site}: {', '.join(there)}"
            for site, there in zip(proposal.sites, candidate.paths, strict=True)
        )
        lines.append(f"  paths:  {shown}")
    if proposal.goal:
        if proposal.status in APPROVED or written_by_the_user(home, proposal.name):
            lines.append(f"  goal:   {proposal.goal}")
        else:
            confidence = (
                f"confidence {proposal.confidence}: " if proposal.confidence is not None else ""
            )
            lines.append(f"  model:  {confidence}{proposal.goal}")
    return lines


# ------------------------------------------------------ the proposal commands ---


def _home(home: Path | None) -> Path:
    """The Seeress home: the one given, or the one the settings name."""
    return seeress_home() if home is None else home


def show(name: str, home: Path | None = None) -> str:
    """One proposal as the report of mine prints it, but without its evidence: the paths and
    the steps are those of the run that found it, and are not remembered with it."""
    home = _home(home)
    return "\n".join(_proposal_lines(remembered(home, name), None, home=home)) + "\n"


def set_goal(name: str, goal: str | None = None, home: Path | None = None) -> str:
    """Write the goal in the user's own words. That approves the proposal.

    A proposal that is compiled stays compiled. Nothing else of it is changed, except the
    confidence: it was the model's, for the model's sentence, and goes with that sentence.
    The goal is 1 to 1000 characters once stripped: anything else is refused, and nothing
    is written.
    """
    if goal is None:
        raise MiningError(f'a goal needs words, in quotes: seeress proposal goal {name} "..."')
    words = goal.strip()
    if not 1 <= len(words) <= GOAL_AT_MOST:
        raise MiningError(
            f"the goal is {len(words)} characters once stripped. It takes from 1 to {GOAL_AT_MOST}."
        )
    home = _home(home)
    before = remembered(home, name)
    status = "compiled" if before.status == "compiled" else "approved"
    record(home, replace(before, goal=words, status=status, confidence=None))
    return f"Goal saved. To compile it: seeress proposal compile {name}"


def dismiss(name: str, home: Path | None = None) -> str:
    """Set a proposal aside, with its goal as it is. The miner keeps it dismissed."""
    home = _home(home)
    record(home, replace(remembered(home, name), status="dismissed"))
    return f"Dismissed: {name}"


def progress(home: Path | None = None) -> str:
    """What was proposed, what was compiled, and how much each flow ran. It judges nothing.

    A proposal counts by the last line that reads in its file. A flow ran well on a day
    when one of its runs that day exited 0, and the day is counted once, however many did.
    """
    home = _home(home)
    remembered_all = every(home)
    counted = Counter(proposal.status for proposal in remembered_all)
    lines = [
        f"Proposals: {counted['proposed']} proposed, {counted['approved']} approved,"
        f" {counted['compiled']} compiled, {counted['dismissed']} dismissed."
    ]
    compiled = sorted(
        (proposal for proposal in remembered_all if proposal.status == "compiled"),
        key=lambda proposal: proposal.name,
    )
    if not compiled:
        lines.append(
            'Nothing is compiled. To compile a proposal: seeress proposal goal NAME "...",'
            " then seeress proposal compile NAME."
        )
    runs = read_flow_runs(home)
    lines.extend(_ran_lines(proposal, runs) for proposal in compiled)
    return "\n".join(lines) + "\n"


def _ran_lines(proposal: Proposal, runs: Sequence[FlowRun]) -> str:
    """One line on how much the flow of a compiled proposal ran."""
    days = sorted({run.ts[:10] for run in runs if run.name == proposal.name and run.exit == 0})
    if not days:
        return f"{proposal.name}   has never run well"
    first, last = days[0], days[-1]
    between = (date.fromisoformat(last) - date.fromisoformat(first)).days
    apart = "1 day apart" if between == 1 else f"{between} days apart"
    day_or_days = "day" if len(days) == 1 else "days"
    return (
        f"{proposal.name}   ran well on {len(days)} {day_or_days}, from {first} to {last}, {apart}"
    )
