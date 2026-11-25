"""Finding what repeats in the activity record: sessions, sequences of sites that recur
across days, and the holes in the record.

Every test works on invented events and reads no clock: whatever time a test needs is an
argument. The timestamps are wall-clock times of the machine the tests run on, turned into
seconds through the same reading the miner uses, so they mean the same in every time zone.
"""

from collections.abc import Sequence

import pytest

from seeress import activity, miner

# One week, Monday 16 November 2026 to Saturday the 21st.
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = (
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
    "2026-11-20",
)
SATURDAY = "2026-11-21"
THREE_DAYS = (MONDAY, WEDNESDAY, FRIDAY)


def at(day: str, clock: str) -> str:
    """A timestamp of the logger's form: a day and a wall-clock time."""
    return f"{day}T{clock}"


def visit(site: str, ts: str, path: str = "/") -> activity.Event:
    """A navigation to a page of a site."""
    return activity.Event(1, ts, "navigate", site, path, "1a2b3c4d")


def sitting(day: str, sites: str, hour: int = 9) -> list[activity.Event]:
    """One sitting on one day: a visit to the front page of each site in turn, a minute
    apart, from the hour."""
    return [
        visit(site, at(day, f"{hour:02d}:{minute:02d}:00")) for minute, site in enumerate(sites)
    ]


def span(start: str, end: str) -> activity.Interval:
    """The time from one wall-clock time to another."""
    return activity.Interval(activity.epoch(start), activity.epoch(end))


def sites_of(events: Sequence[activity.Event]) -> list[str]:
    return [event.site for event in events]


# ----------------------------------------------------------------- sessions ---


def test_a_stream_with_a_forty_minute_hole_is_two_sessions() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "10:40:00"))]

    found = miner.sessions(events)

    assert [(session.start, session.end) for session in found] == [
        (at(TUESDAY, "10:00:00"), at(TUESDAY, "10:00:00")),
        (at(TUESDAY, "10:40:00"), at(TUESDAY, "10:40:00")),
    ]


@pytest.mark.parametrize(
    ("minutes", "count"),
    [(29, 1), (30, 1), (31, 2)],
    ids=["29 minutes", "30 minutes", "31 minutes"],
)
def test_a_session_is_cut_where_more_than_the_gap_passes(minutes: int, count: int) -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("b", at(TUESDAY, f"10:{minutes:02d}:00"))]

    assert len(miner.sessions(events)) == count


def test_a_stream_with_an_outage_in_it_is_cut_there() -> None:
    """Twenty minutes is under the gap, and an outage of one minute still cuts the stream.
    The logger was not recording, and what came after is not the same sitting."""
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("b", at(TUESDAY, "10:20:00"))]
    outages = [span(at(TUESDAY, "10:10:00"), at(TUESDAY, "10:11:00"))]

    assert len(miner.sessions(events)) == 1
    assert [sites_of(session.events) for session in miner.sessions(events, outages=outages)] == [
        ["a"],
        ["b"],
    ]


