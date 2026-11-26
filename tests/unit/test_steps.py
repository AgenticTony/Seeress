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
SOURCE, TARGET = "a1b2c3d4", "e5f60718"  # two tabs of one site

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


# -------------------------------------------------------- the search on steps ---


def steps_of(found: miner.Candidate) -> list[tuple[str, str, int, bool]]:
    return [(step.kind, step.path, step.repeats, step.other_tab) for step in found.steps or ()]


def test_a_sequence_that_recurs_on_three_days_is_found_with_its_steps() -> None:
    events = [*job(MONDAY), *job(WEDNESDAY), *job(FRIDAY)]

    (found,) = steps.recurring(miner.sessions(events))

    assert (found.days, found.sessions, found.hour) == (
        3,
        {MONDAY: 1, WEDNESDAY: 1, FRIDAY: 1},
        9,
    )
    assert (found.first_seen, found.last_seen) == (f"{MONDAY}T09:00:01", f"{FRIDAY}T09:00:10")
    assert found.sites == ("erp.example.com",) * 4
    assert steps_of(found) == [
        ("click", "/orders", 3, True),
        ("click", "/report", 3, True),
        ("input", "/report", 3, False),
        ("click", "/report", 1, False),
    ]


def test_how_many_events_a_step_stood_for_is_what_it_stood_for_most_often() -> None:
    many = [*job(MONDAY, rounds=3), *job(WEDNESDAY, rounds=12), *job(FRIDAY, rounds=12)]
    few = [*job(MONDAY, rounds=3), *job(WEDNESDAY, rounds=5), *job(FRIDAY, rounds=12)]

    (found,) = steps.recurring(miner.sessions(many))
    (other,) = steps.recurring(miner.sessions(few))

    assert [step.repeats for step in found.steps or ()] == [12, 12, 12, 1]
    # Seen as often on two days, the smaller claims less.
    assert [step.repeats for step in other.steps or ()] == [3, 3, 3, 1]


def test_a_sequence_seen_on_two_days_is_not_found() -> None:
    assert steps.recurring(miner.sessions([*job(MONDAY), *job(TUESDAY)])) == []


def test_work_that_differs_every_day_is_not_found() -> None:
    clock = Clock(MONDAY)
    first = [did("click", clock.now(), CELL)]
    clock = Clock(TUESDAY)
    second = [did("click", clock.now(), SAVE)]
    clock = Clock(WEDNESDAY)
    third = [did("click", clock.now(), FIELD)]
    clock = Clock(THURSDAY)
    fourth = [did("input", clock.now(), FIELD, field="text")]

    assert steps.recurring(miner.sessions([*first, *second, *third, *fourth])) == []


def test_a_part_held_by_a_longer_sequence_is_left_out() -> None:
    """The same three steps every day, and on the first two days a fourth after them. The
    three are found. On their own the first two would be found too, and are left out."""
    events = [
        *job(MONDAY)[:3],
        did("click", f"{MONDAY}T09:00:04", SAVE),
        *job(WEDNESDAY)[:3],
        did("click", f"{WEDNESDAY}T09:00:04", SAVE),
        *job(FRIDAY)[:3],
    ]

    found = steps.recurring(miner.sessions(events))

    # The sitting begins with the first step, so it was not done in another tab.
    assert [steps_of(candidate) for candidate in found] == [
        [
            ("click", "/orders", 1, False),
            ("click", "/report", 1, True),
            ("input", "/report", 1, False),
        ]
    ]


def test_two_tasks_on_one_site_never_share_a_name() -> None:
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day, 10)
        events += [
            *job(day),
            went("erp.example.com", f"{day}T10:00:00"),
            did("input", clock.now(), FIELD, path="/search", field="search"),
            did("click", clock.now(), SAVE, path="/search"),
        ]

    found = steps.recurring(miner.sessions(events))
    names = {miner.name_of(candidate) for candidate in found}

    assert len(found) == 2 and len(names) == 2
    for name in names:
        assert name.startswith("erp-example-com-") and proposals.NAME_SHAPE.fullmatch(name)


def test_the_name_of_a_sequence_of_steps_is_what_a_flow_may_have() -> None:
    (found,) = steps.recurring(miner.sessions([*job(MONDAY), *job(WEDNESDAY), *job(FRIDAY)]))

    name = miner.name_of(found)

    assert name == "erp-example-com-" + name.rsplit("-", 1)[1]
    assert proposals.NAME_SHAPE.fullmatch(name)


def test_the_model_is_told_the_kinds_of_the_steps_and_no_page_and_no_element() -> None:
    events = [*job(MONDAY), *job(WEDNESDAY), *job(FRIDAY)]
    (found,) = steps.recurring(miner.sessions(events))

    prompt = miner.prompt_for([found])

    assert (
        "sites: erp.example.com (always in this order) · steps: click, click, input, click"
        in prompt
    )
    assert "orders" not in prompt and "report" not in prompt and "qty" not in prompt
    assert "line-#" not in prompt and "save" not in prompt.split("Answer with")[0]


# ------------------------------------------------- beside the search on sites ---


