"""Reading the activity record into events.

One test per rule, on invented logs in a temporary folder. The record holds what the spike
logger writes: page titles, full addresses with their queries, and lines about the logger
itself. Only the site, the path, the kind, the tab and the second may come out of it.
"""

import errno
import io
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from seeress import activity, miner, watcher

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


def down(ts: str = TS) -> dict[str, Any]:
    """A line the logger writes every five seconds while it cannot reach Chrome."""
    return {"ts": ts, "kind": "chrome_down", "error": "FileNotFoundError"}


def connected(ts: str = TS) -> dict[str, Any]:
    """A line the logger writes when it has reached Chrome."""
    return {"ts": ts, "kind": "connected", "port": 9222}


def agent_run(start: str, end: str) -> dict[str, Any]:
    """A line of the file of agent runs: from when to when the agent drove the browser."""
    return {"start": start, "end": end, "what": "task", "name": None}


def between(start: str, end: str) -> activity.Interval:
    """The time from one timestamp of the logger's form to another."""
    return activity.Interval(activity.epoch(start), activity.epoch(end))


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


def test_a_tab_is_read_as_the_browser_names_it_in_upper_case(tmp_path: Path) -> None:
    """The reader refused the upper case from the first. So it read no line the spike logger
    wrote about a tab, and on the real record only the lines that name no tab were read."""
    line = tab_line("nav", "https://example.org/invoices/7")
    line["target"] = "D3DED672"
    write(tmp_path, FILE, line)

    assert activity.read_events(tmp_path) == [
        activity.Event(1, TS, "navigate", "example.org", "/invoices/7", "D3DED672")
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


def test_an_event_made_from_a_line_of_the_logger_has_no_action(tmp_path: Path) -> None:
    """Nothing the spike logger wrote says what was done in the page."""
    write(tmp_path, FILE, tab_line("nav", "https://example.org/invoices/7"))

    (event,) = activity.read_events(tmp_path)

    assert event.action is None
    assert list(event.as_dict()) == ["v", "ts", "kind", "site", "path", "target"]


def test_an_action_is_kept_through_the_event() -> None:
    """An event made with an action is read back as the same event, with a field or without."""
    typed = activity.Event(
        1,
        TS,
        "input",
        "example.org",
        "/search",
        "1a2b3c4d",
        activity.Action("input", "input", 'input[name="q"]', "password"),
    )
    clicked = activity.Event(
        1,
        TS,
        "click",
        "example.org",
        "/search",
        "1a2b3c4d",
        activity.Action("click", "button", "button:nth-child(2)", None),
    )

    assert activity.Event.parse(typed.as_dict()) == typed
    assert activity.Event.parse(clicked.as_dict()) == clicked
    assert typed.action is not None
    assert typed.action.as_dict() == {
        "type": "input",
        "tag": "input",
        "fingerprint": 'input[name="q"]',
        "field": "password",
    }
    assert clicked.action is not None
    assert "field" not in clicked.action.as_dict()


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


def test_a_line_of_another_version_is_skipped_whatever_its_kind(tmp_path: Path) -> None:
    """A line that names a version is read at the version this reader knows, or not at all.
    It is never taken for a line of the logger's instead."""
    write(tmp_path, FILE, {**focus_line("https://example.org/inbox"), "v": 2})

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


def test_a_host_with_an_underscore_is_kept(tmp_path: Path) -> None:
    """Intranet servers are often named with an underscore, and a browser accepts that."""
    write(tmp_path, FILE, tab_line("nav", "https://Intranet_Server/invoices/7"))

    assert [event.site for event in activity.read_events(tmp_path)] == ["intranet_server"]


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
    activity.write_event(tmp_path, click(), now=WRITTEN_AT)
    before = files_under(tmp_path)

    activity.read_events(tmp_path)
    activity.read_events(tmp_path, hours=1)

    assert files_under(tmp_path) == before


# ------------------------------------------------- time, and an interval of it ---


def test_a_timestamp_is_read_as_the_local_time_of_this_machine() -> None:
    ts = "2026-11-20T09:15:05"

    assert activity.epoch(ts) == time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param("2026-13-45T25:61:61", id="no real date"),
        pytest.param("2026-11-20", id="a date only"),
        pytest.param("0999-01-01T00:00:00", id="a year this machine's clock cannot place"),
    ],
)
def test_a_timestamp_that_is_no_time_cannot_be_read(ts: str) -> None:
    """The schema judges the shape of a ts, not whether it is one."""
    with pytest.raises(ValueError):
        activity.epoch(ts)


