"""The activity event: one thing a person did in their browser, and nothing more about them.

The activity record holds more than Seeress needs to find repetitive work. A line of it
carries a page title and a full address, query included. An event is the part that may be
used: the site, the path without its query and its fragment, the kind of thing that
happened, the tab, and the second. What reaches a model is the site, the hour and counts: a
path is shown to the person and never sent. So an event never carries a page title, typed
text or a full URL. For the four kinds that happen in a page, an event may also say what was
done there: the element's tag, how the element is told apart on its page and, for an input,
the kind of the field. It never says what a field held.

The shape of an event is contracts/activity-event.schema.json. Event.parse is that schema
written by hand, and a contract test keeps the two in agreement. read_events turns the lines
of the record into events, and skips every line that holds none. read_outages and
read_agent_runs read the same record for the times at which it holds nothing of a person's:
when the logger could not reach the browser, and when an agent was driving it.

Seeress writes to the record for three things. write_event writes the events that a listener
in the page reports, which are events from the start. A note is written of every run of the
browser tool, which is a run of the agent's, and of how every run of a saved flow ended.
"""

import json
import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# What an event says happened.
KINDS: tuple[str, ...] = ("navigate", "focus", "click", "input", "submit", "select")

# The kinds that an action can name. An action's type is the kind of its event.
ACTION_TYPES: tuple[str, ...] = ("click", "input", "submit", "select")
ACTION_REQUIRED: tuple[str, ...] = ("type", "tag", "fingerprint")
# The kind of the field an input was typed into. The first three are named so that what was
# typed is known to be secret. No kind ever carries what was typed.
FIELD_KINDS: tuple[str, ...] = (
    "text",
    "password",
    "one-time-code",
    "card",
    "email",
    "search",
    "tel",
    "url",
    "number",
    "date",
    "checkbox",
    "radio",
    "file",
    "other",
)
EVENT_FIELDS = frozenset({"v", "ts", "kind", "site", "path", "target"})

# The patterns of the schema, as it writes them, searched for as its validator searches them.
# A pattern that must end at the end says so with `(?![\s\S])`, which means the same in Python
# and in the dialect JSON Schema names. `$` does not: in Python it also holds before a final
# newline.
TS_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?![\s\S])")
SITE_SHAPE = re.compile(r"^(?!www\.)[a-z0-9._-]+(?![\s\S])")
PATH_SHAPE = re.compile(r"^/")
# A tab is named as the browser names it: hexadecimal in upper case. The lower case is taken
# too, for a record written by a hand that did not know that.
TARGET_SHAPE = re.compile(r"^[0-9a-fA-F]{8}(?![\s\S])")
# A tag is in lower case and starts with a letter. A custom element's name always holds a
# hyphen, and a click often lands on one, so a hyphen, a dot and an underscore may follow.
TAG_SHAPE = re.compile(r"^[a-z][a-z0-9._-]*(?![\s\S])")
FINGERPRINT_AT_MOST = 200
# A page chooses the address of its tab and the name of its element. Before these bounds,
# one click on a real browser wrote a line of 2 MB, and at 200 events a minute that was
# 25 GB an hour from one tab.
SITE_AT_MOST = 253  # no host name is longer
PATH_AT_MOST = 2048  # all that is kept of a longer path
TAG_AT_MOST = 100

# How the logger writes the second that ts holds.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# The record is one person's browsing. A folder of it, and a file, are for the user alone.
PRIVATE = 0o700
PRIVATE_FILE = 0o600

