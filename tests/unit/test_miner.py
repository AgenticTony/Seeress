"""Finding what repeats in the activity record: sittings, sequences of sites that recur
across days, the holes in the record, and the proposals made from all three.

Every test works on invented events and reads no clock: whatever time a test needs is an
argument. The timestamps are wall-clock times of the machine the tests run on, turned into
seconds through the same reading the miner uses, so they mean the same in every time zone.
The model is always a fake that answers with one text.
"""

import json
from collections.abc import Sequence
from dataclasses import replace

import pytest

from seeress import activity, miner, proposals
from seeress.errors import BrainError

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


def test_a_candidate_of_a_sequence_is_fixed() -> None:
    events = [event for day in THREE_DAYS for event in sitting(day, "abc")]

    (found,) = miner.recurring(miner.sessions(events))

    assert found.order == "fixed"


# ------------------------------------------------ rituals whose order varies ---


def test_a_b_c_in_three_orders_on_three_days_is_one_cluster_and_no_sequence() -> None:
    """The plan's test. What recurred is the set of three sites, not their order."""
    sittings = miner.sessions(
        sitting(MONDAY, "abc") + sitting(WEDNESDAY, "cba") + sitting(FRIDAY, "bac")
    )

    assert miner.recurring(sittings) == []
    found = miner.clusters(sittings)

    assert [(cluster.sites, cluster.order, cluster.days) for cluster in found] == [
        (("a", "b", "c"), "varies", 3)
    ]


def test_a_set_seen_on_fewer_days_than_asked_is_not_found() -> None:
    sittings = miner.sessions(sitting(MONDAY, "abc") + sitting(TUESDAY, "abc"))

    assert miner.clusters(sittings) == []
    assert [cluster.sites for cluster in miner.clusters(sittings, days=2)] == [("a", "b", "c")]


def test_a_set_contained_in_a_found_set_is_not_returned() -> None:
    sittings = miner.sessions(
        sitting(MONDAY, "abc") + sitting(WEDNESDAY, "abc") + sitting(FRIDAY, "abcx")
    )

    found = miner.clusters(sittings)

    assert [cluster.sites for cluster in found] == [("a", "b", "c")]


def test_a_set_of_seven_sites_is_never_looked_for() -> None:
    """What is found of it is its seven sets of six."""
    events = [event for day in THREE_DAYS for event in sitting(day, "abcdefg")]

    found = miner.clusters(miner.sessions(events))

    assert len(found) == 7
    assert {len(cluster.sites) for cluster in found} == {6}
    assert sorted({site for cluster in found for site in cluster.sites}) == list("abcdefg")


def test_the_sites_of_a_cluster_come_by_how_many_sittings_hold_them() -> None:
    """b is in six sittings and a in three. Where two are held alike, alphabetically."""
    events = [
        event
        for day, hour, sites in (
            (MONDAY, 9, "bxa"),
            (WEDNESDAY, 9, "ab"),
            (FRIDAY, 9, "ba"),
            (MONDAY, 14, "b"),
            (WEDNESDAY, 14, "b"),
            (FRIDAY, 14, "b"),
        )
        for event in sitting(day, sites, hour)
    ]

    (found,) = miner.clusters(miner.sessions(events))

    assert found.sites == ("b", "a")


def test_a_cluster_is_counted_over_the_sittings_that_hold_the_whole_set() -> None:
    """A sitting that holds a alone, and one of another site, count for nothing."""
    events = [
        event
        for day, hour, sites in (
            (MONDAY, 9, "abc"),
            (WEDNESDAY, 11, "abc"),
            (FRIDAY, 9, "abc"),
            (MONDAY, 14, "a"),
            (TUESDAY, 9, "x"),
        )
        for event in sitting(day, sites, hour)
    ]

    (found,) = miner.clusters(miner.sessions(events))

    assert found.days == 3
    assert dict(found.sessions) == {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1}
    assert (found.first_seen, found.last_seen) == (at(MONDAY, "09:00:00"), at(FRIDAY, "09:02:00"))
    assert found.hour == 9


def test_the_hour_of_a_cluster_is_that_of_its_first_site_in_each_sitting() -> None:
    """In two sittings a site outside the set comes first, in the hour before the set."""
    events = [
        event
        for day in (MONDAY, FRIDAY)
        for event in (
            visit("x", at(day, "08:59:00")),
            visit("a", at(day, "09:00:00")),
            visit("b", at(day, "09:01:00")),
            visit("c", at(day, "09:02:00")),
        )
    ]
    events += sitting(WEDNESDAY, "abc", 11)

    (found,) = miner.clusters(miner.sessions(events))

    assert found.hour == 9