def test_an_outage_between_two_events_of_the_same_second_cuts() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("b", at(TUESDAY, "10:00:00"))]
    outages = [span(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:00:00"))]

    assert len(miner.sessions(events, outages=outages)) == 2


def test_an_outage_that_starts_between_no_two_events_cuts_nothing() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("b", at(TUESDAY, "10:10:00"))]
    outages = [
        span(at(TUESDAY, "09:00:00"), at(TUESDAY, "09:30:00")),
        span(at(TUESDAY, "10:30:00"), at(TUESDAY, "11:00:00")),
    ]

    assert len(miner.sessions(events, outages=outages)) == 1


def test_the_first_event_decides_the_day_and_the_hour() -> None:
    events = [visit("a", at(TUESDAY, "09:59:00")), visit("b", at(TUESDAY, "10:20:00"))]

    (found,) = miner.sessions(events)

    assert found.events == tuple(events)
    assert (found.day, found.hour) == (TUESDAY, 9)
    assert (found.start, found.end) == (at(TUESDAY, "09:59:00"), at(TUESDAY, "10:20:00"))


def test_a_sitting_across_midnight_belongs_to_the_day_it_began_on() -> None:
    events = [visit("a", at(MONDAY, "23:59:00")), visit("b", at(TUESDAY, "00:20:00"))]

    (found,) = miner.sessions(events)

    assert (found.day, found.hour) == (MONDAY, 23)
    assert (found.start, found.end) == (at(MONDAY, "23:59:00"), at(TUESDAY, "00:20:00"))


def test_events_out_of_order_are_put_in_order() -> None:
    events = [
        visit("c", at(TUESDAY, "10:02:00")),
        visit("a", at(TUESDAY, "10:00:00")),
        visit("b", at(TUESDAY, "10:01:00")),
    ]

    (found,) = miner.sessions(events)

    assert sites_of(found.events) == ["a", "b", "c"]


def test_events_that_share_a_second_keep_the_order_they_arrived_in() -> None:
    events = [visit("b", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "10:00:00"))]

    (found,) = miner.sessions(events)

    assert sites_of(found.events) == ["b", "a"]


def test_sessions_come_oldest_first() -> None:
    events = [visit("a", at(WEDNESDAY, "09:00:00")), visit("b", at(MONDAY, "09:00:00"))]

    found = miner.sessions(events)

    assert [session.day for session in found] == [MONDAY, WEDNESDAY]


def test_no_event_is_no_session() -> None:
    assert miner.sessions([]) == []


def test_an_event_whose_ts_is_no_time_is_left_out() -> None:
    """The schema judges the shape of a ts, not whether it is one. Such an event cannot be
    placed in a session."""
    events = [
        visit("a", at(TUESDAY, "10:00:00")),
        visit("x", "2026-13-45T25:61:61"),
        visit("b", at(TUESDAY, "10:01:00")),
    ]

    (found,) = miner.sessions(events)

    assert sites_of(found.events) == ["a", "b"]


# ------------------------------------------------------- the minutes of an agent ---


def test_an_agents_minutes_are_in_no_session() -> None:
    """What the agent did in the browser is not what the person did."""
    events = [
        visit("a", at(TUESDAY, "10:00:00")),
        visit("b", at(TUESDAY, "10:10:00")),
        visit("c", at(TUESDAY, "10:20:00")),
    ]
    runs = [span(at(TUESDAY, "10:05:00"), at(TUESDAY, "10:15:00"))]

    mine = miner.without_runs(events, runs)
    (session,) = miner.sessions(mine)

    assert sites_of(session.events) == ["a", "c"]


def test_an_event_on_the_first_or_the_last_second_of_a_run_is_inside_it() -> None:
    events = [
        visit("a", at(TUESDAY, "10:00:00")),
        visit("b", at(TUESDAY, "10:05:00")),
        visit("c", at(TUESDAY, "10:15:00")),
        visit("d", at(TUESDAY, "10:20:00")),
    ]
    runs = [span(at(TUESDAY, "10:05:00"), at(TUESDAY, "10:15:00"))]

    assert sites_of(miner.without_runs(events, runs)) == ["a", "d"]


def test_no_run_leaves_every_event() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("b", at(TUESDAY, "10:01:00"))]

    assert miner.without_runs(events, []) == events


# ------------------------------------------------------ recurrence across days ---


def test_a_b_c_three_times_in_one_hour_is_no_candidate() -> None:
    """A pattern repeated inside one session is not a workflow. Recurrence is counted
    across days."""
    events = sitting(TUESDAY, "abcxabcyabc")

    assert miner.recurring(miner.sessions(events)) == []


def test_a_site_seen_on_its_own_on_many_days_is_no_candidate() -> None:
    """A sequence is two sites at the least. One site on its own says nothing about a task."""
    events = [event for day in THREE_DAYS for event in sitting(day, "a")]

    assert miner.recurring(miner.sessions(events)) == []


