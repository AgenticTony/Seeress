"""Each schema agrees with the code that reads its message.

Whatever a schema accepts, the reader accepts, with the same fields. Whatever it refuses,
the reader refuses. A schema that drifts from its reader is a contract nobody can rely on.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from seeress import activity, bodies

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def schema(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(document)
    return document


def accepted(document: dict[str, Any], instance: object) -> bool:
    return bool(jsonschema.Draft202012Validator(document).is_valid(instance))


# ------------------------------------------------------- browser body probe ---

PROBE = schema("browser-body-probe.schema.json")

VALID = [
    {"daemon": "seeress-client", "kind": "cdp", "marker": "a-marker", "error": None},
    {"daemon": "seeress-client", "kind": "cdp", "marker": None, "error": None},
    {"daemon": "default", "kind": "local", "marker": None, "error": None},
    {"daemon": "default", "kind": "cdp", "marker": "some-marker", "error": None},
    {"daemon": "default-chromium", "kind": "cdp", "marker": "", "error": None},
    {"daemon": "remote", "kind": "cloud", "marker": None, "error": None},
    {"daemon": "seeress-client", "kind": "cdp", "marker": None, "error": "IPCError"},
]

REFUSED = [
    {},
    {"daemon": "seeress-client", "kind": "cdp", "marker": None},
    {"daemon": "", "kind": "cdp", "marker": None, "error": None},
    {"daemon": "seeress-client", "kind": None, "marker": None, "error": None},
    {"daemon": "seeress-client", "kind": "web", "marker": None, "error": None},
    {"daemon": "seeress-client", "kind": "cdp", "marker": 7, "error": None},
    {"daemon": "seeress-client", "kind": "cdp", "marker": None, "error": ["x"]},
    {"daemon": 3, "kind": "cdp", "marker": None, "error": None},
    {
        "daemon": "seeress-client",
        "kind": "cdp",
        "marker": None,
        "error": None,
        "url": "https://a.page/",
    },
    [],
    "SEERESS_PROBE {}",
    None,
]


def read(output: object) -> bodies.Probe | None:
    if not isinstance(output, str):
        return None
    try:
        return bodies.Probe.parse("noise\nSEERESS_PROBE " + output)
    except ValueError:
        return None


@pytest.mark.parametrize("instance", VALID, ids=lambda instance: json.dumps(instance))
def test_the_schema_and_the_reader_agree_on_every_accepted_probe(instance: dict[str, Any]) -> None:
    assert accepted(PROBE, instance)

    probe = read(json.dumps(instance))

    assert probe is not None
    assert probe == bodies.Probe(
        instance["daemon"], instance["kind"], instance["marker"], instance["error"]
    )


@pytest.mark.parametrize("instance", REFUSED, ids=lambda instance: repr(instance)[:60])
def test_the_schema_and_the_reader_agree_on_every_refused_probe(instance: object) -> None:
    assert not accepted(PROBE, instance)

    assert read(json.dumps(instance)) is None


def test_a_probe_is_read_from_a_marked_line_only() -> None:
    good = json.dumps({"daemon": "seeress-client", "kind": "cdp", "marker": "m", "error": None})

    probe = bodies.Probe.parse("browser-use: a line of its own\nSEERESS_PROBE " + good)

    assert probe is not None and probe.marker == "m"
    with pytest.raises(ValueError, match="no probe"):
        bodies.Probe.parse(good)


def run_probe(kind: str, found: dict[str, Any] | None) -> dict[str, Any]:
    """Run the tool's own probe text with a stand-in browser, and return what it printed."""
    printed: list[str] = []

    def cdp(method: str, **params: Any) -> dict[str, Any]:
        if found is None:
            raise RuntimeError("wasn't found")
        return found

    def print_probe(*values: object, **options: object) -> None:
        printed.append("".join(str(value) for value in values))

    namespace: dict[str, Any] = {
        "NAME": "seeress-client",
        "daemon_browser_kind": lambda: kind,
        "cdp": cdp,
        "print": print_probe,
    }
    exec(bodies.PROBE_CODE, namespace)  # noqa: S102 - Seeress's own probe text, read above
    (line,) = printed
    assert line.startswith("SEERESS_PROBE ")
    instance: dict[str, Any] = json.loads(line[len("SEERESS_PROBE ") :])
    return instance


@pytest.mark.parametrize("kind", ["local", "cdp", "cloud"])
def test_the_probe_the_tool_is_told_to_print_matches_the_schema(kind: str) -> None:
    """The tool's own code, run with a stand-in browser, produces what the schema says."""
    no_cookie: dict[str, Any] = {"cookies": []}
    one_cookie: dict[str, Any] = {"cookies": [{"name": "seeress_body", "value": "n"}]}

    for found, marker, error in (
        (no_cookie, None, None),
        (one_cookie, "n", None),
        (None, None, "RuntimeError"),
    ):
        instance = run_probe(kind, found)

        assert accepted(PROBE, instance), instance
        assert instance == {
            "daemon": "seeress-client",
            "kind": kind,
            "marker": marker,
            "error": error,
        }


# ----------------------------------------------------------- activity event ---


ACTIVITY_EVENT = schema("activity-event.schema.json")

AN_EVENT = {
    "v": 1,
    "ts": "2026-11-20T09:15:02",
    "kind": "navigate",
    "site": "example.org",
    "path": "/invoices/7",
    "target": "1a2b3c4d",
}