def test_an_interval_holds_both_of_its_ends_and_what_lies_between() -> None:
    run = activity.Interval(10.0, 20.0)

    assert [run.holds(moment) for moment in (9.0, 10.0, 15.0, 20.0, 21.0)] == [
        False,
        True,
        True,
        True,
        False,
    ]


def test_an_interval_that_ends_before_it_starts_is_empty() -> None:
    backwards = activity.Interval(20.0, 10.0)

    assert backwards.empty
    assert not backwards.holds(15.0)
    assert not activity.Interval(10.0, 10.0).empty


# --------------------------------------------------------------- the outages ---


def test_a_run_of_chrome_down_ends_at_connected(tmp_path: Path) -> None:
    """While it cannot reach Chrome, the logger writes chrome_down every five seconds."""
    write(
        tmp_path,
        FILE,
        nav("example.org"),
        down("2026-11-20T09:15:00"),
        down("2026-11-20T09:15:05"),
        down("2026-11-20T09:15:10"),
        connected("2026-11-20T09:15:15"),
        nav("example.org", "2026-11-20T09:15:20"),
    )

    found = activity.read_outages(tmp_path)

    assert found == [between("2026-11-20T09:15:00", "2026-11-20T09:15:15")]


@pytest.mark.parametrize(
    "last",
    [
        pytest.param(down("2026-11-20T09:15:10"), id="another chrome_down"),
        pytest.param({"ts": "2026-11-20T09:15:12", "kind": "spike_stop"}, id="a spike_stop"),
    ],
)
def test_a_run_that_never_ended_ends_at_the_last_line(tmp_path: Path, last: dict[str, Any]) -> None:
    write(tmp_path, FILE, down("2026-11-20T09:15:00"), down("2026-11-20T09:15:05"), last)

    found = activity.read_outages(tmp_path)

    assert found == [between("2026-11-20T09:15:00", last["ts"])]


def test_two_runs_separated_by_connected_are_two(tmp_path: Path) -> None:
    write(
        tmp_path,
        FILE,
        down("2026-11-20T09:15:00"),
        connected("2026-11-20T09:15:10"),
        nav("example.org", "2026-11-20T09:16:00"),
        down("2026-11-20T09:20:00"),
        down("2026-11-20T09:20:05"),
        connected("2026-11-20T09:20:10"),
    )

    found = activity.read_outages(tmp_path)

    assert found == [
        between("2026-11-20T09:15:00", "2026-11-20T09:15:10"),
        between("2026-11-20T09:20:00", "2026-11-20T09:20:10"),
    ]


def test_a_run_that_straddles_two_files_is_one(tmp_path: Path) -> None:
    """The logger was restarted inside an outage on 2026-11-24, and wrote a new file."""
    write(
        tmp_path, "spike-20261120.jsonl", down("2026-11-20T23:59:50"), down("2026-11-20T23:59:55")
    )
    write(
        tmp_path,
        "spike-20261121.jsonl",
        {"ts": "2026-11-21T00:00:00", "kind": "spike_start", "pid": 4242},
        connected("2026-11-21T00:00:05"),
    )

    found = activity.read_outages(tmp_path)

    assert found == [between("2026-11-20T23:59:50", "2026-11-21T00:00:05")]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("this line is not JSON", id="not JSON"),
        pytest.param({"kind": "chrome_down"}, id="no ts"),
        pytest.param(
            {"ts": "2026-13-45T25:61:61", "kind": "chrome_down"}, id="a ts that is no time"
        ),
        pytest.param({"ts": 20261120091505, "kind": "chrome_down"}, id="a ts that is not text"),
    ],
)
def test_a_line_that_cannot_be_read_is_skipped_and_the_run_goes_on(
    tmp_path: Path, line: Any
) -> None:
    write(tmp_path, FILE, down("2026-11-20T09:15:00"), line, connected("2026-11-20T09:15:10"))

    found = activity.read_outages(tmp_path)

    assert found == [between("2026-11-20T09:15:00", "2026-11-20T09:15:10")]


def test_a_connected_line_alone_starts_no_outage(tmp_path: Path) -> None:
    write(tmp_path, FILE, connected("2026-11-20T09:15:00"), nav("example.org"))

    assert activity.read_outages(tmp_path) == []


