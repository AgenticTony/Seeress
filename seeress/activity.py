"""The activity event: one thing a person did in their browser, and nothing more about them.

The activity record holds more than Seeress needs to find repetitive work. A line of it
carries a page title and a full address, query included. An event is the part that may be
used: the site, the path without its query and its fragment, the kind of thing that
happened, the tab, and the second. What may reach a model is the site, the path, the hour
and counts, so an event never carries a page title, typed text or a full URL.

The shape of an event is contracts/activity-event.schema.json. Event.parse is that schema
written by hand, and a contract test keeps the two in agreement. read_events turns the lines
of the record into events, and skips every line that holds none.
"""

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# What an event says happened.
KINDS: tuple[str, ...] = ("navigate", "focus", "click", "input", "submit", "select")

# The patterns of the schema, as it writes them, searched for as its validator searches them.
# A pattern that must end at the end says so with `(?![\s\S])`, which means the same in Python
# and in the dialect JSON Schema names. `$` does not: in Python it also holds before a final
# newline.
TS_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?![\s\S])")
SITE_SHAPE = re.compile(r"^(?!www\.)[a-z0-9.-]+(?![\s\S])")
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


def read_events(home: Path, hours: float | None = None) -> list[Event]:
    """The activity events in the record under home, oldest first.

    home is the Seeress home. The record is read file by file in file-name order, and the
    events come back in time order. Events that share a second keep the order of the files
    and of the lines. With hours, only the events of the last hours are returned, by this
    machine's clock and with each ts read as the machine's local time. Nothing is written.
    """
    cutoff = None if hours is None else time.time() - hours * 3600
    events: list[Event] = []
    for path in sorted((home / "watch").glob("spike-*.jsonl")):
        for line in path.read_bytes().splitlines():
            event = _read_line(line)
            if event is None or (cutoff is not None and not _is_since(event.ts, cutoff)):
                continue
            events.append(event)
    # A ts is digits of a fixed width, the largest first, so its order as text is its order
    # in time. The sort is stable, so events that share a second keep their place.
    return sorted(events, key=lambda event: event.ts)


def _read_line(line: bytes) -> Event | None:
    """The event in one line of the record, or None when the line holds none."""
    try:
        data: object = json.loads(line)
    except ValueError:  # not JSON, or not text
        return None
    if not isinstance(data, dict):
        return None
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
    moment = _moment(ts)
    return moment is not None and moment >= cutoff


def _moment(ts: str) -> float | None:
    """When ts happened, in seconds, or None when it is no time this machine can place."""
    try:
        return time.mktime(time.strptime(ts, TS_FORMAT))
    except (ValueError, OverflowError):
        return None
