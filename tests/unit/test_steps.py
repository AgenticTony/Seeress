"""A sitting as steps, and the sequences of steps that recur across days.

Every test works on invented events and reads no clock. The model is always a fake that
answers with one text and remembers what it was asked.
"""

from collections.abc import Sequence

from seeress import activity, miner, proposals

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = (
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
    "2026-11-20",
)

# Two tabs of one site.
SOURCE, TARGET = "source000", "target000"

# Elements as the listener names them: the tag, and how the element is told apart.
CELL = ("td", 'td[name="qty-#"]')
FIELD = ("input", 'input[name="line-#"]')
SAVE = ("button", 'button[data-testid="save"]')


def did(
    kind: str,
    ts: str,
    element: tuple[str, str],
    *,
    site: str = "erp.example.com",
    path: str = "/",
    field: str | None = None,
    tab: str = SOURCE,
) -> activity.Event:
    """A click, an input, a submit or a select in a page: what the listener reports of it."""
    return activity.Event(
        1, ts, kind, site, path, tab, activity.Action(kind, element[0], element[1], field)
    )


def went(site: str, ts: str) -> activity.Event:
    """A navigation, as the older logger writes it: no action, and so no step."""
    return activity.Event(1, ts, "navigate", site, "/", SOURCE)


class Clock:
    """A time of one day that goes on a second at a time."""

    def __init__(self, day: str, hour: int = 9) -> None:
        self.day = day
        self.second = hour * 3600

    def now(self) -> str:
        self.second += 1
        minute, second = divmod(self.second, 60)
        hour, minute = divmod(minute, 60)
        return f"{self.day}T{hour:02d}:{minute:02d}:{second:02d}"


def job(day: str, rounds: int = 3, hour: int = 9) -> list[activity.Event]:
    """A copy-and-paste job between two tabs of one site: rounds of a click on a cell, a click
    on a field and an input into it, then a save. A second apart."""
    clock = Clock(day, hour)
    events: list[activity.Event] = []
    for _ in range(rounds):
        events += [
            did("click", clock.now(), CELL, path="/orders"),
            did("click", clock.now(), FIELD, path="/report", tab=TARGET),
            did("input", clock.now(), FIELD, path="/report", field="text", tab=TARGET),
        ]
    events.append(did("click", clock.now(), SAVE, path="/report", tab=TARGET))
    return events


def sitting_of(events: Sequence[activity.Event]) -> miner.Session:
    """The events as one sitting."""
    (only,) = miner.sessions(events)
    return only


# ------------------------------------------------------ the search on any units ---


def test_a_unit_is_told_apart_by_its_key_and_holds_the_events_of_its_visit() -> None:
    clock = Clock(MONDAY)
    events = [
        went("a.example", clock.now()),
        did("click", clock.now(), SAVE, site="a.example"),
        went("b.example", clock.now()),
    ]

    (first, second) = miner.visits(sitting_of(events))

    assert (first.key, first.site, len(first.events), first.step) == (
        "a.example",
        "a.example",
        2,
        None,
    )
    assert (second.key, second.site, len(second.events)) == ("b.example", "b.example", 1)


def test_the_search_runs_on_any_sequence_of_units_and_any_lengths() -> None:
    """Three days on which the same two visits come in the middle of other work. Searched on
    visits, with blocks of two, the pair is found."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, WEDNESDAY, FRIDAY)):
        # Other work before and after, on another site every day.
        events += [
            went(f"x{at}.example", f"{day}T09:00:00"),
            went("a.example", f"{day}T09:01:00"),
            went("b.example", f"{day}T09:02:00"),
            went(f"y{at}.example", f"{day}T09:03:00"),
        ]

    (found,) = miner.recurring(miner.sessions(events), sizes=(2, 2))

    assert found.sites == ("a.example", "b.example")
    assert found.keys == ("a.example", "b.example")
    assert found.steps is None


def test_a_sequence_is_looked_for_only_where_the_one_before_it_recurs() -> None:
    """Five sites recur on three days, with a sixth on two. The five are found and the sixth
    is not, as before the search was generalised."""
    days = (MONDAY, WEDNESDAY, FRIDAY)
    sites = ("a", "b", "c", "d", "e")
    events = [
        went(f"{site}.example", f"{day}T09:{at:02d}:00")
        for day in days
        for at, site in enumerate(sites)
    ]
    events += [went("f.example", f"{day}T09:06:00") for day in (MONDAY, WEDNESDAY)]

    found = miner.recurring(miner.sessions(events), sizes=(2, 6))

    assert [candidate.sites for candidate in found] == [
        ("a.example", "b.example", "c.example", "d.example", "e.example")
    ]


def test_a_name_is_told_apart_by_keys_when_keys_are_given() -> None:
    one = proposals.name_for(["erp.example.com"], keys=["click", "input"])
    other = proposals.name_for(["erp.example.com"], keys=["click", "click"])

    assert one != other
    assert one == proposals.name_for(["erp.example.com"], keys=["click", "input"])
    assert one.startswith("erp-example-com-") and proposals.NAME_SHAPE.fullmatch(one)


def test_sites_once_in_a_row_are_left_out_and_a_return_is_kept() -> None:
    assert miner.once(("a", "a", "b", "b", "a")) == ("a", "b", "a")


def test_the_path_of_an_identifier_is_hidden() -> None:
    assert miner.masked("/orders/4711/new") == "/orders/{}/new"