def test_under_hours_an_outage_that_ended_before_the_cutoff_is_left_out(tmp_path: Path) -> None:
    write(tmp_path, FILE, down(ago(3 * 3600)), connected(ago(2 * 3600)), nav("example.org"))

    assert activity.read_outages(tmp_path, hours=1) == []
    assert activity.read_outages(tmp_path) == [between(ago(3 * 3600), ago(2 * 3600))]


def test_under_hours_an_outage_that_is_still_running_is_kept(tmp_path: Path) -> None:
    """A run that never ended may have begun any time ago, and the record may have ended
    inside it a day ago. It is still going on."""
    write(tmp_path, FILE, down(ago(40 * 3600)), down(ago(39 * 3600)))

    found = activity.read_outages(tmp_path, hours=1)

    assert found == [between(ago(40 * 3600), ago(39 * 3600))]


def test_no_record_is_no_outage(tmp_path: Path) -> None:
    assert activity.read_outages(tmp_path) == []


# ------------------------------------------------------------ the agent's runs ---

RUNS = "agent-runs.jsonl"


def a_run(start: str, end: str) -> activity.Interval:
    """The run a note of these two moments reads back as: it reaches past its end by the
    time the logger may be late."""
    return activity.Interval(activity.epoch(start), activity.epoch(end) + activity.POLL_LAG)


def test_no_file_of_agent_runs_is_no_run(tmp_path: Path) -> None:
    assert activity.read_agent_runs(tmp_path) == []


def test_each_line_that_reads_is_a_run_in_the_order_written(tmp_path: Path) -> None:
    write(
        tmp_path,
        RUNS,
        agent_run("2026-11-20T09:00:00", "2026-11-20T09:10:00"),
        {
            "start": "2026-11-20T08:20:00",
            "end": "2026-11-20T08:25:00",
            "what": "flow",
            "name": "check-invoices",
        },
    )

    found = activity.read_agent_runs(tmp_path)

    assert found == [
        a_run("2026-11-20T09:00:00", "2026-11-20T09:10:00"),
        a_run("2026-11-20T08:20:00", "2026-11-20T08:25:00"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param({"start": "2026-11-20T09:00:00", "what": "task", "name": None}, id="no end"),
        pytest.param({"end": "2026-11-20T09:10:00", "what": "task", "name": None}, id="no start"),
        pytest.param(
            agent_run("2026-11-20T09:10:00", "2026-11-20T09:00:00"), id="end before start"
        ),
        pytest.param(agent_run("2026-11-20", "2026-11-20T09:10:00"), id="start of another shape"),
        pytest.param(
            agent_run("2026-13-45T25:61:61", "2026-11-20T09:10:00"), id="start that is no time"
        ),
        pytest.param(
            {"start": 1763926800, "end": "2026-11-20T09:10:00", "what": "task", "name": None},
            id="start that is not text",
        ),
        pytest.param("this line is not JSON", id="not JSON"),
        pytest.param([], id="not an object"),
    ],
)
def test_a_line_of_the_runs_that_does_not_read_is_skipped(tmp_path: Path, line: Any) -> None:
    write(tmp_path, RUNS, line, agent_run("2026-11-20T10:00:00", "2026-11-20T10:05:00"))

    found = activity.read_agent_runs(tmp_path)

    assert found == [a_run("2026-11-20T10:00:00", "2026-11-20T10:05:00")]


def test_a_run_that_begins_and_ends_in_one_second_is_a_run(tmp_path: Path) -> None:
    write(tmp_path, RUNS, agent_run("2026-11-20T10:00:00", "2026-11-20T10:00:00"))

    assert activity.read_agent_runs(tmp_path) == [
        a_run("2026-11-20T10:00:00", "2026-11-20T10:00:00")
    ]


def test_under_hours_a_run_that_ended_before_the_cutoff_is_left_out(tmp_path: Path) -> None:
    write(
        tmp_path,
        RUNS,
        agent_run(ago(3 * 3600), ago(2 * 3600)),
        agent_run(ago(20 * 60), ago(10 * 60)),
    )

    found = activity.read_agent_runs(tmp_path, hours=1)

    assert found == [a_run(ago(20 * 60), ago(10 * 60))]