# How long after a run of the agent ended the logger may still write a page the agent
# opened. The spike logger asks the browser which page is in front once every five seconds,
# and writes what it sees at the time it sees it. So a run reaches this far past its end,
# and what was written inside it is not taken for the person's. A contract test holds this
# to the logger's own interval.
POLL_LAG = 5


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
class Action:
    """What was done in a page: the element's tag, how it is told apart, and a field's kind.

    type is the kind of the event. field is the kind of the field an input was typed into,
    and None for every other action. Nothing here says what a field held.
    """

    type: str
    tag: str
    fingerprint: str
    field: str | None

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the action, in the schema's order, without a field it has none
        of."""
        data: dict[str, object] = {
            "type": self.type,
            "tag": self.tag,
            "fingerprint": self.fingerprint,
        }
        if self.field is not None:
            data["field"] = self.field
        return data


@dataclass(frozen=True)
class Event:
    """One thing a person did in their browser: a site, a path, a kind, a tab and a second.

    action is there when a listener in the page reported what was done, and None otherwise.
    """

    v: int
    ts: str
    kind: str
    site: str
    path: str
    target: str | None
    action: Action | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON object of the event, with its six fields in the schema's order, and the
        action as the seventh when there is one."""
        data: dict[str, object] = {
            "v": self.v,
            "ts": self.ts,
            "kind": self.kind,
            "site": self.site,
            "path": self.path,
            "target": self.target,
        }
        if self.action is not None:
            data["action"] = self.action.as_dict()
        return data

    @classmethod
    def parse(cls, data: object) -> "Event":
        """The event in data. Raises ValueError when the schema would refuse data."""
        # The shape is contracts/activity-event.schema.json; a contract test keeps this
        # reader and that schema in agreement.
        if not (
            isinstance(data, dict)
            and EVENT_FIELDS <= set(data) <= EVENT_FIELDS | {"action"}
            and _is_one(data["v"])
            and _matches(TS_SHAPE, data["ts"])
            and data["kind"] in KINDS
            and _matches(SITE_SHAPE, data["site"])
            and len(data["site"]) <= SITE_AT_MOST
            and _matches(PATH_SHAPE, data["path"])
            and len(data["path"]) <= PATH_AT_MOST
            and (data["target"] is None or _matches(TARGET_SHAPE, data["target"]))
        ):
            raise ValueError("the activity event has an unexpected shape")
        # An action is read against the kind of its event, which is where the schema's rules
        # tie the two.
        action = None if "action" not in data else _action_in(data["action"], data["kind"])
        if "action" in data and action is None:
            raise ValueError("the activity event has an unexpected shape")
        return cls(1, data["ts"], data["kind"], data["site"], data["path"], data["target"], action)


def _action_in(data: object, kind: str) -> Action | None:
    """The action in data for an event of this kind, or None when the schema would refuse it.

    An action names one of the four kinds that happen in a page, and names the kind of its
    event. Only an input has a field, and an input must have one.
    """
    if not (
        isinstance(data, dict)
        and set(ACTION_REQUIRED) <= set(data) <= set(ACTION_REQUIRED) | {"field"}
    ):
        return None
    field = data.get("field")
    if not (
        kind in ACTION_TYPES
        and data["type"] == kind
        and _matches(TAG_SHAPE, data["tag"])
        and len(data["tag"]) <= TAG_AT_MOST
        and isinstance(data["fingerprint"], str)
        and 1 <= len(data["fingerprint"]) <= FINGERPRINT_AT_MOST
        and _can_be_written(data["fingerprint"])
        and (
            field in FIELD_KINDS
            if kind == "input"
            else "field" not in data  # a field that is null is a field, and no kind
        )
    ):
        return None
    return Action(data["type"], data["tag"], data["fingerprint"], field)


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


@dataclass(frozen=True)
class Schedule:
    """When a person is meant to be working.

    days holds the days of the week, 0 being Monday. start and end are the first and the
    last hour of the day, so 8 and 17 is 08:00 to 18:00. The capture records by it, the
    miner reads the record by it, and the settings file names it.
    """

    days: frozenset[int]
    start: int
    end: int


# Monday to Friday, 08:00 to 18:00.
DEFAULT_SCHEDULE = Schedule(frozenset({0, 1, 2, 3, 4}), 8, 17)


def due(when: float, schedule: Schedule = DEFAULT_SCHEDULE) -> bool:
    """True when the second when falls in the schedule: on one of its days, and in one of
    its hours, the first and the last included."""
    local = time.localtime(when)
    return local.tm_wday in schedule.days and schedule.start <= local.tm_hour <= schedule.end


