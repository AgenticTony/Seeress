"""End to end, with a real browser: the search on steps, from what the capture records.

A copy-and-paste job between two tabs of one site is recorded by the capture as it runs, on
three shapes of page, and the search finds it from a record of three days. The browser is
one Seeress starts, on a throwaway profile inside a throwaway home: none of this attaches to
the user's Chrome.

The paste is the debugger's insertText, which fires the input event a paste from the
keyboard fires. A real paste was not used: it reads the clipboard of the machine.

Run them with:  .venv/bin/python -m pytest -m e2e
"""

import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seeress import activity, mine, miner, proposals, steps
from seeress.activity import Event

from .cdp import Browser, listened
from .test_capture import nothing_of, record_on, waited_a_moment

pytestmark = pytest.mark.e2e

ROWS = 4
AMOUNT = "ROW-{}-amount-9137"


def _grid(named: bool) -> str:
    """A page with a grid of cells. The first cell of every row has a name, or none has."""
    cells = ""
    for row in range(ROWS):
        cells += "<tr>"
        for column in range(3):
            name = f" name='qty-{row}'" if named and column == 0 else ""
            cells += f"<td{name}>{row * 10 + column}</td>"
        cells += "</tr>"
    return (
        f'<!doctype html><title>A grid</title><meta charset="utf-8">'
        f'<table id="grid">{cells}</table>'
    )


def _form(prefix: str) -> str:
    """A page with a form of as many fields as there are rows, and a save."""
    fields = "".join(f"<input name='{prefix}-{row}' type='text'>" for row in range(ROWS))
    return (
        '<!doctype html><title>A form</title><meta charset="utf-8"><form id="entry">'
        f"{fields}"
        '<button type="button" data-testid="save">Save</button></form>'
    )


class _Handler(BaseHTTPRequestHandler):
    """Two pages of one site: the source at /orders and the target at /report."""

    named: bool = True
    source_is_a_grid: bool = True

    def do_GET(self) -> None:
        if self.path.startswith("/orders"):
            page = _grid(self.named) if self.source_is_a_grid else _form("qty")
        else:
            page = _form("line")
        sent = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(sent)))
        self.end_headers()
        self.wfile.write(sent)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def pages() -> Iterator[str]:
    """The pages served on this machine. The origin is returned."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _job(own: Browser, origin: str, source: str, *, rounds: int = 3) -> None:
    """Do a copy-and-paste job between two tabs, a round for every row: a click on a cell or a
    field of the source, then, in the other tab, a click on a field and a paste into it. Then
    a save."""
    from_tab = own.tab(f"{origin}/orders")
    to_tab = own.tab(f"{origin}/report")
    try:
        listened(from_tab)
        listened(to_tab)
        for row in range(rounds):
            from_tab.front()
            waited_a_moment()
            from_tab.click(source.format(row=row, rowplus=row + 1))
            to_tab.front()
            waited_a_moment()
            to_tab.click(f"input[name='line-{row}']")
            to_tab.call("Input.insertText", {"text": AMOUNT.format(row)})
        to_tab.click('[data-testid="save"]')
        waited_a_moment()
        waited_a_moment()
    finally:
        from_tab.close()
        to_tab.close()


def _two_days_more(home: Path, recorded: list[Event]) -> None:
    """The record of the day twice more, on the two days after."""
    for later in (1, 2):
        for event in recorded:
            day = date.fromisoformat(event.ts[:10]) + timedelta(days=later)
            activity.write_event(home, replace(event, ts=f"{day.isoformat()}{event.ts[10:]}"))


@pytest.mark.parametrize(
    ("named", "source_is_a_grid", "source", "element"),
    [
        (True, True, "td[name='qty-{row}']", 'td[name="qty-#"]'),
        (
            False,
            True,
            "#grid tr:nth-child({rowplus}) td:nth-child(1)",
            "table:nth-child(#)>tbody:nth-child(#)>tr:nth-child(#)>td:nth-child(#)",
        ),
        (False, False, "input[name='qty-{row}']", 'input[name="qty-#"]'),
    ],
    ids=["a grid that names its cells", "a grid that names no cell", "a form into a form"],
)
def test_a_copy_and_paste_job_between_two_tabs_is_found_from_three_days(
    home: Path,
    browser: tuple[Browser, str],
    pages: str,
    named: bool,
    source_is_a_grid: bool,
    source: str,
    element: str,
) -> None:
    """What the capture writes for the job is four steps once its rounds are one, and the same
    four on three days are one proposal, with no value of the job in the record."""
    _Handler.named, _Handler.source_is_a_grid = named, source_is_a_grid
    own, address = browser
    recording = record_on(home, address)
    try:
        _job(own, pages, source)
        recorded = recording.events()
    finally:
        recording.stop()
    nothing_of("amount-9137", recording.written())

    assert [(event.kind, event.path) for event in recorded] == [
        ("click", "/orders"),
        ("click", "/report"),
        ("input", "/report"),
    ] * 3 + [("click", "/report")]
    _two_days_more(home, recorded)

    (found,) = steps.recurring(miner.sessions(activity.read_events(home)))

    assert found.days == 3
    assert [(step.kind, step.path, step.element) for step in found.steps or ()] == [
        ("click", "/orders", element),
        ("click", "/report", 'input[name="line-#"]'),
        ("input", "/report", 'input[name="line-#"]'),
        ("click", "/report", 'button[data-testid="save"]'),
    ]
    assert [(step.repeats, step.other_tab) for step in found.steps or ()] == [
        (3, True),
        (3, True),
        (3, False),
        (1, False),
    ]
    assert proposals.NAME_SHAPE.fullmatch(miner.name_of(found))

    report = mine.mine(home, None, ask=None)

    assert "Proposals: 1." in report
    assert f"  steps:  1. click  /orders  {element}  ×3  other tab" in report
    assert "amount-9137" not in report