def test_the_paths_of_a_cluster_are_those_of_its_sites_in_those_sittings() -> None:
    events = [
        event
        for day, number in zip(THREE_DAYS, (7, 8, 9), strict=True)
        for event in (
            visit("a", at(day, "09:00:00")),
            visit("b", at(day, "09:01:00"), f"/b/{number}/x"),
            visit("b", at(day, "09:02:00"), "/b/list"),
            visit("c", at(day, "09:03:00")),
        )
    ]
    # A sitting that holds b alone: its path is not the cluster's. It does make b the site
    # that most sittings hold, so b comes first.
    events.append(visit("b", at(MONDAY, "14:00:00"), "/b/elsewhere"))

    (found,) = miner.clusters(miner.sessions(events))

    assert found.sites == ("b", "a", "c")
    assert found.paths == (("/b/list", "/b/{}/x"), ("/",), ("/",))


def test_clusters_come_most_days_first_then_most_sittings_then_alphabetically() -> None:
    """Four rituals, each in its own hours so that no sitting holds two of them. `jk` is on
    the most days, `hi` in the most sittings, and `fg` is found before `cde` but comes after
    it in the alphabet."""
    events = [
        *(event for day in THREE_DAYS for event in sitting(day, "cde", 9)),
        *(event for day in THREE_DAYS for event in sitting(day, "fg", 11)),
        *(event for day in THREE_DAYS for event in sitting(day, "hi", 13)),
        *(event for day in (MONDAY, WEDNESDAY) for event in sitting(day, "hi", 15)),
        *(event for day in (*THREE_DAYS, TUESDAY) for event in sitting(day, "jk", 17)),
    ]

    found = miner.clusters(miner.sessions(events))

    assert [(cluster.sites, cluster.days, sum(cluster.sessions.values())) for cluster in found] == [
        (("j", "k"), 4, 4),
        (("h", "i"), 3, 5),
        (("c", "d", "e"), 3, 3),
        (("f", "g"), 3, 3),
    ]


def test_no_sitting_is_no_cluster() -> None:
    assert miner.clusters([]) == []


def test_a_cluster_counts_days_and_not_sittings() -> None:
    events = sitting(MONDAY, "abc", 9) + sitting(MONDAY, "abc", 14) + sitting(WEDNESDAY, "abc")

    found = miner.clusters(miner.sessions(events), days=2)

    assert [(cluster.days, dict(cluster.sessions)) for cluster in found] == [
        (2, {MONDAY: 2, WEDNESDAY: 1})
    ]


def test_a_cluster_does_not_depend_on_the_order_the_sittings_come_in() -> None:
    friday = miner.sessions(sitting(FRIDAY, "abc", 15))
    monday = miner.sessions(sitting(MONDAY, "abc", 9))

    (found,) = miner.clusters([*friday, *monday], days=2)

    assert (found.first_seen, found.last_seen) == (at(MONDAY, "09:00:00"), at(FRIDAY, "15:02:00"))


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


def test_scheduled_time_is_counted_inside_the_schedule_only() -> None:
    start = activity.epoch(at(TUESDAY, "16:00:00"))
    end = activity.epoch(at(WEDNESDAY, "10:00:00"))

    assert miner.scheduled(start, end) == 4 * 3600


def test_no_time_outside_the_schedule_is_scheduled() -> None:
    start = activity.epoch(at(SATURDAY, "09:00:00"))
    end = activity.epoch(at(SATURDAY, "17:00:00"))

    assert miner.scheduled(start, end) == 0


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


# ----------------------------------------------------------------- proposals ---


def candidate(sites: Sequence[str], *, order: str = "fixed", path: str = "/") -> miner.Candidate:
    """A candidate with its evidence invented: three days, and one sitting on each."""
    return miner.Candidate(
        sites=tuple(sites),
        paths=tuple((path,) for _ in sites),
        days=3,
        sessions={MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1},
        first_seen=at(MONDAY, "09:00:00"),
        last_seen=at(FRIDAY, "09:02:00"),
        hour=9,
        order=order,
    )


class Model:
    """A model that answers with one text, and remembers what it was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def answer(*objects: dict[str, object]) -> str:
    """A model's answer: a list of the objects."""
    return json.dumps(list(objects))


