"""A sitting as steps, and the sequences of steps that recur across days.

Every test works on invented events and reads no clock. The model is always a fake that
answers with one text and remembers what it was asked.
"""

from collections.abc import Sequence

import pytest

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


def word(number: int) -> str:
    """A number as four letters: a digit in a name or in a page is hidden, and letters are not."""
    return "".join(chr(97 + (number // (26**at)) % 26) for at in range(4))


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


def test_the_longest_sequence_on_sites_is_found_whole_and_one_on_two_days_is_not() -> None:
    """Five sites recur on three days, with a sixth on two. The five are found and the sixth
    is not.

    That a longer block is looked for only where the one before it recurs cannot be pinned by
    what is found: switched off, the same is found, only slower. It is a guard of time.
    """
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

    assert name.startswith("erp-example-com-")
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


# ------------------------------------------------- what the mutants found ---


def test_a_step_done_three_times_with_one_of_them_in_another_tab_is_not_said_to_be() -> None:
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), CELL, tab=TARGET),
        did("click", clock.now(), CELL),
        did("click", clock.now(), CELL),
    ]

    (only,) = steps.of(sitting_of(events))

    assert only.step is not None and (only.step.repeats, only.step.other_tab) == (3, False)


def _said_of_another_tab(tabs: Sequence[str]) -> list[bool]:
    """Four days, a sitting of three steps on each, the second and the third in the tab of its
    day: what is said of each step about another tab than the one before it."""
    events: list[activity.Event] = []
    for day, tab in zip((MONDAY, TUESDAY, WEDNESDAY, THURSDAY), tabs, strict=True):
        clock = Clock(day)
        events += [
            did("click", clock.now(), SAVE, path="/menu"),
            did("click", clock.now(), CELL, tab=tab),
            did("input", clock.now(), FIELD, field="text", tab=tab),
        ]
    (found,) = steps.recurring(miner.sessions(events))
    return [step.other_tab for step in found.steps or ()]


def test_a_step_is_said_to_be_in_another_tab_when_it_was_so_in_more_sittings_than_not() -> None:
    assert _said_of_another_tab((SOURCE, SOURCE, TARGET, TARGET)) == [False, False, False]
    assert _said_of_another_tab((SOURCE, TARGET, TARGET, TARGET)) == [False, True, False]


def test_two_steps_on_the_first_site_still_take_the_place_of_the_sites() -> None:
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [
            did("click", clock.now(), CELL, site="a.example"),
            did("click", clock.now(), SAVE, site="a.example"),
            did("input", clock.now(), FIELD, site="b.example", field="text"),
        ]
    sittings = miner.sessions(events)

    found = steps.beside(miner.recurring(sittings), steps.recurring(sittings))

    assert [(candidate.sites, candidate.steps is not None) for candidate in found] == [
        (("a.example", "a.example", "b.example"), True)
    ]


def test_a_sequence_of_twelve_steps_is_found_whole() -> None:
    twelve = [(f"/{letter}", ("td", f'td[name="s{letter}"]')) for letter in "abcdefghijkl"]
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [did("click", clock.now(), element, path=path) for path, element in twelve]

    (found,) = steps.recurring(miner.sessions(events))

    assert [(step.path, step.element) for step in found.steps or ()] == [
        (path, element[1]) for path, element in twelve
    ]


def test_a_round_of_seven_steps_is_left_as_it_came() -> None:
    seven = [(f"/{letter}", ("td", f'td[name="s{letter}"]')) for letter in "abcdefg"]
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), element, path=path) for _ in range(3) for path, element in seven
    ]

    found = steps.of(sitting_of(events))

    assert len(found) == 21
    assert all(unit.step is not None and unit.step.repeats == 1 for unit in found)


def test_a_step_done_twice_with_one_of_them_in_another_tab_is_not_said_to_be() -> None:
    # The first click is the first step of the sitting. The two that follow are alike, in one
    # tab, and that tab is another than the tab of the step before them: one of two.
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), SAVE, tab=TARGET),
        did("click", clock.now(), CELL),
        did("click", clock.now(), CELL),
    ]

    (_, only) = steps.of(sitting_of(events))

    assert only.step is not None and (only.step.repeats, only.step.other_tab) == (2, False)