def test_the_readers_of_outages_and_runs_write_nothing(tmp_path: Path) -> None:
    write(tmp_path, FILE, down("2026-11-20T09:15:00"), connected(), nav("example.org"))
    write(tmp_path, RUNS, agent_run(TS, "2026-11-20T09:20:00"))
    before = files_under(tmp_path)

    activity.read_outages(tmp_path)
    activity.read_outages(tmp_path, hours=1)
    activity.read_agent_runs(tmp_path)
    activity.read_agent_runs(tmp_path, hours=1)

    assert files_under(tmp_path) == before


# --------------------------------------------------------- writing the record ---

FLOW_RUNS = "flow-runs.jsonl"


def test_a_run_of_the_agent_is_noted_as_one_line_that_reads_back(tmp_path: Path) -> None:
    activity.note_agent_run(
        tmp_path,
        activity.epoch("2026-11-20T09:00:00"),
        activity.epoch("2026-11-20T09:05:00"),
        "flow",
        "check-invoices",
    )

    file = tmp_path / "watch" / "agent-runs.jsonl"
    (line,) = file.read_text(encoding="utf-8").splitlines()
    assert json.loads(line) == {
        "start": "2026-11-20T09:00:00",
        "end": "2026-11-20T09:05:00",
        "what": "flow",
        "name": "check-invoices",
    }
    assert activity.read_agent_runs(tmp_path) == [
        a_run("2026-11-20T09:00:00", "2026-11-20T09:05:00")
    ]


def test_a_run_of_a_flow_that_failed_is_noted_as_one_line_that_reads_back(tmp_path: Path) -> None:
    activity.note_flow_run(tmp_path, TS, "check-invoices", 1, "BrowserError")

    file = tmp_path / "watch" / FLOW_RUNS
    (line,) = file.read_text(encoding="utf-8").splitlines()
    assert json.loads(line) == {
        "ts": TS,
        "name": "check-invoices",
        "exit": 1,
        "error": "BrowserError",
    }
    assert activity.read_flow_runs(tmp_path) == [
        activity.FlowRun(TS, "check-invoices", 1, "BrowserError")
    ]


def test_a_run_of_a_flow_that_went_well_is_noted_without_an_error(tmp_path: Path) -> None:
    activity.note_flow_run(tmp_path, TS, "check-invoices", 0, None)

    file = tmp_path / "watch" / FLOW_RUNS
    (line,) = file.read_text(encoding="utf-8").splitlines()
    assert "error" not in json.loads(line)
    assert activity.read_flow_runs(tmp_path) == [activity.FlowRun(TS, "check-invoices", 0, None)]


def test_a_note_that_cannot_be_written_breaks_nothing(tmp_path: Path) -> None:
    """A note must never break the run it notes."""
    (tmp_path / "watch").write_text("not a folder")

    activity.note_agent_run(tmp_path, 0.0, 1.0, "task", None)
    activity.note_flow_run(tmp_path, TS, "check-invoices", 0, None)


def test_a_note_is_for_the_user_alone(tmp_path: Path) -> None:
    activity.note_agent_run(tmp_path, 0.0, 1.0, "task", None)

    file = tmp_path / "watch" / "agent-runs.jsonl"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700


def test_no_file_of_flow_runs_is_no_run(tmp_path: Path) -> None:
    assert activity.read_flow_runs(tmp_path) == []


def test_the_runs_of_flows_are_read_in_the_order_written(tmp_path: Path) -> None:
    write(
        tmp_path,
        FLOW_RUNS,
        {"ts": TS, "name": "alpha", "exit": 0},
        {"ts": "2026-11-20T09:20:00", "name": "beta", "exit": 1, "error": "BrowserError"},
    )

    assert activity.read_flow_runs(tmp_path) == [
        activity.FlowRun(TS, "alpha", 0, None),
        activity.FlowRun("2026-11-20T09:20:00", "beta", 1, "BrowserError"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("this line is not JSON", id="not JSON"),
        pytest.param({"name": "alpha", "exit": 0}, id="no ts"),
        pytest.param(
            {"ts": "2026-11-20", "name": "alpha", "exit": 0}, id="a ts that is no timestamp"
        ),
        pytest.param(
            {"ts": "2026-11-20T9:15:02", "name": "alpha", "exit": 0},
            id="a ts the clock can read that is not of the logger's form",
        ),
        pytest.param(
            {"ts": "2026-13-45T25:61:61", "name": "alpha", "exit": 0}, id="a ts that is no time"
        ),
        pytest.param({"ts": TS, "exit": 0}, id="no name"),
        pytest.param({"ts": TS, "name": "", "exit": 0}, id="a name empty"),
        pytest.param({"ts": TS, "name": "alpha"}, id="no exit"),
        pytest.param({"ts": TS, "name": "alpha", "exit": "0"}, id="an exit that is not a number"),
        pytest.param({"ts": TS, "name": "alpha", "exit": True}, id="an exit that is true"),
        pytest.param(
            {"ts": TS, "name": "alpha", "exit": 0, "error": 7}, id="an error that is not text"
        ),
        pytest.param([], id="not an object"),
    ],
)
def test_a_line_of_the_flow_runs_that_does_not_read_is_skipped(tmp_path: Path, line: Any) -> None:
    write(tmp_path, FLOW_RUNS, line, {"ts": TS, "name": "alpha", "exit": 0})

    assert activity.read_flow_runs(tmp_path) == [activity.FlowRun(TS, "alpha", 0, None)]


