"""Reading the activity record into events.

One test per rule, on invented logs in a temporary folder. The record holds what the spike
logger writes: page titles, full addresses with their queries, and lines about the logger
itself. Only the site, the path, the kind, the tab and the second may come out of it.
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from seeress import activity

TS = "2026-11-20T09:15:05"
FILE = "spike-20261120.jsonl"
TITLE = "a title that must not survive"

# An invented log in every shape the spike logger writes.
LOG: list[dict[str, Any] | str] = [
    {"ts": "2026-11-20T09:15:02", "kind": "spike_start", "pid": 4242},
    {"ts": "2026-11-20T09:15:03", "kind": "connected", "port": 9222},
    {
        "ts": "2026-11-20T09:15:05",
        "kind": "tab_open",
        "target": "1a2b3c4d",
        "url": "https://www.example.org/invoices/7?tab=all#top",
        "title": "Invoices, a title that must not survive",
    },
    {
        "ts": "2026-11-20T09:15:05",
        "kind": "nav",
        "target": "1a2b3c4d",
        "url": "https://www.example.org/invoices/7?tab=all",
        "title": "Invoices",
    },
    {
        "ts": "2026-11-20T09:15:10",
        "kind": "focus",
        "url": "https://Mail.Example.net",
        "title": "Mail",
    },
    {"ts": "2026-11-20T09:15:12", "kind": "chrome_down", "error": "FileNotFoundError"},
    {
        "ts": "2026-11-20T09:15:12",
        "kind": "ws_error",
        "error": "ConnectionClosedError: no close frame received or sent",
    },
    {"ts": "2026-11-20T09:15:14", "kind": "focus_unavailable"},
    {
        "ts": "2026-11-20T09:15:20",
        "kind": "tab_close",
        "target": "1a2b3c4d",
        "url": "",
        "title": "",
    },
    {
        "ts": "2026-11-20T09:15:25",
        "kind": "nav",
        "target": "5e6f7a8b",
        "url": "chrome://inspect/#remote-debugging",
        "title": "Inspect",
    },
    {"ts": "2026-11-20T09:15:30", "kind": "spike_stop"},
    "this line is not JSON",
]


def write(home: Path, name: str, *lines: dict[str, Any] | str) -> None:
    """Write one log file of the record under home. A line that is text is written as it is."""
    watch = home / "watch"
    watch.mkdir(parents=True, exist_ok=True)
    (watch / name).write_text(
        "".join(
            line + "\n" if isinstance(line, str) else json.dumps(line) + "\n" for line in lines
        ),
        encoding="utf-8",
    )


def tab_line(kind: str, url: str, *, ts: str = TS) -> dict[str, Any]:
    """A line the logger writes for a tab. Every one carries the page's title."""
    return {"ts": ts, "kind": kind, "target": "1a2b3c4d", "url": url, "title": TITLE}


def focus_line(url: str, *, ts: str = TS) -> dict[str, Any]:
    """A line the logger writes when the active tab shows another page. It names no tab."""
    return {"ts": ts, "kind": "focus", "url": url, "title": TITLE}


def nav(site: str, ts: str = TS) -> dict[str, Any]:
    """A line the logger writes when a tab goes to the front page of a site."""
    return tab_line("nav", f"https://{site}/", ts=ts)


def navigate(site: str, ts: str = TS) -> activity.Event:
    """The event that nav() becomes."""
    return activity.Event(1, ts, "navigate", site, "/", "1a2b3c4d")