def test_another_tab_is_judged_against_the_step_before_and_not_the_event_before() -> None:
    """A click in one tab, a navigation in the other, the same click in the first tab again:
    three clicks, each in the tab of the step before it. A navigation between two steps says
    nothing of where a step was done."""
    clock = Clock(MONDAY)
    events: list[activity.Event] = []
    for _ in range(3):
        events += [
            did("click", clock.now(), CELL),
            activity.Event(1, clock.now(), "navigate", "erp.example.com", "/", TARGET),
        ]

    (only,) = steps.of(sitting_of(events))

    assert only.step is not None and (only.step.repeats, only.step.other_tab) == (3, False)


def test_another_tab_is_judged_by_sittings_and_not_by_occurrences() -> None:
    """On Monday the two steps come six times, five of them with the second in another tab; on
    Wednesday and Friday once, in the same tab. In more of the occurrences than not, and in one
    sitting of three: not said."""
    clock = Clock(MONDAY)
    monday: list[activity.Event] = []
    for at in range(6):
        monday += [
            did("click", clock.now(), SAVE, path="/menu"),
            did("click", clock.now(), CELL, tab=TARGET if at < 5 else SOURCE),
            did("click", clock.now(), ("td", f'td[name="other{chr(97 + at)}"]'), path="/elsewhere"),
        ]
    later: list[activity.Event] = []
    for day in (WEDNESDAY, FRIDAY):
        clock = Clock(day)
        later += [
            did("click", clock.now(), SAVE, path="/menu"),
            did("click", clock.now(), CELL),
            did("click", clock.now(), ("td", 'td[name="else"]'), path="/elsewhere"),
        ]

    (found,) = steps.recurring(miner.sessions([*monday, *later]))

    assert [(step.kind, step.path) for step in found.steps or ()] == [
        ("click", "/menu"),
        ("click", "/"),
    ]
    assert [step.other_tab for step in found.steps or ()] == [False, False]


def test_another_tab_is_said_when_more_sittings_than_not_say_it_however_many_occurrences() -> None:
    """The mirror of the one before: on Monday six occurrences, none in another tab; on
    Wednesday and Friday one each, in another tab. In two sittings of three, and in two
    occurrences of eight: said."""
    clock = Clock(MONDAY)
    monday: list[activity.Event] = []
    for at in range(6):
        monday += [
            did("click", clock.now(), SAVE, path="/menu"),
            did("click", clock.now(), CELL),
            did("click", clock.now(), ("td", f'td[name="other{chr(97 + at)}"]'), path="/elsewhere"),
        ]
    later: list[activity.Event] = []
    for day in (WEDNESDAY, FRIDAY):
        clock = Clock(day)
        later += [
            did("click", clock.now(), SAVE, path="/menu"),
            did("click", clock.now(), CELL, tab=TARGET),
            did("click", clock.now(), ("td", 'td[name="else"]'), path="/elsewhere"),
        ]

    (found,) = steps.recurring(miner.sessions([*monday, *later]))

    assert [step.other_tab for step in found.steps or ()] == [False, True]


def test_the_kind_of_a_field_tells_two_steps_apart() -> None:
    clock = Clock(MONDAY)
    events = [
        did("input", clock.now(), FIELD, field="text"),
        did("input", clock.now(), FIELD, field="search"),
    ]

    found = steps.of(sitting_of(events))

    assert [unit.step.field if unit.step else None for unit in found] == ["text", "search"]
    assert all(len(unit.events) == 1 for unit in found)


def test_a_page_or_a_name_that_holds_what_tells_a_key_apart_makes_no_other_step() -> None:
    """The reader keeps a control character in the name of an element. What tells the parts of
    a key apart is more than a character then: two different steps never share a key."""
    # A character that would tell the parts of a key apart, if a key were its parts joined.
    inside = "\x1f" + "click" + "\x1f"
    clock = Clock(MONDAY)
    events = [
        did("click", clock.now(), ("td", "x" + inside + "y"), path="/a"),
        did("click", clock.now(), ("td", "y"), path="/a" + inside + "x"),
        did("click", clock.now(), CELL),
    ]

    found = steps.of(sitting_of(events))

    assert len(found) == 3
    assert len({unit.key for unit in found}) == 3