# --------------------------------------------------------- what the capture writes ---


def click(ts: str = "2026-11-20T09:15:05") -> activity.Event:
    """An event as the capture makes one: what a person did in a page."""
    return activity.Event(
        1,
        ts,
        "click",
        "example.org",
        "/invoices/7",
        "1a2b3c4d",
        activity.Action("click", "button", 'button[data-testid="save"]', None),
    )


def moment(local_time: str) -> float:
    """A moment of this machine's local time, in seconds since the epoch."""
    return time.mktime(time.strptime(local_time, "%Y-%m-%dT%H:%M:%S"))


WRITTEN_AT = moment("2026-11-25T10:30:00")


def test_an_event_is_written_as_one_line_of_json(tmp_path: Path) -> None:
    event = click()

    path = activity.write_event(tmp_path, event, now=WRITTEN_AT)

    assert path == tmp_path / "watch" / "activity-20261125.jsonl"
    assert path.read_bytes() == (json.dumps(event.as_dict(), ensure_ascii=False) + "\n").encode()


def test_a_file_is_named_for_the_day_its_event_is_written(tmp_path: Path) -> None:
    """And never for the day of the event. An event written at midnight goes to the new day's
    file."""
    before_midnight = moment("2026-11-25T23:59:59")

    late = activity.write_event(tmp_path, click(), now=before_midnight)
    after = activity.write_event(tmp_path, click("2026-11-26T00:00:00"), now=before_midnight + 1)

    assert late.name == "activity-20261125.jsonl"
    assert after.name == "activity-20261126.jsonl"


def test_a_file_is_named_for_today_when_no_moment_is_given(tmp_path: Path) -> None:
    path = activity.write_event(tmp_path, click())

    assert path.name == f"activity-{time.strftime('%Y%m%d')}.jsonl"


def test_each_event_is_one_more_line(tmp_path: Path) -> None:
    first, second = click(), click("2026-11-20T09:15:06")
    activity.write_event(tmp_path, first, now=WRITTEN_AT)

    path = activity.write_event(tmp_path, second, now=WRITTEN_AT)

    assert path.read_text(encoding="utf-8").splitlines() == [
        json.dumps(first.as_dict(), ensure_ascii=False),
        json.dumps(second.as_dict(), ensure_ascii=False),
    ]


def test_an_event_is_read_back_as_it_was_written(tmp_path: Path) -> None:
    """Through its file, and with a fingerprint outside ASCII."""
    event = activity.Event(
        1,
        "2026-11-20T09:15:05",
        "input",
        "example.org",
        "/search",
        "1a2b3c4d",
        activity.Action("input", "input", 'input[aria-label="Sök"]', "search"),
    )
    activity.write_event(tmp_path, event, now=WRITTEN_AT)

    assert activity.read_events(tmp_path) == [event]


def test_nothing_is_written_but_the_event(tmp_path: Path) -> None:
    """One file under the home, and it holds the event and nothing else."""
    event = click()

    activity.write_event(tmp_path, event, now=WRITTEN_AT)

    files = [str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()]
    assert files == ["watch/activity-20261125.jsonl"]
    assert (tmp_path / files[0]).read_text(encoding="utf-8") == (
        json.dumps(event.as_dict(), ensure_ascii=False) + "\n"
    )