ACCEPTED_EVENTS = [
    pytest.param(AN_EVENT, id="navigate"),
    pytest.param({**AN_EVENT, "kind": "focus", "target": None}, id="focus, no tab"),
    pytest.param({**AN_EVENT, "kind": "click", "path": "/"}, id="click"),
    pytest.param({**AN_EVENT, "kind": "input", "site": "mail.example.net"}, id="input"),
    pytest.param(
        {**AN_EVENT, "kind": "submit", "site": "intranet.corp", "path": "/a/b/"}, id="submit"
    ),
    pytest.param({**AN_EVENT, "kind": "select", "target": "5e6f7a8b"}, id="select"),
    # JSON has one kind of number, so 1.0 is 1.
    pytest.param({**AN_EVENT, "v": 1.0}, id="v written 1.0"),
    # The schema judges the shape of a ts, not whether the date exists.
    pytest.param({**AN_EVENT, "ts": "2026-13-45T25:61:61"}, id="ts that is no real date"),
]

REFUSED_EVENTS = [
    pytest.param({**AN_EVENT, "v": 2}, id="v of another value"),
    pytest.param({**AN_EVENT, "v": "1"}, id="v of another type"),
    # True is 1 in Python. It is not a number in JSON.
    pytest.param({**AN_EVENT, "v": True}, id="v true"),
    pytest.param({**AN_EVENT, "ts": "2026-11-20"}, id="ts date only"),
    pytest.param({**AN_EVENT, "ts": "2026-11-20T09:15:02.5"}, id="ts with a fraction of a second"),
    pytest.param({**AN_EVENT, "ts": "2026-11-20T09:15:02+01:00"}, id="ts with an offset"),
    pytest.param({**AN_EVENT, "ts": "2026-11-20 09:15:02"}, id="ts of an impossible shape"),
    pytest.param({**AN_EVENT, "ts": 20261120091505}, id="ts of another type"),
    # JSON Schema writes a pattern in the ECMA-262 dialect, where the end of a pattern is
    # the end of the string, newline included. The reader ends its patterns the same way.
    pytest.param({**AN_EVENT, "ts": "2026-11-20T09:15:02\n"}, id="ts ending in a newline"),
    pytest.param({**AN_EVENT, "site": "example.org\n"}, id="site ending in a newline"),
    pytest.param({**AN_EVENT, "target": "1a2b3c4d\n"}, id="target ending in a newline"),
    pytest.param(
        {**AN_EVENT, "kind": "nav"}, id="the logger's word, where the event says navigate"
    ),
    pytest.param({**AN_EVENT, "kind": "hover"}, id="kind no event has"),
    pytest.param({**AN_EVENT, "kind": None}, id="kind of another type"),
    pytest.param({**AN_EVENT, "site": ""}, id="site empty"),
    pytest.param({**AN_EVENT, "site": 7}, id="site of another type"),
    pytest.param({**AN_EVENT, "site": "https://example.org"}, id="site with a scheme"),
    pytest.param({**AN_EVENT, "site": "example.org/invoices"}, id="site with a path"),
    pytest.param({**AN_EVENT, "site": "www.example.org"}, id="site starting with www."),
    pytest.param({**AN_EVENT, "path": "invoices/7"}, id="path not starting with a slash"),
    pytest.param({**AN_EVENT, "path": 7}, id="path of another type"),
    pytest.param({**AN_EVENT, "target": "1a2b3c4d5"}, id="target of the wrong length"),
    pytest.param({**AN_EVENT, "target": "1A2B3C4D"}, id="target of other characters"),
    pytest.param({**AN_EVENT, "target": 7}, id="target of another type"),
    pytest.param(
        {name: value for name, value in AN_EVENT.items() if name != "target"},
        id="a missing field",
    ),
    pytest.param({**AN_EVENT, "title": "Invoices"}, id="a field the schema does not know"),
    pytest.param(None, id="something that is not an object"),
]


@pytest.mark.parametrize("instance", ACCEPTED_EVENTS)
def test_the_schema_and_the_reader_agree_on_every_accepted_event(instance: object) -> None:
    assert accepted(ACTIVITY_EVENT, instance)

    event = activity.Event.parse(instance)

    assert event.as_dict() == instance


@pytest.mark.parametrize("instance", REFUSED_EVENTS)
def test_the_schema_and_the_reader_agree_on_every_refused_event(instance: object) -> None:
    assert not accepted(ACTIVITY_EVENT, instance)

    with pytest.raises(ValueError, match="unexpected shape"):
        activity.Event.parse(instance)


def test_an_event_is_returned_as_a_json_object_in_the_schema_s_order() -> None:
    """The fields come in the schema's order, and the version as the integer 1, however written."""
    event = activity.Event.parse({**AN_EVENT, "v": 1.0})

    assert list(event.as_dict()) == ["v", "ts", "kind", "site", "path", "target"]
    assert json.dumps(event.as_dict()) == json.dumps(AN_EVENT)


def test_the_reader_holds_the_schemas_own_patterns_and_words() -> None:
    """The reader searches with the patterns the schema writes, and with its list of words.
    An instance can show only that the two agree on that instance."""
    properties = ACTIVITY_EVENT["properties"]

    assert list(activity.KINDS) == properties["kind"]["enum"]
    assert activity.TS_SHAPE.pattern == properties["ts"]["pattern"]
    assert activity.SITE_SHAPE.pattern == properties["site"]["pattern"]
    assert activity.PATH_SHAPE.pattern == properties["path"]["pattern"]
    assert activity.TARGET_SHAPE.pattern == properties["target"]["pattern"]
