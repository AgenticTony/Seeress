"""The work of `seeress mine`: the record read and mined, a model asked what a pattern may
be, and a report.

What reaches a model is a site, an hour and counts: never a path, a title or anything typed.
And the model is one on this machine, by the guard. So nothing of the record leaves it.
"""

import time
from collections.abc import Sequence
from pathlib import Path

from . import miner
from .activity import Interval, epoch, read_agent_runs, read_events, read_outages
from .brain import chat, endpoint_for, message
from .config import seeress_home
from .errors import MiningError
from .miner import Ask, Candidate, Gap, Session, by_strength
from .proposals import Proposal, remember

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
    """
    moment = time.time() if now is None else now
    events = read_events(home, hours, now=moment)
    if not events:
        raise MiningError(_nothing_found(home, hours))
    outages = read_outages(home, hours, now=moment)
    runs = read_agent_runs(home, hours, now=moment)
    kept = miner.without_runs(events, runs)
    gaps = miner.gaps(kept, outages, now=moment)
    sittings = miner.sessions(kept, outages=outages)
    candidates = _strongest(sittings, asked_at_most)
    made = miner.propose(candidates, ask)
    proposals = [remember(home, proposal) for proposal in made]
    return _report(hours, len(kept), sittings, outages, gaps, moment, candidates, proposals)


def _nothing_found(home: Path, hours: float | None) -> str:
    """Why nothing could be mined, and what to check."""
    when = "" if hours is None else f" in the last {hours:g} hours"
    return (
        f"no activity is recorded under {home / 'watch'}{when}. Check two things: is the"
        " activity logger running, and is remote debugging allowed in Chrome."
    )


def _strongest(sittings: Sequence[Session], asked_at_most: int) -> list[Candidate]:
    """The candidates to propose: the sequences that recur, the rituals whose order varies,
    and of them the strongest.

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
    return by_strength([*fixed, *varied])[:asked_at_most]


def _report(
    hours: float | None,
    event_count: int,
    sittings: Sequence[Session],
    outages: Sequence[Interval],
    gaps: Sequence[Gap],
    now: float,
    candidates: Sequence[Candidate],
    proposals: Sequence[Proposal],
) -> str:
    """What was mined, then where the record has no data, then the proposals."""
    lines = [
        f"Mined {_window(hours)}: {_count(event_count, 'event')} in "
        f"{_count(len(sittings), 'sitting')}.",
        _no_data(sittings, outages, gaps, now),
    ]
    lines.extend(_gap_lines(gaps))
    if not proposals:
        lines.append("Proposals: none.")
        lines.append(f"  Something must recur on at least {DAYS_NEEDED} days to be proposed.")
    else:
        lines.append(f"Proposals: {len(proposals)}.")
        last = sorted(
            zip(candidates, proposals, strict=True),
            key=lambda pair: pair[1].status == "dismissed",
        )
        for candidate, proposal in last:
            lines.extend(_proposal_lines(candidate, proposal))
    return "\n".join(lines) + "\n"


def _window(hours: float | None) -> str:
    return "all recorded time" if hours is None else f"the last {hours:g} hours"


def _count(number: int, thing: str) -> str:
    """The number and the thing, in the singular when there is one."""
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


def _no_data(
    sittings: Sequence[Session],
    outages: Sequence[Interval],
    gaps: Sequence[Gap],
    now: float,
) -> str:
    """One line: the minutes in the schedule for which the record holds nothing, out of the
    scheduled minutes of the period. The period runs from the first sitting to now."""
    if not sittings:
        return "Time with no data: no event can be placed in the schedule."
    scheduled = miner.scheduled(epoch(sittings[0].start), now)
    silent = sum(epoch(gap.end) - epoch(gap.start) for gap in gaps if gap.why == "silent")
    down = sum(miner.scheduled(outage.start, outage.end) for outage in outages if not outage.empty)
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


def _proposal_lines(candidate: Candidate, proposal: Proposal) -> list[str]:
    """The lines of one proposal: its name and status, its sites, its evidence, and what the
    model said when it said something."""
    order = "   the order varies" if proposal.order == "varies" else ""
    seen = sum(proposal.sessions.values())
    paths = " · ".join(
        f"{site}: {', '.join(shown)}"
        for site, shown in zip(proposal.sites, candidate.paths, strict=True)
    )
    lines = [
        f"{proposal.name}   {proposal.status}{order}",
        f"  sites:  {' → '.join(proposal.sites)}",
        f"  seen:   on {proposal.days} days, in {seen} sittings; first {proposal.first_seen},"
        f" last {proposal.last_seen}; usually at hour {proposal.hour}",
        f"  paths:  {paths}",
    ]
    if proposal.goal:
        confidence = (
            f"confidence {proposal.confidence}: " if proposal.confidence is not None else ""
        )
        lines.append(f"  model:  {confidence}{proposal.goal}")
    return lines