def test_an_event_is_for_the_user_alone(tmp_path: Path) -> None:
    path = activity.write_event(tmp_path, click(), now=WRITTEN_AT)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_line_that_cannot_be_written_is_raised(tmp_path: Path) -> None:
    """The capture that calls it ends with the error, rather than going on and recording
    nothing."""
    (tmp_path / "watch").write_text("a file where the folder should be", encoding="utf-8")

    with pytest.raises(OSError):
        activity.write_event(tmp_path, click(), now=WRITTEN_AT)


def test_the_capture_s_files_are_read_beside_the_logger_s(tmp_path: Path) -> None:
    write(tmp_path, "spike-20261120.jsonl", nav("example.org", ts="2026-11-20T09:15:02"))
    activity.write_event(tmp_path, click("2026-11-20T09:15:03"), now=WRITTEN_AT)
    write(tmp_path, "spike-20261121.jsonl", nav("other.example", ts="2026-11-20T09:15:04"))

    events = activity.read_events(tmp_path)

    assert events == [
        navigate("example.org", "2026-11-20T09:15:02"),
        click("2026-11-20T09:15:03"),
        navigate("other.example", "2026-11-20T09:15:04"),
    ]


def test_events_that_share_a_second_keep_the_order_of_the_files(tmp_path: Path) -> None:
    """A file of the capture comes before a file of the logger, whichever was written first."""
    write(tmp_path, "spike-20261120.jsonl", nav("logger.example", ts="2026-11-20T09:15:03"))
    activity.write_event(tmp_path, click("2026-11-20T09:15:03"), now=WRITTEN_AT)

    events = activity.read_events(tmp_path)

    assert [event.kind for event in events] == ["click", "navigate"]


def test_no_file_of_the_record_but_the_two_kinds_is_read(tmp_path: Path) -> None:
    """Whatever a line of another file holds, it is no event."""
    for name in ("agent-runs.jsonl", "flow-runs.jsonl", "activity-20261120.jsonl.bak"):
        write(tmp_path, name, nav("example.org"), {**click().as_dict()})

    assert activity.read_events(tmp_path) == []


def test_only_the_logger_s_own_files_say_when_it_could_not_reach_the_browser(
    tmp_path: Path,
) -> None:
    """An event of the capture's, a note of a flow's run, or a file of any other name, ends no
    outage, whatever its ts. timeline.jsonl is invented: it sorts after the logger's files,
    where a line with a later ts could move the end of an outage that never ended."""
    write(
        tmp_path, "spike-20261120.jsonl", down("2026-11-20T09:15:00"), down("2026-11-20T09:15:05")
    )
    write(tmp_path, "flow-runs.jsonl", {"ts": "2026-11-20T09:15:40", "name": "alpha", "exit": 0})
    write(tmp_path, "timeline.jsonl", nav("example.org", ts="2026-11-20T09:15:45"))
    activity.write_event(tmp_path, click("2026-11-20T09:15:50"), now=WRITTEN_AT)

    assert activity.read_outages(tmp_path) == [
        between("2026-11-20T09:15:00", "2026-11-20T09:15:05")
    ]


def test_a_run_reaches_past_its_end_by_the_logger_s_poll(tmp_path: Path) -> None:
    """The logger writes the page in front once every five seconds, so a page the agent
    opened can be written after the run was noted as ended."""
    write(tmp_path, RUNS, agent_run("2026-11-20T09:00:00", "2026-11-20T09:05:00"))

    (run,) = activity.read_agent_runs(tmp_path)

    assert run.end - run.start == 300 + activity.POLL_LAG


def test_the_poll_of_the_logger_is_the_one_the_reader_allows_for() -> None:
    assert int(watcher.POLL_FOCUS_S) == activity.POLL_LAG


def test_under_hours_a_run_that_straddles_the_cutoff_is_kept(tmp_path: Path) -> None:
    write(tmp_path, RUNS, agent_run(ago(90 * 60), ago(30 * 60)))

    assert activity.read_agent_runs(tmp_path, hours=1) == [a_run(ago(90 * 60), ago(30 * 60))]


def test_under_hours_an_outage_that_straddles_the_cutoff_is_kept(tmp_path: Path) -> None:
    write(tmp_path, FILE, down(ago(90 * 60)), connected(ago(30 * 60)))

    assert activity.read_outages(tmp_path, hours=1) == [between(ago(90 * 60), ago(30 * 60))]


