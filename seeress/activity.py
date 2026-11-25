"""The activity event: one thing a person did in their browser, and nothing more about them.

The activity record holds more than Seeress needs to find repetitive work. A line of it
carries a page title and a full address, query included. An event is the part that may be
used: the site, the path without its query and its fragment, the kind of thing that
happened, the tab, and the second. What may reach a model is the site, the path, the hour
and counts, so an event never carries a page title, typed text or a full URL.

The shape of an event is contracts/activity-event.schema.json. Event.parse is that schema
written by hand, and a contract test keeps the two in agreement. read_events turns the lines
of the record into events, and skips every line that holds none. read_outages and
read_agent_runs read the same record for the times at which it holds nothing of a person's:
when the logger could not reach the browser, and when an agent was driving it.
"""

import json
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# What an event says happened.
KINDS: tuple[str, ...] = ("navigate", "focus", "click", "input", "submit", "select")

# The patterns of the schema, as it writes them, searched for as its validator searches them.
# A pattern that must end at the end says so with `(?![\s\S])`, which means the same in Python
# and in the dialect JSON Schema names. `$` does not: in Python it also holds before a final
# newline.
TS_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?![\s\S])")
SITE_SHAPE = re.compile(r"^(?!www\.)[a-z0-9._-]+(?![\s\S])")
PATH_SHAPE = re.compile(r"^/")
TARGET_SHAPE = re.compile(r"^[0-9a-f]{8}(?![\s\S])")

# How the logger writes the second that ts holds.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _matches(shape: re.Pattern[str], value: object) -> bool:
    """True when value is a string that holds the shape where the schema looks for it."""
    return isinstance(value, str) and shape.search(value) is not None


def _is_one(value: object) -> bool:
    """True for the number 1, and for nothing else.

    JSON has one kind of number, so 1.0 is 1. True equals 1 in Python, and is no number in
    JSON.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float) and value == 1


@dataclass(frozen=True)
class Event:
    """One thing a person did in their browser: a site, a path, a kind, a tab and a second."""

    v: int
    ts: str
    kind: str
    site: str
    path: str
    target: str | None

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the event, with its six fields in the schema's order."""
        return {
            "v": self.v,
            "ts": self.ts,
            "kind": self.kind,
            "site": self.site,
            "path": self.path,
            "target": self.target,
        }

    @classmethod
    def parse(cls, data: object) -> "Event":
        """The event in data. Raises ValueError when the schema would refuse data."""
        # The shape is contracts/activity-event.schema.json; a contract test keeps this
        # reader and that schema in agreement.
        if not (
            isinstance(data, dict)
            and set(data) == {"v", "ts", "kind", "site", "path", "target"}
            and _is_one(data["v"])
            and _matches(TS_SHAPE, data["ts"])
            and data["kind"] in KINDS
            and _matches(SITE_SHAPE, data["site"])
            and _matches(PATH_SHAPE, data["path"])
            and (data["target"] is None or _matches(TARGET_SHAPE, data["target"]))
        ):
            raise ValueError("the activity event has an unexpected shape")
        return cls(1, data["ts"], data["kind"], data["site"], data["path"], data["target"])


# --------------------------------------------------------------- the record ---


@dataclass(frozen=True)
class Interval:
    """A stretch of time between two seconds, both of which are in it.

    start and end are seconds since the epoch, by the clock of this machine. An interval
    whose end is before its start is empty.
    """

    start: float
    end: float

    @property
    def empty(self) -> bool:
        """True when no second is in the interval."""
        return self.end < self.start

    def holds(self, when: float) -> bool:
        """True when the second when is in the interval."""
        return self.start <= when <= self.end


def epoch(ts: str) -> float:
    """When ts happened, in seconds since the epoch, read as this machine's local time.

    Raises ValueError when ts is no time this machine can place, such as
    "2026-13-45T25:61:61". The schema judges the shape of a ts, not whether it is one.
    """
    try:
        return time.mktime(time.strptime(ts, TS_FORMAT))
    except OverflowError as error:  # a year this machine's clock cannot place
        raise ValueError("the ts is no time this machine can place") from error


def read_events(home: Path, hours: float | None = None) -> list[Event]:
    """The activity events in the record under home, oldest first.

    home is the Seeress home. The record is read file by file in file-name order, and the
    events come back in time order. Events that share a second keep the order of the files
    and of the lines. With hours, only the events of the last hours are returned, by this
    machine's clock and with each ts read as the machine's local time. Nothing is written.
    """
    cutoff = _cutoff(hours)
    events: list[Event] = []
    for data in _lines(home):
        event = _event_in(data)
        if event is None or (cutoff is not None and not _is_since(event.ts, cutoff)):
            continue
        events.append(event)
    # A ts is digits of a fixed width, the largest first, so its order as text is its order
    # in time. The sort is stable, so events that share a second keep their place.
    return sorted(events, key=lambda event: event.ts)