def said(number: object, sentence: object, confidence: object) -> dict[str, object]:
    """One object of a model's answer. A test passes what a model should not write, too."""
    return {"n": number, "sentence": sentence, "confidence": confidence}


def test_the_prompt_shows_each_candidates_number_and_its_sites_in_order() -> None:
    candidates = [candidate(["b.example", "a.example"]), candidate(["c.example"])]

    prompt = miner.prompt_for(candidates)

    assert "1. sites: b.example → a.example" in prompt
    assert "2. sites: c.example" in prompt


def test_the_prompt_says_when_the_order_varies() -> None:
    candidates = [
        candidate(["a.example", "b.example"]),
        candidate(["c.example", "d.example"], order="varies"),
    ]

    prompt = miner.prompt_for(candidates)

    assert prompt.count("the order varies") == 1


def test_the_prompt_shows_the_hour_and_the_counts_of_a_candidate() -> None:
    mine = replace(candidate(["a.example"]), hour=10, sessions={MONDAY: 2, WEDNESDAY: 1, FRIDAY: 4})

    prompt = miner.prompt_for([mine])

    assert "hour 10" in prompt
    assert "3 days, in 7 sessions" in prompt


def test_the_prompt_shows_no_path() -> None:
    prompt = miner.prompt_for([candidate(["a.example"], path="/invoices/7f3a9c1e/list")])

    assert "invoices" not in prompt
    assert "7f3a9c1e" not in prompt


def test_the_prompt_asks_for_json_of_one_shape_and_nothing_else() -> None:
    prompt = miner.prompt_for([candidate(["a.example", "b.example"])])

    assert '"n"' in prompt and '"sentence"' in prompt and '"confidence"' in prompt
    assert "JSON and nothing else" in prompt
    assert "1 means coincidence" in prompt
    assert "5 means" in prompt


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            answer(said(1, "Checks mail.", 4), said(2, "Pays invoices.", 2)),
            [("Checks mail.", 4), ("Pays invoices.", 2)],
            id="good JSON",
        ),
        pytest.param(
            "Here it is:\n" + answer(said(1, "Checks mail.", 4)) + "\nDone.",
            [("Checks mail.", 4)],
            id="JSON inside prose",
        ),
        pytest.param(
            "[Thought] these look like mail. " + answer(said(1, "Checks mail.", 4)),
            [("Checks mail.", 4)],
            id="a bracket in the prose before the JSON",
        ),
        pytest.param("I see no pattern here.", [("", None)], id="no JSON at all"),
        pytest.param("Sure: [not json]", [("", None)], id="brackets that hold no JSON"),
        pytest.param(
            '{"n": 1, "sentence": "a", "confidence": 3}', [("", None)], id="JSON that is no list"
        ),
        pytest.param(
            answer(said(1, "a", 3), said(7, "b", 5)),
            [("a", 3)],
            id="a number that is not one of them",
        ),
        pytest.param(answer(said(True, "a", 3)), [("", None)], id="a number that is true"),
        pytest.param(answer(said(1.0, "a", 3)), [("a", 3)], id="a number written 1.0"),
        pytest.param(answer(said(2, "b", 4)), [("", None), ("b", 4)], id="a number left out"),
        pytest.param(
            answer(said(1, ["not", "text"], 3)), [("", 3)], id="a sentence that is not text"
        ),
        pytest.param(
            answer(said(1, "a", 3), said(1, "b", 5)),
            [("a", 3)],
            id="a number twice: the first stands",
        ),
        pytest.param(answer(said(1, "a", 4.0)), [("a", 4)], id="a confidence written 4.0"),
        pytest.param(
            answer(said(1, "  Checks   mail,\nthen  invoices.  ", 3)),
            [("Checks mail, then invoices.", 3)],
            id="whitespace collapsed, and the ends stripped",
        ),
        pytest.param(answer(said(1, "x" * 400, 3)), [("x" * 300, 3)], id="a sentence cut at 300"),
    ],
)
def test_the_models_answer_is_read_for_each_candidate(
    reply: str, expected: list[tuple[str, int | None]]
) -> None:
    assert miner.sentences(reply, len(expected)) == expected