def read_events(
    home: Path,
    hours: float | None = None,
    *,
    now: float | None = None,
    patterns: Sequence[str] = ("activity-*.jsonl", "spike-*.jsonl"),
) -> list[Event]:
    """The activity events in the record under home, oldest first.

    home is the Seeress home. The record is read file by file in file-name order, and the
    events come back in time order. Events that share a second keep the order of the files
    and of the lines. The files are the spike logger's and the capture's: spike-*.jsonl and
    activity-*.jsonl, or only those whose names match one of patterns. With hours, only the
    events of the last hours are returned, up to now
    or, when now is not given, up to the clock. Each ts is read as the machine's local time.
    Nothing is written.
    """
    cutoff = _cutoff(hours, now)
    events: list[Event] = []
    for data in _lines(home, *patterns):
        event = _event_in(data)
        if event is None or (cutoff is not None and not _is_since(event.ts, cutoff)):
            continue
        events.append(event)
    # A ts is digits of a fixed width, the largest first, so its order as text is its order
    # in time. The sort is stable, so events that share a second keep their place.
    return sorted(events, key=lambda event: event.ts)


def read_outages(
    home: Path, hours: float | None = None, *, now: float | None = None
) -> list[Interval]:
    """Every outage in the record under home, in time order.

    While the logger cannot reach the browser it writes chrome_down every few seconds, and
    once it can again it writes connected. An outage starts at the ts of the first
    chrome_down and ends at the ts of the connected that follows it. A record that ends
    inside an outage ends it at the ts of its last line that can be read, and the outage
    counts as still going on. A line that cannot be read is skipped. With hours, an outage
    that ended before the cutoff is left out, and one still going on never is. The cutoff
    is now, or the clock, less the hours. Nothing is written.
    """
    cutoff = _cutoff(hours, now)
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


def read_agent_runs(
    home: Path, hours: float | None = None, *, now: float | None = None
) -> list[Interval]:
    """The runs of the agent in the record under home, in the order they were written.

    The runs are in agent-runs.jsonl, one object per line. Its start is the ts of when a
    run began and its end the ts of when it ended. A missing file is no run. A line whose
    start and end cannot be read, or whose end is before its start, is skipped.

    Each run reaches POLL_LAG seconds past its end. The spike logger asks the browser which
    page is in front only once every few seconds, so a page the agent opened can be written
    after the note of the run's end. With hours, a run that ended before the cutoff is left
    out. The cutoff is now, or the clock, less the hours. Nothing is written.
    """
    cutoff = _cutoff(hours, now)
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
            runs.append(Interval(start, end + POLL_LAG))
    return [run for run in runs if cutoff is None or run.end >= cutoff]


def _cutoff(hours: float | None, now: float | None) -> float | None:
    """The moment, in seconds, before which a reader with hours leaves out what it read: now,
    or the clock when now is not given, less the hours."""
    if hours is None:
        return None
    return (time.time() if now is None else now) - hours * 3600


def _lines(home: Path, *patterns: str) -> Iterator[dict[str, Any]]:
    """Every line of the record under home that is an object in JSON, file by file in
    file-name order and in line order. A line that is neither is skipped.

    The files are those whose names match a pattern. With no pattern they are the spike
    logger's, whose lines alone say when the logger could not reach the browser. A file of
    the logger is named for the day the logger started, and a file of the capture for the day
    of its lines. Either way no file is read as a day: the ts of every line says when.
    """
    files = [
        path
        for pattern in patterns or ("spike-*.jsonl",)
        for path in (home / "watch").glob(pattern)
    ]
    for path in sorted(files):
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


def path_at_most(path: str) -> str:
    """The path, cut at PATH_AT_MOST characters and never inside one. The capture cuts one
    where it makes an event, and the reader cuts one from the older logger's record: an event
    is cut the same way from either."""
    return path[:PATH_AT_MOST]