# ---------------------------------------------------------------- the schedule ---


def test_a_second_is_due_in_the_schedule_or_not() -> None:
    due = activity.epoch

    assert activity.due(due("2026-11-20T09:15:00"))  # Friday, at work
    assert not activity.due(due("2026-11-20T07:59:59"))  # before the first hour
    assert activity.due(due("2026-11-20T08:00:00"))
    assert activity.due(due("2026-11-20T17:59:59"))  # in the last hour
    assert not activity.due(due("2026-11-20T18:00:00"))
    assert not activity.due(due("2026-11-21T10:00:00"))  # a Saturday
    assert activity.due(due("2026-11-21T10:00:00"), activity.Schedule(frozenset({5}), 10, 11))


def test_a_second_is_due_exactly_when_a_minute_from_it_holds_scheduled_time() -> None:
    """Two readings of one schedule: due, for a second, and scheduled, for a stretch. They
    agree on every minute of three days, one of them a Sunday."""
    friday = activity.epoch("2026-11-20T00:00:00")

    for minute in range(3 * 24 * 60):
        when = friday + minute * 60
        assert activity.due(when) == (miner.scheduled(when, when + 1) == 1), when


def test_a_line_that_does_not_go_in_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk that is full is this failure: the folder is there and the file opens, and the
    line does not go in. It is raised, and the file is left empty."""

    class Full(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    def opened_full(fd: int, *args: object, **options: object) -> Full:
        os.close(fd)  # the file is made, as it would be on a disk that is full
        return Full()

    monkeypatch.setattr(os, "fdopen", opened_full)

    with pytest.raises(OSError, match="No space left on device"):
        activity.write_event(tmp_path, click(), now=WRITTEN_AT)

    (path,) = (tmp_path / "watch").iterdir()
    assert path.read_text(encoding="utf-8") == ""


def test_the_reader_asked_for_one_record_reads_that_one_alone(tmp_path: Path) -> None:
    """While two loggers run, the record holds both and the mine reads both. Asked for one
    of them, the reader reads that one alone."""
    write(tmp_path, FILE, nav("one.example.org"))
    activity.write_event(tmp_path, click(), now=WRITTEN_AT)

    assert {event.site for event in activity.read_events(tmp_path)} == {
        "one.example.org",
        "example.org",
    }

    older = activity.read_events(tmp_path, patterns=("spike-*.jsonl",))
    own = activity.read_events(tmp_path, patterns=("activity-*.jsonl",))

    assert [event.site for event in older] == ["one.example.org"]
    assert [event.site for event in own] == ["example.org"]


def test_a_long_path_of_the_older_logger_is_cut_as_the_capture_cuts_one(tmp_path: Path) -> None:
    """A page chose the address then too, and the older logger keeps it in full. An event is
    cut the same way from either record."""
    write(tmp_path, FILE, tab_line("nav", "https://example.org/" + "a" * 3000))

    (event,) = activity.read_events(tmp_path)

    assert event.path == "/" + "a" * 2047


def test_an_event_after_a_line_cut_short_lands_on_its_own_line(tmp_path: Path) -> None:
    """A crash or a power loss can cut the last line of a day's file short, with no newline at
    its end. Before, the next event was glued to it, and both were lost."""
    path = activity.write_event(tmp_path, click(), now=WRITTEN_AT)
    whole = path.read_bytes()
    cut_short = whole[: len(whole) // 2]
    with path.open("ab") as stream:
        stream.write(cut_short)
    after = navigate("two.example.org", "2026-11-20T09:15:06")

    activity.write_event(tmp_path, after, now=WRITTEN_AT)

    line = (json.dumps(after.as_dict(), ensure_ascii=False) + "\n").encode()
    assert path.read_bytes() == whole + cut_short + b"\n" + line
    assert activity.read_events(tmp_path) == [click(), after]


def test_a_note_after_a_line_cut_short_lands_on_its_own_line(tmp_path: Path) -> None:
    path = tmp_path / "watch" / "agent-runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"kind": "run", "ended": 17')  # cut short, as a crash leaves it
    fields = {"kind": "run", "ended": 18}

    activity.note(tmp_path, "agent-runs.jsonl", fields)

    assert (
        path.read_bytes()
        == b'{"kind": "run", "ended": 17\n'
        + (json.dumps(fields, ensure_ascii=False) + "\n").encode()
    )
