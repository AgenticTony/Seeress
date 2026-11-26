"""A sitting as steps, and the sequences of steps that recur across days.

Every test works on invented events and reads no clock. The model is always a fake that
answers with one text and remembers what it was asked.
"""

from collections.abc import Sequence

from seeress import activity, miner, proposals, steps

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


# ------------------------------------------------------------- a sitting as steps ---


def kinds_of(events: Sequence[activity.Event]) -> list[tuple[str, str, int]]:
    """Of every step of the events' sitting: what was done, on which page, and how many events
    it stood for."""
    return [
        (unit.step.kind, unit.step.path, unit.step.repeats)
        for unit in steps.of(sitting_of(events))
        if unit.step is not None
    ]


def test_a_sitting_is_its_steps_in_order_and_a_navigation_is_no_step() -> None:
    events = [went("erp.example.com", f"{MONDAY}T08:59:00"), *job(MONDAY, rounds=1)]

    found = steps.of(sitting_of(events))

    assert len(found) == 4
    assert kinds_of(events) == [
        ("click", "/orders", 1),
        ("click", "/report", 1),
        ("input", "/report", 1),
        ("click", "/report", 1),
    ]
    assert found[0].site == "erp.example.com" and len(found[0].events) == 1


def test_the_same_step_twice_in_a_row_is_one_step_that_stood_for_two_events() -> None:
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), SAVE),
        did("click", clock.now(), SAVE),
        did("click", clock.now(), CELL),
    ]

    found = steps.of(sitting_of(events))

    assert [(unit.step.repeats if unit.step else 0, len(unit.events)) for unit in found] == [
        (2, 2),
        (1, 1),
    ]


def test_a_click_on_the_cell_of_one_row_and_a_click_on_the_cell_of_another_are_one_step() -> None:
    """The listener tells an element that no attribute names by its place, and the place keeps
    its digits. In a step every digit is #."""
    clock = Clock(MONDAY)

    def cell(row: int) -> activity.Event:
        place = f"table:nth-child(7)>tbody:nth-child(1)>tr:nth-child({row})>td:nth-child(2)"
        return did("click", clock.now(), ("td", place), path="/orders")

    (only,) = steps.of(sitting_of([cell(1), cell(5)]))

    assert only.step is not None
    assert (only.step.repeats, only.step.element) == (
        2,
        "table:nth-child(#)>tbody:nth-child(#)>tr:nth-child(#)>td:nth-child(#)",
    )


def test_two_pages_that_differ_by_an_identifier_are_one_step() -> None:
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), SAVE, path="/orders/4711/new"),
        did("click", clock.now(), SAVE, path="/orders/4712/new"),
    ]

    (only,) = steps.of(sitting_of(events))

    assert only.step is not None and (only.step.repeats, only.step.path) == (2, "/orders/{}/new")


def test_a_round_three_times_is_one_round_and_each_step_holds_its_three_events() -> None:
    found = steps.of(sitting_of(job(MONDAY, rounds=3)))

    assert [unit.step.repeats if unit.step else 0 for unit in found] == [3, 3, 3, 1]
    assert [len(unit.events) for unit in found] == [3, 3, 3, 1]
    assert [unit.step.other_tab if unit.step else None for unit in found] == [
        True,
        True,
        False,
        False,
    ]


def test_a_round_twice_is_left_as_it_came() -> None:
    found = steps.of(sitting_of(job(MONDAY, rounds=2)))

    assert len(found) == 7
    assert all(unit.step is not None and unit.step.repeats == 1 for unit in found)


def test_a_round_of_two_steps_three_times_is_one_round_of_two() -> None:
    clock = Clock(MONDAY)
    events = [
        event
        for _ in range(4)
        for event in (did("click", clock.now(), CELL), did("click", clock.now(), SAVE))
    ]

    found = steps.of(sitting_of(events))

    assert [unit.step.repeats if unit.step else 0 for unit in found] == [4, 4]


def test_twelve_rounds_are_one_round_of_twelve() -> None:
    found = steps.of(sitting_of(job(MONDAY, rounds=12)))

    assert [unit.step.repeats if unit.step else 0 for unit in found] == [12, 12, 12, 1]