# ------------------------------------------------- what a page chose to name ---


def test_a_name_with_a_line_break_in_it_adds_no_line_to_the_report() -> None:
    """An element's name is text a page chooses. A line break in it made a second line of the
    report, in the report's own words. It is shown as its escape."""
    forged = 'button[id="Save\n  seen:   on 99 days, in 99 sittings"]'

    (line,) = steps.lines([miner.Step("click", "erp.example.com", "/", forged, None, 1, False)])

    assert (
        line
        == '  steps:  1. click  /  button[id="Save\\u000a  seen:   on 99 days, in 99 sittings"]'
    )
    assert (
        len(steps.lines([miner.Step("click", "erp.example.com", "/", forged, None, 1, False)])) == 1
    )


@pytest.mark.parametrize(
    "chosen",
    [
        "\x1bc",  # ESC c: a terminal may read it as a command
        "\x9bJ",  # CSI J
        "\u202e",  # a right-to-left override
        "\u200b",  # a zero-width space
        "e\u0301",  # a letter and a combining mark: shown apart, and never as one sign
        "\u2028",  # a line separator
    ],
    ids=["escape", "csi", "override", "zero-width", "combining", "separator"],
)
def test_what_is_not_printable_nor_a_letter_nor_a_digit_is_shown_as_its_escape(chosen: str) -> None:
    shown = steps.lines([miner.Step("click", "erp.example.com", "/", chosen, None, 1, False)])

    (line,) = shown
    assert line.startswith("  steps:  1. click  /  ")
    assert line[len("  steps:  1. click  /  ") :] == "".join(
        character if character.isalnum() else f"\\u{ord(character):04x}" for character in chosen
    )


def test_a_letter_of_any_script_and_a_page_are_shown_as_they_are() -> None:
    (line,) = steps.lines(
        [
            miner.Step(
                "input",
                "erp.example.com",
                "/beställningar/{}",
                'input[name="företag"]',
                "text",
                1,
                False,
            )
        ]
    )

    assert line == '  steps:  1. input  /beställningar/{}  input[name="företag"]  text'


def test_nothing_of_a_page_reaches_the_report_as_the_terminal_reads_it() -> None:
    """A page is encoded in an address by the browser, and is escaped here all the same."""
    (line,) = steps.lines(
        [miner.Step("click", "erp.example.com", "/a\x1b[2Jb", 'td[name="x"]', None, 1, False)]
    )

    assert line == '  steps:  1. click  /a\\u001b[2Jb  td[name="x"]'


def test_a_task_of_fifty_steps_is_one_proposal_and_not_a_window_of_it() -> None:
    """A form of fifty fields filled the same way on three days: one proposal holds the whole
    task. By windows of twelve it was thirty-nine proposals, each with a name of its own, and
    a person who dismissed one had the rest to dismiss."""
    fifty = [(f"/{word(at)}", ("td", f'td[name="f{word(at)}"]')) for at in range(50)]
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [
            did("input", clock.now(), element, path=path, field="text") for path, element in fifty
        ]

    found = steps.recurring(miner.sessions(events))

    assert [len(candidate.steps or ()) for candidate in found] == [50]
    assert found[0].steps is not None
    assert (found[0].steps[0].path, found[0].steps[-1].element) == (fifty[0][0], fifty[-1][1][1])


# ------------------------------------------------- what a review broke ---


def test_the_page_tells_two_steps_apart() -> None:
    """A click on Save at /orders and a click on Save at /report, every day: two steps."""
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [
            did("click", clock.now(), SAVE, path="/orders"),
            did("click", clock.now(), SAVE, path="/report"),
        ]

    (found,) = steps.recurring(miner.sessions(events))

    assert [step.path for step in found.steps or ()] == ["/orders", "/report"]