def ago(seconds: float) -> str:
    """A timestamp of the logger's form, this many seconds before now by this machine's clock."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))


def files_under(folder: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under folder, with when it was last written and what it holds."""
    return {
        str(path.relative_to(folder)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in folder.rglob("*")
        if path.is_file()
    }


# -------------------------------------------------------- a line of the log ---


@pytest.mark.parametrize("kind", ["nav", "tab_open"])
def test_a_tab_line_becomes_a_navigate_event(tmp_path: Path, kind: str) -> None:
    write(tmp_path, FILE, tab_line(kind, "https://example.org/invoices/7"))

    assert activity.read_events(tmp_path) == [
        activity.Event(1, TS, "navigate", "example.org", "/invoices/7", "1a2b3c4d")
    ]


def test_the_query_and_the_fragment_are_dropped(tmp_path: Path) -> None:
    write(tmp_path, FILE, tab_line("nav", "https://example.org/invoices/7?tab=all&hide=paid#top"))

    assert [event.path for event in activity.read_events(tmp_path)] == ["/invoices/7"]


def test_the_title_never_leaves_the_log(tmp_path: Path) -> None:
    write(
        tmp_path,
        FILE,
        tab_line("nav", "https://example.org/invoices/7"),
        focus_line("https://example.org/inbox"),
    )

    events = activity.read_events(tmp_path)

    assert events == [
        activity.Event(1, TS, "navigate", "example.org", "/invoices/7", "1a2b3c4d"),
        activity.Event(1, TS, "focus", "example.org", "/inbox", None),
    ]
    assert TITLE not in repr(events)


def test_a_focus_line_becomes_a_focus_event_with_no_tab(tmp_path: Path) -> None:
    write(tmp_path, FILE, focus_line("https://mail.example.net/inbox"))

    assert activity.read_events(tmp_path) == [
        activity.Event(1, TS, "focus", "mail.example.net", "/inbox", None)
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param({"ts": TS, "kind": "spike_start", "pid": 4242}, id="spike_start"),
        pytest.param({"ts": TS, "kind": "connected", "port": 9222}, id="connected"),
        pytest.param(
            {"ts": TS, "kind": "chrome_down", "error": "FileNotFoundError"}, id="chrome_down"
        ),
        pytest.param(
            {"ts": TS, "kind": "ws_error", "error": "ConnectionClosedError"},
            id="ws_error",
        ),
        pytest.param(
            {
                "ts": TS,
                "kind": "focus_unavailable",
                "note": "osascript blocked in this context; tab events only",
            },
            id="focus_unavailable",
        ),
        pytest.param(
            {
                "ts": TS,
                "kind": "tab_close",
                "target": "1a2b3c4d",
                "url": "https://example.org/",
                "title": "",
            },
            id="tab_close",
        ),
        pytest.param({"ts": TS, "kind": "spike_stop"}, id="spike_stop"),
    ],
)
def test_any_other_kind_of_line_is_skipped(tmp_path: Path, line: dict[str, Any]) -> None:
    """The logger writes these about itself and its tabs. None says the person went to a page.

    The logger closes a tab with an empty address. This one carries a page's, so that only
    its kind keeps it out.
    """
    write(tmp_path, FILE, line, nav("example.org"))

    assert activity.read_events(tmp_path) == [navigate("example.org")]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            {"kind": "nav", "target": "1a2b3c4d", "url": "https://example.org/", "title": TITLE},
            id="no ts",
        ),
        pytest.param({**nav("example.org"), "ts": 20261120091505}, id="a ts that is not text"),
    ],
)
def test_a_line_without_a_timestamp_is_skipped(tmp_path: Path, line: dict[str, Any]) -> None:
    write(tmp_path, FILE, line, nav("example.org"))

    assert activity.read_events(tmp_path) == [navigate("example.org")]


def test_a_line_that_is_not_json_is_skipped_and_the_rest_is_read(tmp_path: Path) -> None:
    write(
        tmp_path, FILE, nav("first.example.org"), "this line is not JSON", nav("second.example.org")
    )

    assert activity.read_events(tmp_path) == [
        navigate("first.example.org"),
        navigate("second.example.org"),
    ]


def test_a_line_of_json_that_is_no_object_is_skipped(tmp_path: Path) -> None:
    """The logger writes one object per line. Anything else holds no event, and stops nothing."""
    write(tmp_path, FILE, nav("first.example.org"), "[]", nav("second.example.org"))

    assert activity.read_events(tmp_path) == [
        navigate("first.example.org"),
        navigate("second.example.org"),
    ]


def test_a_line_that_already_is_an_activity_event_is_read_back(tmp_path: Path) -> None:
    """A click is no line the spike logger writes, so this event was read, not made."""
    click = activity.Event(1, TS, "click", "example.org", "/invoices/7", "1a2b3c4d")
    write(tmp_path, FILE, click.as_dict())

    assert activity.read_events(tmp_path) == [click]


def test_an_activity_event_the_schema_refuses_is_skipped(tmp_path: Path) -> None:
    """An event never carries a page title, so a line with one is no event."""
    refused = {
        **activity.Event(1, TS, "click", "example.org", "/invoices/7", None).as_dict(),
        "title": TITLE,
    }
    write(tmp_path, FILE, refused)

    assert activity.read_events(tmp_path) == []


def test_a_line_of_version_1_that_is_no_event_is_skipped_whatever_its_kind(tmp_path: Path) -> None:
    """A line with a v of 1 says it is an event. One that is not a well-shaped event is not
    then read as a line of the logger's instead."""
    write(tmp_path, FILE, {**focus_line("https://example.org/inbox"), "v": 1})

    assert activity.read_events(tmp_path) == []


# --------------------------------------------------------- a page's address ---


@pytest.mark.parametrize(
    "line",
    [
        pytest.param({"ts": TS, "kind": "nav", "target": "1a2b3c4d", "title": TITLE}, id="no url"),
        pytest.param({**nav("example.org"), "url": None}, id="a url of null"),
        pytest.param({**nav("example.org"), "url": 9222}, id="a url that is not text"),
    ],
)
def test_a_line_that_names_no_address_is_skipped(tmp_path: Path, line: dict[str, Any]) -> None:
    write(tmp_path, FILE, line, nav("example.org"))

    assert activity.read_events(tmp_path) == [navigate("example.org")]


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("chrome://inspect/#remote-debugging", id="chrome"),
        pytest.param("about:blank", id="about"),
        pytest.param("file:///Users/someone/a-page.html", id="file"),
        pytest.param("", id="a tab with no address yet"),
    ],
)
def test_a_page_that_is_not_on_the_web_is_skipped(tmp_path: Path, url: str) -> None:
    write(tmp_path, FILE, tab_line("nav", url))

    assert activity.read_events(tmp_path) == []