def read_outages(home: Path, hours: float | None = None) -> list[Interval]:
    """Every outage in the record under home, in time order.

    While the logger cannot reach the browser it writes chrome_down every few seconds, and
    once it can again it writes connected. An outage starts at the ts of the first
    chrome_down and ends at the ts of the connected that follows it. A record that ends
    inside an outage ends it at the ts of its last line that can be read, and the outage
    counts as still going on. A line that cannot be read is skipped. With hours, an outage
    that ended before the cutoff is left out, and one still going on never is. Nothing is
    written.
    """
    cutoff = _cutoff(hours)
    outages: list[tuple[Interval, bool]] = []
    start: float | None = None
    last = 0.0
    for data in _lines(home):
        when = _when(data.get("ts"))
        if when is None:
            continue
        kind = data.get("kind")
        if start is None:
            if kind == "chrome_down":
                start, last = when, when
        elif kind == "connected":
            outages.append((Interval(start, when), True))
            start = None
        else:
            last = when
    if start is not None:
        outages.append((Interval(start, last), False))
    # The second of each pair says whether the outage ended. One that did not is still
    # going on, whenever it began.
    return sorted(
        (outage for outage, over in outages if not over or cutoff is None or outage.end >= cutoff),
        key=lambda outage: outage.start,
    )


def read_agent_runs(home: Path, hours: float | None = None) -> list[Interval]:
    """The runs of the agent in the record under home, in the order they were written.

    The runs are in agent-runs.jsonl, one object per line. Its start is the ts of when a
    run began and its end the ts of when it ended. A missing file is no run. A line whose
    start and end cannot be read, or whose end is before its start, is skipped. With hours,
    a run that ended before the cutoff is left out. Nothing is written.
    """
    cutoff = _cutoff(hours)
    try:
        lines = (home / "watch" / "agent-runs.jsonl").read_bytes().splitlines()
    except FileNotFoundError:
        return []
    runs: list[Interval] = []
    for line in lines:
        data = _object(line)
        if data is None:
            continue
        start, end = _when(data.get("start")), _when(data.get("end"))
        if start is not None and end is not None and start <= end:
            runs.append(Interval(start, end))
    return [run for run in runs if cutoff is None or run.end >= cutoff]


def _cutoff(hours: float | None) -> float | None:
    """The moment, in seconds, before which a reader with hours leaves out what it read."""
    return None if hours is None else time.time() - hours * 3600


def _lines(home: Path) -> Iterator[dict[str, Any]]:
    """Every line of the record under home that is an object in JSON, file by file in
    file-name order and in line order. A line that is neither is skipped.

    A file is named for the day the logger started, not for the day of its lines. So a file
    is never read as a day.
    """
    for path in sorted((home / "watch").glob("spike-*.jsonl")):
        for line in path.read_bytes().splitlines():
            data = _object(line)
            if data is not None:
                yield data


def _object(line: bytes) -> dict[str, Any] | None:
    """The object in one line, or None when the line is not JSON or not an object."""
    try:
        data: object = json.loads(line)
    except ValueError:  # not JSON, or not text
        return None
    return data if isinstance(data, dict) else None


def _event_in(data: dict[str, Any]) -> Event | None:
    """The event in one line of the record, or None when the line holds none."""
    # A line that names a version is an activity event, and is read as it is. So it is never
    # taken for a line of the logger's, whatever kind it names. Event.parse then keeps it only
    # at the version this reader knows.
    if "v" in data:
        return _event_or_none(data)
    kind = data.get("kind")
    if kind == "focus":
        return _from_address(data, "focus", None)
    if kind in ("nav", "tab_open"):
        return _from_address(data, "navigate", data.get("target"))
    return None


def _from_address(data: Mapping[str, object], kind: str, target: object) -> Event | None:
    """The event in a line that names a page by its address, or None when it names none.

    Only the host name and the path of the address are read. The title on the line is not
    read at all.
    """
    url = data.get("url")
    if not isinstance(url, str):
        return None
    try:
        address = urlsplit(url)
    except ValueError:  # an address Python cannot split, such as "https://[::1"
        return None
    host = address.hostname
    if address.scheme not in ("http", "https") or host is None:
        return None
    return _event_or_none(
        {
            "v": 1,
            "ts": data.get("ts"),
            "kind": kind,
            # The host name comes out of the address in lower case. One leading www. goes.
            "site": host.removeprefix("www."),
            "path": address.path or "/",
            "target": target,
        }
    )


def _event_or_none(candidate: object) -> Event | None:
    """The event in candidate, or None when the schema refuses it."""
    try:
        return Event.parse(candidate)
    except ValueError:
        return None


def _is_since(ts: str, cutoff: float) -> bool:
    """True when ts is not before the cutoff. A ts that is no time never is."""
    moment = _when(ts)
    return moment is not None and moment >= cutoff


def _when(value: object) -> float | None:
    """When a ts of the record happened, in seconds, or None when it is not a ts this
    machine can place."""
    if not isinstance(value, str):
        return None
    try:
        return epoch(value)
    except ValueError:
        return None