def _can_be_written(text: str) -> bool:
    """Whether a file can hold text, as UTF-8. A lone surrogate cannot be written, and a page
    may put one in an attribute that names an element. An action with such a name is refused
    where it is made, as it is where it is read back."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
            "path": path_at_most(address.path or "/"),
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


def clock(when: float) -> str:
    """A moment in seconds since the epoch, in the form the logger writes a ts in."""
    return time.strftime(TS_FORMAT, time.localtime(when))


# --------------------------------------------------------- writing the record ---


def write_event(home: Path, event: Event, *, now: float | None = None) -> Path:
    """Append one activity event to the record under home, for the user alone.

    The file is home/watch/activity-YYYYMMDD.jsonl, named for the day the event is written,
    which is today when now is not given. It is never named for the day of the event's ts: an
    event written at midnight goes to the new day's file. The folder is made when it is
    missing, for the user alone, and so is the file. One line of JSON per event, and nothing
    else. Returns the file.

    Raises OSError when the line cannot be written. A capture that cannot write ends with the
    error and says so, rather than going on and recording nothing.
    """
    when = time.time() if now is None else now
    path = home / "watch" / f"activity-{time.strftime('%Y%m%d', time.localtime(when))}.jsonl"
    path.parent.mkdir(mode=PRIVATE, parents=True, exist_ok=True)
    opened = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE)
    with os.fdopen(opened, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
    return path


def note(home: Path, file: str, fields: Mapping[str, object]) -> None:
    """Append one line to a file of the record under home, for the user alone.

    The line is the fields as JSON. The folder is made when it is missing. Nothing is
    raised when the line cannot be written: a note must never break the run it notes.
    """
    try:
        watch = home / "watch"
        watch.mkdir(mode=PRIVATE, parents=True, exist_ok=True)
        opened = os.open(watch / file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE)
        with os.fdopen(opened, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(fields), ensure_ascii=False) + "\n")
    except OSError:
        return


def note_agent_run(home: Path, start: float, end: float, what: str, name: str | None) -> None:
    """Note that the agent drove a person's browser from start to end.

    what says what the run was: a task, a flow or a probe. name is the flow's, when it was
    one. The miner reads the note to leave those minutes out of the person's work.
    """
    note(
        home,
        "agent-runs.jsonl",
        {"start": clock(start), "end": clock(end), "what": what, "name": name},
    )


def note_flow_run(home: Path, ts: str, name: str, exit_status: int, error: str | None) -> None:
    """Note how a run of a saved flow ended. error is the class of the error that ended it,
    when one did."""
    fields: dict[str, object] = {"ts": ts, "name": name, "exit": exit_status}
    if error is not None:
        fields["error"] = error
    note(home, "flow-runs.jsonl", fields)


@dataclass(frozen=True)
class FlowRun:
    """One run of a saved flow: when it began, which flow, and how it ended.

    A run went well when exit is 0. error is the class of the error that ended a run that
    did not.
    """

    ts: str
    name: str
    exit: int
    error: str | None


def read_flow_runs(home: Path) -> list[FlowRun]:
    """Every run of a saved flow noted in the record under home, in the order written.

    A missing file is no run. A line whose ts is no time, or whose name, exit or error is
    not of its kind, is skipped. Nothing is written.
    """
    try:
        lines = (home / "watch" / "flow-runs.jsonl").read_bytes().splitlines()
    except FileNotFoundError:
        return []
    runs: list[FlowRun] = []
    for line in lines:
        data = _object(line)
        if data is None:
            continue
        ts, name = data.get("ts"), data.get("name")
        exit_status, error = data.get("exit"), data.get("error")
        if not (
            isinstance(ts, str)
            and _matches(TS_SHAPE, ts)
            and _when(ts) is not None
            and isinstance(name, str)
            and name
            and isinstance(exit_status, int)
            and not isinstance(exit_status, bool)
            and (error is None or isinstance(error, str))
        ):
            continue
        runs.append(FlowRun(ts, name, exit_status, error))
    return runs