def two_sites(day: str, first: str, then: str) -> list[activity.Event]:
    """A click on one site and an input on another, a second apart."""
    clock = Clock(day)
    return [
        did("click", clock.now(), (first.split("[")[0], first), site="a.example"),
        did("input", clock.now(), (then.split("[")[0], then), site="b.example", field="text"),
    ]


WANTED = 'button[data-testid="save"]'
TYPED = 'input[name="line-#"]'


def test_what_the_steps_say_wholly_of_the_sites_takes_the_place_of_the_sites() -> None:
    events = [
        event for day in (MONDAY, WEDNESDAY, FRIDAY) for event in two_sites(day, WANTED, TYPED)
    ]

    found = steps.beside(
        miner.recurring(miner.sessions(events)), steps.recurring(miner.sessions(events))
    )

    assert [(candidate.sites, candidate.steps is not None) for candidate in found] == [
        (("a.example", "b.example"), True)
    ]


def test_the_sites_stay_when_the_steps_were_seen_on_fewer_days() -> None:
    events: list[activity.Event] = []
    for day in (MONDAY, TUESDAY, WEDNESDAY, FRIDAY):
        # Another click on a on Tuesday: the sites recur on four days, the steps on three.
        first = WANTED if day != TUESDAY else 'button[data-testid="other"]'
        events += two_sites(day, first, TYPED)

    sittings = miner.sessions(events)

    found = steps.beside(miner.recurring(sittings), steps.recurring(sittings))

    # Strongest first: the sites on four days, then the steps on three.
    assert [(candidate.days, candidate.steps is not None) for candidate in found] == [
        (4, False),
        (3, True),
    ]


def test_a_ritual_whose_order_varies_is_never_left_out() -> None:
    """a and b in one order on two days and in the other on one. The set of them recurs. So
    does a click on a and an input on b at the end of every day."""
    clock = Clock(MONDAY)
    monday = [
        did("click", clock.now(), CELL, site="a.example"),
        did("click", clock.now(), SAVE, site="a.example"),
        did("input", clock.now(), FIELD, site="b.example", field="text"),
    ]
    clock = Clock(WEDNESDAY)
    wednesday = [
        did("click", clock.now(), CELL, site="b.example"),
        did("click", clock.now(), SAVE, site="a.example"),
        did("input", clock.now(), FIELD, site="b.example", field="text"),
    ]
    clock = Clock(FRIDAY)
    friday = [
        did("click", clock.now(), CELL, site="a.example"),
        did("click", clock.now(), SAVE, site="a.example"),
        did("input", clock.now(), FIELD, site="b.example", field="text"),
    ]
    sittings = miner.sessions([*monday, *wednesday, *friday])

    found = steps.beside(
        [*miner.recurring(sittings), *miner.clusters(sittings)],
        steps.recurring(sittings),
    )

    assert sorted((candidate.order, candidate.steps is not None) for candidate in found) == [
        ("fixed", True),
        ("varies", False),
    ]


def test_the_sites_stay_when_the_steps_are_a_part_of_them() -> None:
    """A click on a and an input on b every day, then another click on c each day. The steps
    found are the first two; the sites found are a, b and c, which say more. As both were
    seen on as many days and in as many sittings, the alphabet decides the order."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, WEDNESDAY, FRIDAY)):
        clock = Clock(day)
        # Another name every day, and not another number: a digit is # in a step.
        events += [
            did("click", clock.now(), SAVE, site="a.example"),
            did("input", clock.now(), FIELD, site="b.example", field="text"),
            did("click", clock.now(), ("td", f'td[name="c{chr(97 + at)}"]'), site="c.example"),
        ]

    sittings = miner.sessions(events)

    found = steps.beside(miner.recurring(sittings), steps.recurring(sittings))

    assert [(candidate.sites, candidate.steps is not None) for candidate in found] == [
        (("a.example", "b.example"), True),
        (("a.example", "b.example", "c.example"), False),
    ]


# ------------------------------------------------------------- what is shown ---


def test_the_lines_show_what_was_done_where_how_often_and_in_which_tab() -> None:
    events = [*job(MONDAY, rounds=12), *job(WEDNESDAY, rounds=12), *job(FRIDAY, rounds=12)]
    (found,) = steps.recurring(miner.sessions(events))

    assert steps.lines(found.steps or ()) == [
        '  steps:  1. click  /orders  td[name="qty-#"]  ×12  other tab',
        '          2. click  /report  input[name="line-#"]  ×12  other tab',
        '          3. input  /report  input[name="line-#"]  text  ×12',
        '          4. click  /report  button[data-testid="save"]',
    ]


def test_no_step_shows_no_line() -> None:
    assert steps.lines(()) == []


def test_an_element_is_shown_cut_and_never_past_its_eightieth_character() -> None:
    (step,) = steps.lines(
        [
            miner.Step(
                "click",
                "erp.example.com",
                "/",
                "button[" + "x" * 120 + "]",
                None,
                1,
                False,
            )
        ]
    )

    assert step == "  steps:  1. click  /  " + "button[" + "x" * 73