@pytest.mark.parametrize(
    "confidence",
    [0, 6, -1, 4.5, "4", True, None, [4]],
    ids=["0", "6", "below 1", "a fraction", "text", "true", "null", "a list"],
)
def test_a_confidence_that_is_no_integer_from_1_to_5_is_not_kept(confidence: object) -> None:
    assert miner.sentences(answer(said(1, "Checks mail.", confidence)), 1) == [
        ("Checks mail.", None)
    ]


def test_a_sentence_that_names_a_site_outside_the_evidence_is_dropped_with_its_confidence() -> None:
    """The plan's test. A model may not bring a site of its own into a proposal."""
    mine = candidate(["mail.example.net", "invoices.example.org"])
    model = Model(answer(said(1, "Checks mail, then files the invoices in dms.example.com.", 5)))

    (found,) = miner.propose([mine], model)

    assert (found.goal, found.confidence) == ("", None)


def test_a_sentence_that_names_only_its_own_sites_is_kept() -> None:
    mine = candidate(["mail.example.net", "intranet_server"])
    sentence = "Checks Mail.Example.net, then opens intranet_server."
    model = Model(answer(said(1, sentence, 4)))

    (found,) = miner.propose([mine], model)

    assert (found.goal, found.confidence) == (sentence, 4)


@pytest.mark.parametrize(
    "sentence",
    ["Look at www.example.net first.", "Open http://example.net.", "Write to a@b.example."],
    ids=["www.", "http", "an at sign"],
)
def test_a_sentence_with_www_http_or_an_at_sign_is_dropped(sentence: str) -> None:
    (found,) = miner.propose([candidate(["a.example"])], Model(answer(said(1, sentence, 4))))

    assert (found.goal, found.confidence) == ("", None)


def test_a_sentence_that_names_a_foreign_site_in_capitals_is_dropped() -> None:
    mine = candidate(["mail.example.net"])
    model = Model(answer(said(1, "Checks Mail.Example.net, then DMS.Example.com.", 4)))

    (found,) = miner.propose([mine], model)

    assert (found.goal, found.confidence) == ("", None)


def test_a_sentence_with_an_at_sign_keeps_no_site_of_its_own_either() -> None:
    """The at sign alone drops a sentence, whatever sites it names."""
    mine = candidate(["a.example"])
    model = Model(answer(said(1, "Writes to someone@a.example about a.example.", 4)))

    (found,) = miner.propose([mine], model)

    assert (found.goal, found.confidence) == ("", None)


def test_a_dropped_sentence_leaves_the_other_candidates_alone() -> None:
    candidates = [candidate(["a.example", "b.example"]), candidate(["c.example", "d.example"])]
    model = Model(answer(said(1, "ab", 3), said(2, "cd on nowhere.example.org", 5)))

    found = miner.propose(candidates, model)

    assert [(proposal.goal, proposal.confidence) for proposal in found] == [("ab", 3), ("", None)]


def test_with_no_model_a_proposal_carries_the_evidence_and_no_sentence() -> None:
    mine = candidate(["a.example", "b.example"], order="varies", path="/a/{}/x")

    (found,) = miner.propose([mine], None)

    assert (found.goal, found.confidence, found.status) == ("", None, "proposed")
    assert (found.sites, found.order, found.days, found.hour) == (
        ("a.example", "b.example"),
        "varies",
        3,
        9,
    )
    assert dict(found.sessions) == {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1}
    assert (found.first_seen, found.last_seen) == (at(MONDAY, "09:00:00"), at(FRIDAY, "09:02:00"))
    assert found.name == proposals.name_for(mine.sites)
    assert proposals.Proposal.parse(found.as_dict()) == found


def test_the_model_is_asked_once_about_every_candidate_in_order() -> None:
    candidates = [candidate(["a.example", "b.example"]), candidate(["c.example"])]
    model = Model(answer(said(1, "ab", 3), said(2, "c", 5)))

    found = miner.propose(candidates, model)

    assert model.prompts == [miner.prompt_for(candidates)]
    assert [(proposal.sites, proposal.goal, proposal.confidence) for proposal in found] == [
        (("a.example", "b.example"), "ab", 3),
        (("c.example",), "c", 5),
    ]


def test_no_candidate_is_no_proposal_and_the_model_is_not_asked() -> None:
    model = Model("[]")

    assert miner.propose([], model) == []
    assert model.prompts == []


def test_what_the_model_raises_is_not_caught() -> None:
    def no_model(prompt: str) -> str:
        raise BrainError("no model answered")

    with pytest.raises(BrainError, match="no model answered"):
        miner.propose([candidate(["a.example", "b.example"])], no_model)