def test_the_site_tells_two_steps_apart() -> None:
    """A click on Save on one site and a click on Save on another, every day: two steps."""
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [
            did("click", clock.now(), SAVE, site="a.example"),
            did("click", clock.now(), SAVE, site="b.example"),
        ]

    (found,) = steps.recurring(miner.sessions(events))

    assert [step.site for step in found.steps or ()] == ["a.example", "b.example"]


def test_work_whose_elements_differ_every_day_is_not_found() -> None:
    """The same two pages every day, and another element on each every day. Were the element
    no part of a step, the same two steps would be found on every day."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, TUESDAY, WEDNESDAY, THURSDAY)):
        clock = Clock(day)
        events += [
            did("click", clock.now(), ("td", f'td[name="e{word(at)}"]'), path="/a"),
            did("input", clock.now(), ("td", f'td[name="f{word(at)}"]'), path="/b", field="text"),
        ]

    assert steps.recurring(miner.sessions(events)) == []


def test_the_sites_stay_when_the_steps_say_them_in_another_order() -> None:
    """Every day: a look at a, at b, and at a site of that day alone; then a click on b and an
    input on a. The sites recur as a then b, and as b then a. The steps say b then a, and take
    the place of that alone."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, WEDNESDAY, FRIDAY)):
        clock = Clock(day)
        events += [
            went("a.example", clock.now()),
            went("b.example", clock.now()),
            went(f"z{at}.example", clock.now()),
            did("click", clock.now(), SAVE, site="b.example"),
            did("input", clock.now(), FIELD, site="a.example", field="text"),
        ]
    sittings = miner.sessions(events)

    found = steps.beside(miner.recurring(sittings), steps.recurring(sittings))

    assert [(candidate.sites, candidate.steps is not None) for candidate in found] == [
        (("a.example", "b.example"), False),
        (("b.example", "a.example"), True),
    ]


def test_the_sites_stay_when_they_came_in_more_sittings_than_the_steps() -> None:
    """The sites a then b in five sittings on each of three days; the same two steps in one
    sitting of each, and other work in the rest. The sites say how often, and stay."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, WEDNESDAY, FRIDAY)):
        for sitting in range(5):
            clock = Clock(day, 9 + sitting)
            if sitting == 0:
                events += [
                    did("click", clock.now(), SAVE, site="a.example"),
                    did("input", clock.now(), FIELD, site="b.example", field="text"),
                ]
            else:
                own = f"{word(at * 5 + sitting)}"
                events += [
                    did("click", clock.now(), ("td", f'td[name="x{own}"]'), site="a.example"),
                    did(
                        "input",
                        clock.now(),
                        ("td", f'td[name="y{own}"]'),
                        site="b.example",
                        field="text",
                    ),
                ]
    sittings = miner.sessions(events)

    found = steps.beside(miner.recurring(sittings), steps.recurring(sittings))

    assert [(candidate.sites, candidate.steps is not None) for candidate in found] == [
        (("a.example", "b.example"), False),
        (("a.example", "b.example"), True),
    ]
    assert [sum(candidate.sessions.values()) for candidate in found] == [15, 3]


def test_the_model_is_told_one_line_of_a_task_and_nothing_more_of_it() -> None:
    events = [*job(MONDAY), *job(WEDNESDAY), *job(FRIDAY)]
    (found,) = steps.recurring(miner.sessions(events))

    prompt = miner.prompt_for([found])

    (line,) = [line for line in prompt.splitlines() if line.startswith("1.")]
    assert line == (
        "1. sites: erp.example.com (always in this order) · steps: click, click, input, click"
        " · usually starts at hour 9 · seen on 3 days, in 3 sessions"
    )


def test_a_sitting_in_which_half_the_occurrences_are_in_another_tab_says_nothing() -> None:
    """On each of three days the two steps come four times, twice with the second in another
    tab: in half of twelve occurrences, and in no sitting more than half."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, WEDNESDAY, FRIDAY)):
        clock = Clock(day)
        for round_ in range(4):
            filler = ("td", f'td[name="other{word(at)}{chr(97 + round_)}"]')  # of its day alone
            events += [
                did("click", clock.now(), SAVE, path="/menu"),
                did("click", clock.now(), CELL, tab=TARGET if round_ < 2 else SOURCE),
                did("click", clock.now(), filler, path="/elsewhere"),
            ]

    (found,) = steps.recurring(miner.sessions(events))

    assert [step.other_tab for step in found.steps or ()] == [False, False]