def test_a_b_c_on_monday_wednesday_and_friday_is_a_candidate_with_three_days() -> None:
    events = [event for day in THREE_DAYS for event in sitting(day, "abc")]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.sites == ("a", "b", "c")
    assert found.days == 3
    assert dict(found.sessions) == {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1}


def test_a_repeat_inside_one_session_is_not_a_second_day() -> None:
    events = sitting(MONDAY, "abcxabc") + sitting(TUESDAY, "abcxabc")

    found = miner.recurring(miner.sessions(events), days=2)

    assert miner.recurring(miner.sessions(events), days=3) == []
    assert {candidate.days for candidate in found} == {2}


def test_consecutive_repeats_are_collapsed() -> None:
    """`A A B` is three events and two visits. The block that recurs is `A B`. Read without
    the collapse, it would be a candidate of three sites, `A A B`."""
    events = [event for day in THREE_DAYS for event in sitting(day, "aab")]

    found = miner.recurring(miner.sessions(events))

    assert [candidate.sites for candidate in found] == [("a", "b")]


def test_a_block_of_seven_sites_is_never_a_candidate() -> None:
    events = [event for day in THREE_DAYS for event in sitting(day, "abcdefg")]

    found = miner.recurring(miner.sessions(events))

    assert [candidate.sites for candidate in found] == [
        ("a", "b", "c", "d", "e", "f"),
        ("b", "c", "d", "e", "f", "g"),
    ]


def test_of_a_five_site_sequence_that_recurs_only_it_is_returned() -> None:
    events = [event for day in THREE_DAYS for event in sitting(day, "vwxyz")]

    found = miner.recurring(miner.sessions(events))

    assert [candidate.sites for candidate in found] == [("v", "w", "x", "y", "z")]


def test_a_part_seen_on_more_days_than_a_longer_sequence_is_kept() -> None:
    events = [
        event
        for day, sites in (
            (MONDAY, "abcd"),
            (TUESDAY, "abc"),
            (WEDNESDAY, "abcd"),
            (THURSDAY, "abc"),
            (FRIDAY, "abcd"),
        )
        for event in sitting(day, sites)
    ]

    found = miner.recurring(miner.sessions(events))

    assert [(candidate.sites, candidate.days) for candidate in found] == [
        (("a", "b", "c"), 5),
        (("a", "b", "c", "d"), 3),
    ]


def test_candidates_come_most_days_first_then_most_sessions_then_by_their_sites() -> None:
    """Four patterns, each in its own hours so that no session holds two of them. `tu` is
    found before `rs`, so that the order of the two is the alphabet's and not the order they
    were found in."""
    events = [
        *(
            event
            for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY)
            for event in sitting(day, "pq", 9)
        ),
        *(
            event
            for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY)
            for event in sitting(day, "tu", 11)
        ),
        *(event for day in (MONDAY, TUESDAY) for event in sitting(day, "tu", 14)),
        *(
            event
            for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY)
            for event in sitting(day, "rs", 16)
        ),
        *(event for day in (MONDAY, TUESDAY) for event in sitting(day, "rs", 20)),
        *(
            event
            for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY)
            for event in sitting(day, "vw", 6)
        ),
    ]

    found = miner.recurring(miner.sessions(events))

    assert [
        (candidate.sites, candidate.days, sum(candidate.sessions.values())) for candidate in found
    ] == [
        (("v", "w"), 5, 5),
        (("r", "s"), 4, 6),
        (("t", "u"), 4, 6),
        (("p", "q"), 4, 4),
    ]


@pytest.mark.parametrize(
    ("hours", "expected"),
    [((9, 9, 11), 9), ((9, 11, 11), 11), ((9, 11), 9)],
    ids=["nine twice", "eleven twice", "a tie"],
)
def test_hour_is_the_most_common_start_hour_and_the_earlier_one_where_two_tie(
    hours: tuple[int, ...], expected: int
) -> None:
    events = [
        event
        for day, hour in zip(THREE_DAYS, hours, strict=False)
        for event in sitting(day, "abc", hour)
    ]

    # A candidate must recur on at least as many days as there are sittings here.
    (found,) = miner.recurring(miner.sessions(events), days=len(hours))

    assert found.hour == expected