def test_an_address_with_no_host_is_skipped(tmp_path: Path) -> None:
    write(tmp_path, FILE, tab_line("nav", "https:///invoices/7"))

    assert activity.read_events(tmp_path) == []


def test_an_address_that_cannot_be_split_is_skipped(tmp_path: Path) -> None:
    """Python cannot split a bracketed host it never closes. The reader must go on past it."""
    write(tmp_path, FILE, tab_line("nav", "https://[::1/invoices/7"), nav("example.org"))

    assert activity.read_events(tmp_path) == [navigate("example.org")]


def test_a_host_is_read_in_lower_case_without_www(tmp_path: Path) -> None:
    write(tmp_path, FILE, tab_line("nav", "https://WWW.Example.Org/invoices/7"))

    assert [event.site for event in activity.read_events(tmp_path)] == ["example.org"]


def test_a_host_still_starting_with_www_holds_no_event(tmp_path: Path) -> None:
    """One www. is removed, and only one. What is left must then satisfy the schema."""
    write(tmp_path, FILE, tab_line("nav", "https://www.www.example.org/"))

    assert activity.read_events(tmp_path) == []


# ---------------------------------------------------------------- the files ---


def test_the_log_in_every_shape_the_logger_writes_holds_three_events(tmp_path: Path) -> None:
    write(tmp_path, FILE, *LOG)

    assert activity.read_events(tmp_path) == [
        activity.Event(
            1, "2026-11-20T09:15:05", "navigate", "example.org", "/invoices/7", "1a2b3c4d"
        ),
        activity.Event(
            1, "2026-11-20T09:15:05", "navigate", "example.org", "/invoices/7", "1a2b3c4d"
        ),
        activity.Event(1, "2026-11-20T09:15:10", "focus", "mail.example.net", "/", None),
    ]


def test_events_from_two_files_come_back_in_time_order(tmp_path: Path) -> None:
    """The files are read by name, and a later file can hold an earlier second."""
    write(tmp_path, "spike-20261120.jsonl", nav("two.example.org", "2026-11-20T09:15:30"))
    write(
        tmp_path,
        "spike-20261121.jsonl",
        nav("one.example.org", "2026-11-20T09:15:05"),
        nav("three.example.org", "2026-11-20T09:15:45"),
    )

    assert activity.read_events(tmp_path) == [
        navigate("one.example.org", "2026-11-20T09:15:05"),
        navigate("two.example.org", "2026-11-20T09:15:30"),
        navigate("three.example.org", "2026-11-20T09:15:45"),
    ]


def test_events_that_share_a_second_keep_file_and_line_order(tmp_path: Path) -> None:
    """One to four is neither alphabetical nor the order the system lists the files in."""
    write(tmp_path, "spike-20261120.jsonl", nav("one.example.org"), nav("two.example.org"))
    write(tmp_path, "spike-20261121.jsonl", nav("three.example.org"))
    write(tmp_path, "spike-20261122.jsonl", nav("four.example.org"))

    assert activity.read_events(tmp_path) == [
        navigate("one.example.org"),
        navigate("two.example.org"),
        navigate("three.example.org"),
        navigate("four.example.org"),
    ]


def test_hours_keeps_only_the_recent_events(tmp_path: Path) -> None:
    """The old event is a day away. The clock changing by an hour cannot bring it near."""
    old, recent = ago(24 * 3600), ago(30 * 60)
    write(tmp_path, FILE, nav("old.example.org", old), nav("new.example.org", recent))

    assert activity.read_events(tmp_path, hours=1) == [navigate("new.example.org", recent)]
    assert activity.read_events(tmp_path) == [
        navigate("old.example.org", old),
        navigate("new.example.org", recent),
    ]


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param("2026-13-45T25:61:61", id="no real date"),
        pytest.param("0999-01-01T00:00:00", id="a year this machine's clock may not place"),
    ],
)
def test_under_hours_an_event_whose_ts_is_no_time_is_not_kept(tmp_path: Path, ts: str) -> None:
    """The schema judges the shape of a ts, not whether it is a time. Under hours, a ts the
    clock cannot read cannot be shown to be recent."""
    recent = ago(30 * 60)
    no_time = {
        "v": 1,
        "ts": ts,
        "kind": "click",
        "site": "example.org",
        "path": "/",
        "target": None,
    }
    write(tmp_path, FILE, no_time, nav("example.org", recent))

    assert activity.read_events(tmp_path, hours=1) == [navigate("example.org", recent)]


def test_the_log_files_are_exactly_as_they_were(tmp_path: Path) -> None:
    """Nothing is written: no file changes, none appears, and none is written back as it was."""
    write(tmp_path, "spike-20261119.jsonl", nav("example.org"))
    write(
        tmp_path,
        "spike-20261120.jsonl",
        "this line is not JSON",
        focus_line("https://example.org/inbox"),
    )
    before = files_under(tmp_path)

    activity.read_events(tmp_path)
    activity.read_events(tmp_path, hours=1)

    assert files_under(tmp_path) == before