def test_the_strongest_holder_of_a_part_decides_and_not_the_last_looked_at() -> None:
    """Five days of p, q, r; on three of them p, q, z comes later, with a site of its own on
    either side. The pair p, q recurs on five days. It is held on five by p, q, r, and on
    three by p, q, z, which is looked at after it: what the weaker says must not undo what
    the stronger did."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY)):
        clock = Clock(day)
        sites = ["p", "q", "r", f"u{at}"]
        if at < 3:
            sites += [f"v{at}", "p", "q", "z", f"w{at}"]
        events += [went(f"{site}.example", clock.now()) for site in sites]

    found = miner.recurring(miner.sessions(events), sizes=(2, 6))

    assert ("p.example", "q.example") not in [candidate.sites for candidate in found]


# ------------------------------------------------- a task longer than fifty ---


def task_of(
    count: int, days: Sequence[str] = (MONDAY, WEDNESDAY, FRIDAY), first: int = 0
) -> list[activity.Event]:
    """A task of count steps, each on a page and an element of its own, done the same way on
    every day, from the step numbered first."""
    steps_of_it = [
        (f"/t/{word(at)}", ("td", f'td[name="f{word(at)}"]')) for at in range(first, first + count)
    ]
    events: list[activity.Event] = []
    for day in days:
        clock = Clock(day)
        events += [
            did("input", clock.now(), element, path=path, field="text")
            for path, element in steps_of_it
        ]
    return events


def test_a_task_of_a_hundred_steps_is_one_proposal_by_its_first_fifty_steps() -> None:
    """The windows of fifty overlap by forty-nine steps, and a task of a hundred gave fifty-one
    proposals, each with a name of its own. A window that comes one step after another, in a
    sitting, is the same run seen one step on: the first is kept and the rest are held."""
    found = steps.recurring(miner.sessions(task_of(100)))

    assert [(len(candidate.steps or ()), candidate.further) for candidate in found] == [(50, True)]
    assert found[0].steps is not None
    assert (found[0].steps[0].path, found[0].steps[-1].element) == (
        f"/t/{word(0)}",
        f'td[name="f{word(49)}"]',
    )


def test_a_task_of_fifty_is_whole_and_is_not_said_to_go_on() -> None:
    (found,) = steps.recurring(miner.sessions(task_of(50)))

    assert (len(found.steps or ()), found.further) == (50, False)


def test_a_window_that_recurs_on_more_days_than_the_one_before_it_is_kept() -> None:
    """Five days of fifty steps; on three of them another step comes first. The window that
    begins with that step recurs on three days and the one after it on five: both are kept, as
    a part seen on more days than a longer sequence is kept."""
    events: list[activity.Event] = []
    for at, day in enumerate((MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY)):
        clock = Clock(day)
        if at < 3:
            events.append(
                did("input", clock.now(), ("td", 'td[name="before"]'), path="/before", field="text")
            )
        events += task_of(50, days=(day,))

    found = steps.recurring(miner.sessions(events))

    assert sorted(
        (candidate.days, candidate.steps[0].path if candidate.steps else None, candidate.further)
        for candidate in found
    ) == [(3, "/before", True), (5, f"/t/{word(0)}", False)]


def test_the_search_on_sites_does_not_join_its_windows() -> None:
    """Nine sites in a row, on three days: four proposals, as before the search on steps was
    made. The rule of one window a run is for steps alone."""
    events: list[activity.Event] = []
    for day in (MONDAY, WEDNESDAY, FRIDAY):
        clock = Clock(day)
        events += [went(f"s{at}.example", clock.now()) for at in range(9)]

    found = miner.recurring(miner.sessions(events))

    assert [len(candidate.sites) for candidate in found] == [6, 6, 6, 6]
    assert [candidate.further for candidate in found] == [False] * 4