def test_a_day_with_two_sittings_is_one_day_and_two_sessions() -> None:
    events = [
        event
        for day, hour in ((MONDAY, 9), (MONDAY, 14), (WEDNESDAY, 9), (FRIDAY, 9))
        for event in sitting(day, "abc", hour)
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.days == 3
    assert dict(found.sessions) == {MONDAY: 2, WEDNESDAY: 1, FRIDAY: 1}
    assert list(found.sessions) == [MONDAY, WEDNESDAY, FRIDAY]


def test_first_seen_and_last_seen_are_the_earliest_start_and_the_latest_end() -> None:
    events = [
        event
        for day, hour in ((MONDAY, 9), (WEDNESDAY, 11), (FRIDAY, 9), (FRIDAY, 15))
        for event in sitting(day, "abc", hour)
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.first_seen == at(MONDAY, "09:00:00")
    assert found.last_seen == at(FRIDAY, "15:02:00")


def test_first_seen_and_last_seen_do_not_depend_on_the_order_the_sittings_come_in() -> None:
    friday = miner.sessions(sitting(FRIDAY, "abc", 15))
    monday = miner.sessions(sitting(MONDAY, "abc", 9))

    (found,) = miner.recurring([*friday, *monday], days=2)

    assert (found.first_seen, found.last_seen) == (at(MONDAY, "09:00:00"), at(FRIDAY, "15:02:00"))


def test_a_block_repeated_inside_one_sitting_counts_that_sitting_once() -> None:
    events = sitting(MONDAY, "abcxabc") + [
        event for day in (WEDNESDAY, FRIDAY) for event in sitting(day, "abc")
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.sessions == {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1}


def test_an_occurrence_counts_for_the_hour_each_time_it_happens() -> None:
    """Monday holds the pattern twice, at nine. Wednesday and Friday hold it once each, at
    fifteen. Two against two is a tie, and the earlier hour wins."""
    events = sitting(MONDAY, "abcabc", 9) + [
        event for day in (WEDNESDAY, FRIDAY) for event in sitting(day, "abc", 15)
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.hour == 9


def test_the_hour_is_the_one_an_occurrence_starts_in() -> None:
    """Every occurrence starts at 09:58 and ends at 10:00."""
    events = [
        event
        for day in THREE_DAYS
        for event in (
            visit("x", at(day, "09:57:00")),
            visit("a", at(day, "09:58:00")),
            visit("b", at(day, "09:59:00")),
            visit("c", at(day, "10:00:00")),
        )
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.hour == 9


def test_one_step_seen_at_three_identifiers_is_shown_as_one_path() -> None:
    """/b/7/x, /b/8/x and /b/9/x are one place, seen three times."""
    events = [
        event
        for day, number in zip(THREE_DAYS, (7, 8, 9), strict=True)
        for event in (
            visit("a", at(day, "09:00:00")),
            visit("b", at(day, "09:01:00"), f"/b/{number}/x"),
            visit("c", at(day, "09:02:00")),
        )
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.paths == (("/",), ("/b/{}/x",), ("/",))


def test_a_step_keeps_its_three_most_common_paths() -> None:
    """Most common first. Where counts are equal, alphabetically. The fourth is left out."""
    events = [
        event
        for day, number in zip(THREE_DAYS, (7, 8, 9), strict=True)
        for event in (
            visit("a", at(day, "09:00:00")),
            visit("b", at(day, "09:01:00"), f"/b/{number}/x"),
            visit("b", at(day, "09:02:00"), "/b/list"),
            visit("b", at(day, "09:03:00"), "/b/list"),
            visit("b", at(day, "09:04:00"), "/b/alpha"),
            visit("b", at(day, "09:05:00"), "/b/other"),
            visit("b", at(day, "09:06:00"), "/b/zzz"),
            visit("c", at(day, "09:07:00")),
        )
    ]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.paths == (("/",), ("/b/list", "/b/alpha", "/b/other"), ("/",))


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        pytest.param("/i/7", "/i/{}", id="a number"),
        pytest.param("/i/2026", "/i/{}", id="a year, which is a number"),
        pytest.param("/i/3f2a9c1e-7b4d-4e8a-9c2b-5d1a6f0e2b7c", "/i/{}", id="a UUID"),
        pytest.param("/i/deadbeef01", "/i/{}", id="eight hexadecimal digits"),
        pytest.param("/i/DEADBEEF01", "/i/{}", id="the same, in upper case"),
        pytest.param("/i/7/top", "/i/{}/top", id="one segment of three"),
        pytest.param("/invoices/open", "/invoices/open", id="words"),
        pytest.param("/i/short1", "/i/short1", id="a word with a digit"),
        pytest.param("/i/ab-12", "/i/ab-12", id="a mixture"),
        pytest.param("/i/deadbee", "/i/deadbee", id="seven hexadecimal digits"),
        pytest.param("/", "/", id="the root"),
    ],
)
def test_a_path_keeps_its_words_and_hides_its_identifiers(path: str, shown: str) -> None:
    events = [visit("a", at(day, "09:00:00")) for day in THREE_DAYS]
    events += [visit("i", at(day, "09:01:00"), path) for day in THREE_DAYS]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.paths == (("/",), (shown,))


def test_no_session_is_no_candidate() -> None:
    assert miner.recurring([]) == []


# ------------------------------------------------------------------- the gaps ---


def test_thirty_seven_minutes_of_chrome_down_is_one_gap_of_37_minutes() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "10:37:00"))]
    outages = [span(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:37:00"))]

    found = miner.gaps(events, outages, now=activity.epoch(at(TUESDAY, "11:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:37:00"), 37, "down")]


def test_an_outage_outside_the_schedule_is_still_returned() -> None:
    events = [visit("a", at(SATURDAY, "10:00:00")), visit("a", at(SATURDAY, "10:37:00"))]
    outages = [span(at(SATURDAY, "10:00:00"), at(SATURDAY, "10:37:00"))]

    found = miner.gaps(events, outages, now=activity.epoch(at(SATURDAY, "11:00:00")))

    assert found == [miner.Gap(at(SATURDAY, "10:00:00"), at(SATURDAY, "10:37:00"), 37, "down")]


def test_a_hole_of_forty_minutes_on_a_tuesday_morning_is_one_silent_gap() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "10:40:00"))]

    found = miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "11:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:40:00"), 40, "silent")]


def test_the_same_hole_on_a_saturday_is_none() -> None:
    events = [visit("a", at(SATURDAY, "10:00:00")), visit("a", at(SATURDAY, "10:40:00"))]

    assert miner.gaps(events, [], now=activity.epoch(at(SATURDAY, "11:00:00"))) == []


def test_a_hole_from_17_50_to_19_00_on_a_weekday_is_a_gap_of_ten_minutes() -> None:
    events = [visit("a", at(TUESDAY, "17:50:00")), visit("a", at(TUESDAY, "19:00:00"))]

    found = miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "20:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "17:50:00"), at(TUESDAY, "18:00:00"), 10, "silent")]


def test_a_hole_with_an_outage_inside_it_is_silent_only_around_the_outage() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "12:00:00"))]
    outages = [span(at(TUESDAY, "10:30:00"), at(TUESDAY, "11:00:00"))]

    found = miner.gaps(events, outages, now=activity.epoch(at(TUESDAY, "12:00:00")))

    assert found == [
        miner.Gap(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:30:00"), 30, "silent"),
        miner.Gap(at(TUESDAY, "10:30:00"), at(TUESDAY, "11:00:00"), 30, "down"),
        miner.Gap(at(TUESDAY, "11:00:00"), at(TUESDAY, "12:00:00"), 60, "silent"),
    ]


def test_a_hole_after_the_last_event_counts_up_to_now() -> None:
    events = [visit("a", at(TUESDAY, "15:00:00"))]

    found = miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "16:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "15:00:00"), at(TUESDAY, "16:00:00"), 60, "silent")]


def test_no_hole_is_counted_after_the_last_event_when_now_is_near() -> None:
    events = [visit("a", at(TUESDAY, "15:00:00"))]

    assert miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "15:20:00"))) == []


def test_a_hole_no_longer_than_the_session_gap_is_none() -> None:
    events = [visit("a", at(TUESDAY, "10:00:00")), visit("a", at(TUESDAY, "10:30:00"))]

    assert miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "11:00:00"))) == []


def test_a_hole_across_the_evening_is_one_gap_per_day() -> None:
    events = [visit("a", at(TUESDAY, "17:00:00")), visit("a", at(WEDNESDAY, "09:00:00"))]

    found = miner.gaps(events, [], now=activity.epoch(at(WEDNESDAY, "09:00:00")))

    assert found == [
        miner.Gap(at(TUESDAY, "17:00:00"), at(TUESDAY, "18:00:00"), 60, "silent"),
        miner.Gap(at(WEDNESDAY, "08:00:00"), at(WEDNESDAY, "09:00:00"), 60, "silent"),
    ]


def test_a_gap_of_less_than_a_minute_is_still_returned() -> None:
    events = [visit("a", at(TUESDAY, "17:59:30")), visit("a", at(TUESDAY, "18:40:00"))]

    found = miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "19:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "17:59:30"), at(TUESDAY, "18:00:00"), 0, "silent")]


def test_a_schedule_of_the_users_own_is_used() -> None:
    events = [visit("a", at(TUESDAY, "08:00:00")), visit("a", at(TUESDAY, "12:00:00"))]
    schedule = miner.Schedule(frozenset({1}), 9, 11)

    found = miner.gaps(events, [], now=activity.epoch(at(TUESDAY, "12:00:00")), schedule=schedule)

    assert found == [miner.Gap(at(TUESDAY, "09:00:00"), at(TUESDAY, "12:00:00"), 180, "silent")]


def test_an_outage_is_reported_when_no_event_was_ever_written() -> None:
    outages = [span(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:37:00"))]

    found = miner.gaps([], outages, now=activity.epoch(at(TUESDAY, "11:00:00")))

    assert found == [miner.Gap(at(TUESDAY, "10:00:00"), at(TUESDAY, "10:37:00"), 37, "down")]


def test_no_event_and_no_outage_is_no_gap() -> None:
    assert miner.gaps([], [], now=0.0) == []


def test_gaps_come_oldest_first() -> None:
    """The hole from Monday at eleven to Tuesday at ten is one gap for the rest of Monday
    and one for the start of Tuesday."""
    events = [
        visit("a", at(MONDAY, "10:00:00")),
        visit("a", at(MONDAY, "11:00:00")),
        visit("a", at(TUESDAY, "10:00:00")),
        visit("a", at(TUESDAY, "11:00:00")),
    ]
    outages = [span(at(TUESDAY, "10:20:00"), at(TUESDAY, "10:40:00"))]

    found = miner.gaps(events, outages, now=activity.epoch(at(TUESDAY, "11:00:00")))

    assert [(gap.start, gap.why) for gap in found] == [
        (at(MONDAY, "10:00:00"), "silent"),
        (at(MONDAY, "11:00:00"), "silent"),
        (at(TUESDAY, "08:00:00"), "silent"),
        (at(TUESDAY, "10:00:00"), "silent"),
        (at(TUESDAY, "10:20:00"), "down"),
        (at(TUESDAY, "10:40:00"), "silent"),
    ]


def test_the_default_schedule_is_monday_to_friday_from_eight_to_six() -> None:
    schedule = miner.DEFAULT_SCHEDULE

    assert (schedule.days, schedule.start, schedule.end) == (frozenset({0, 1, 2, 3, 4}), 8, 17)
