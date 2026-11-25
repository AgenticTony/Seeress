"""Each schema agrees with the code that reads its message.

Whatever a schema accepts, the reader accepts, with the same fields. Whatever it refuses,
the reader refuses. A schema that drifts from its reader is a contract nobody can rely on.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from seeress import activity, bodies, capture

from .schemas import accepted, schema

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
CAPTURE_STATUS = schema("capture-status.schema.json")

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
    # A browser names a tab in upper case. The reader refused it from the first, and so
    # read no line of the spike logger that named a tab.
    pytest.param({**AN_EVENT, "kind": "navigate", "target": "D3DED672"}, id="a tab in upper case"),
    # JSON has one kind of number, so 1.0 is 1.
    pytest.param({**AN_EVENT, "v": 1.0}, id="v written 1.0"),
    # The schema judges the shape of a ts, not whether the date exists.
    pytest.param({**AN_EVENT, "ts": "2026-13-45T25:61:61"}, id="ts that is no real date"),
    # A browser accepts an underscore in a host name, as intranet servers often have one.
    pytest.param({**AN_EVENT, "site": "my_host.example"}, id="site with an underscore"),
    # A page chooses the address of its tab. The path and the site are bounded, at the most
    # a name can be: 2,048 characters of path and 253 of site.
    pytest.param({**AN_EVENT, "path": "/" + "a" * 2047}, id="a path of 2,048 characters"),
    pytest.param({**AN_EVENT, "site": "a" * 253}, id="a site of 253 characters"),
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
    pytest.param({**AN_EVENT, "path": "/" + "a" * 2048}, id="a path over 2,048 characters"),
    pytest.param({**AN_EVENT, "site": "a" * 254}, id="a site over 253 characters"),
    pytest.param({**AN_EVENT, "path": 7}, id="path of another type"),
    pytest.param({**AN_EVENT, "target": "1a2b3c4d5"}, id="target of the wrong length"),
    pytest.param({**AN_EVENT, "target": "1G2B3C4D"}, id="a character that is no hexadecimal digit"),
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


# ------------------------------------------------------- the action of an event ---


def click(**changes: Any) -> dict[str, Any]:
    """An event that says a button was clicked, with its action changed as asked."""
    action = {"type": "click", "tag": "button", "fingerprint": 'button[data-testid="save"]'}
    return {**AN_EVENT, "kind": "click", "action": {**action, **changes}}


def typed(**changes: Any) -> dict[str, Any]:
    """An event that says a text field was typed into, with its action changed as asked."""
    action = {
        "type": "input",
        "tag": "input",
        "fingerprint": 'input[name="query"]',
        "field": "text",
    }
    return {**AN_EVENT, "kind": "input", "action": {**action, **changes}}


def without(instance: dict[str, Any], name: str) -> dict[str, Any]:
    """The event without one member of its action."""
    return {**instance, "action": {k: v for k, v in instance["action"].items() if k != name}}


FIELDS = (
    "password",
    "one-time-code",
    "card",
    "email",
    "search",
    "tel",
    "url",
    "number",
    "date",
    "checkbox",
    "radio",
    "file",
    "other",
)

ACCEPTED_ACTIONS = [
    pytest.param(click(), id="click"),
    pytest.param(click(tag="x-" + "a" * 98), id="a tag of 100 characters"),
    pytest.param(
        {
            **AN_EVENT,
            "kind": "submit",
            "action": {"type": "submit", "tag": "form", "fingerprint": "form:nth-child(2)"},
        },
        id="submit",
    ),
    pytest.param(
        {
            **AN_EVENT,
            "kind": "select",
            "action": {"type": "select", "tag": "select", "fingerprint": 'select[id="country"]'},
        },
        id="select",
    ),
    pytest.param(typed(), id="input, text"),
    *(pytest.param(typed(field=field), id=f"input, {field}") for field in FIELDS),
    pytest.param(
        typed(tag="textarea", fingerprint="textarea:nth-child(1)"),
        id="input on a textarea, which has a type of its own",
    ),
    # A custom element's name always holds a hyphen, and a click often lands on one.
    pytest.param(
        click(tag="my-button", fingerprint="my-button:nth-child(3)"), id="tag of a custom element"
    ),
    pytest.param(click(tag="x1"), id="tag of one letter and a digit"),
    pytest.param(click(tag="a.b_c-d"), id="tag with a dot and an underscore"),
    pytest.param(click(fingerprint="x"), id="fingerprint of one character"),
    pytest.param(click(fingerprint="x" * 200), id="fingerprint of 200 characters"),
    pytest.param(click(fingerprint='input[aria-label="Sök"]'), id="fingerprint outside ASCII"),
    pytest.param(click(fingerprint='button[id="a b"]'), id="fingerprint with a space"),
]

REFUSED_ACTIONS = [
    pytest.param({**click(), "action": "click"}, id="action that is not an object"),
    pytest.param(click(tag="x-" + "a" * 99), id="a tag over 100 characters"),
    pytest.param({**click(), "action": ["click"]}, id="action that is a list"),
    pytest.param({**click(), "action": None}, id="action that is null"),
    pytest.param(click(type="navigate"), id="type outside the four: navigate"),
    pytest.param(
        {
            **click(),
            "kind": "navigate",
            "action": {"type": "navigate", "tag": "button", "fingerprint": "x"},
        },
        id="a navigate whose action says navigate, as the logger's word would",
    ),
    pytest.param(click(type="hover"), id="type outside the four: hover"),
    pytest.param(click(type="submit"), id="type says submit where the kind says click"),
    pytest.param(click(type="Click"), id="type in upper case"),
    pytest.param(click(type="input "), id="type ending in a space"),
    pytest.param(click(type=1), id="type of another type"),
    pytest.param(without(click(), "type"), id="type missing"),
    pytest.param(click(tag="Input"), id="tag with an upper-case letter"),
    pytest.param(click(tag="myButton"), id="tag in camel case, as an SVG element is named"),
    pytest.param(click(tag="-button"), id="tag starting with a hyphen"),
    pytest.param(click(tag="1button"), id="tag starting with a digit"),
    pytest.param(click(tag="my button"), id="tag with a space"),
    pytest.param(click(tag="button\n"), id="tag ending in a newline"),
    pytest.param(click(tag=""), id="tag empty"),
    pytest.param(click(tag=7), id="tag of another type"),
    pytest.param(click(fingerprint=""), id="fingerprint empty"),
    pytest.param(click(fingerprint="x" * 201), id="fingerprint of 201 characters"),
    pytest.param(click(fingerprint=7), id="fingerprint of another type"),
    pytest.param(without(click(), "fingerprint"), id="fingerprint missing"),
    pytest.param(typed(field="color"), id="field outside its list"),
    pytest.param(typed(field="Text"), id="field in upper case"),
    pytest.param(typed(field=7), id="field of another type"),
    pytest.param(click(field="text"), id="field on a click"),
    pytest.param(click(field=None), id="a field of null on a click"),
    pytest.param(
        {
            **AN_EVENT,
            "kind": "submit",
            "action": {
                "type": "submit",
                "tag": "form",
                "fingerprint": "form:nth-child(2)",
                "field": "text",
            },
        },
        id="field on a submit",
    ),
    pytest.param(
        {
            **AN_EVENT,
            "kind": "select",
            "action": {
                "type": "select",
                "tag": "select",
                "fingerprint": 'select[id="country"]',
                "field": "other",
            },
        },
        id="field on a select",
    ),
    pytest.param(without(typed(), "field"), id="input without a field"),
    pytest.param(typed(field=None), id="input whose field is null"),
    pytest.param(click(type="input", field="text"), id="type says input where the kind says click"),
    # No field here, so that only the rule between the kind and the type refuses it.
    pytest.param(
        {
            **AN_EVENT,
            "kind": "input",
            "action": {"type": "click", "tag": "button", "fingerprint": "x"},
        },
        id="type says click where the kind says input",
    ),
    pytest.param({**click(), "kind": "navigate"}, id="action on a navigate"),
    pytest.param({**click(), "kind": "focus", "target": None}, id="action on a focus"),
    pytest.param(
        click(html="<button>Save</button>"),
        id="a member the action does not know",
    ),
]


@pytest.mark.parametrize("instance", ACCEPTED_ACTIONS)
def test_the_schema_and_the_reader_agree_on_every_accepted_action(instance: object) -> None:
    assert accepted(ACTIVITY_EVENT, instance)

    event = activity.Event.parse(instance)

    assert event.as_dict() == instance


@pytest.mark.parametrize("instance", REFUSED_ACTIONS)
def test_the_schema_and_the_reader_agree_on_every_refused_action(instance: object) -> None:
    assert not accepted(ACTIVITY_EVENT, instance)

    with pytest.raises(ValueError, match="unexpected shape"):
        activity.Event.parse(instance)


def test_an_action_is_returned_as_it_was_read() -> None:
    """Its members come in the schema's order, and a field that is not there is not written."""
    read, fieldless = activity.Event.parse(typed()), activity.Event.parse(click())

    assert list(read.as_dict()) == ["v", "ts", "kind", "site", "path", "target", "action"]
    assert read.as_dict()["action"] == typed()["action"]
    assert fieldless.as_dict()["action"] == click()["action"]


def test_the_reader_holds_the_action_s_own_words_and_patterns() -> None:
    """The reader searches with the patterns the schema writes, and with its lists of words."""
    action = ACTIVITY_EVENT["properties"]["action"]

    assert list(activity.ACTION_TYPES) == action["properties"]["type"]["enum"]
    assert list(activity.FIELD_KINDS) == action["properties"]["field"]["enum"]
    assert activity.TAG_SHAPE.pattern == action["properties"]["tag"]["pattern"]
    assert action["properties"]["fingerprint"]["maxLength"] == activity.FINGERPRINT_AT_MOST
    assert list(activity.ACTION_REQUIRED) == action["required"]


def test_the_rules_between_two_fields_are_written_where_the_fields_are() -> None:
    """An instance shows only that the two agree on that instance. The rules themselves are
    in the schema, where what crosses the boundary is written down once.

    Five compare the kind of an event with its action, so they are the event's. Two compare
    the type of an action with its field, so they are the action's, and say so only for an
    object: an action that is not one is refused by its type and by nothing else."""
    event_rules = ACTIVITY_EVENT["allOf"]
    action_rules = ACTIVITY_EVENT["properties"]["action"]["allOf"]

    assert len(event_rules) == 5
    assert len(action_rules) == 2
    for rule in (*event_rules, *action_rules):
        assert set(rule) == {"if", "then"}
    for rule in action_rules:
        assert rule["if"]["type"] == "object"


# --------------------------------------------------------------- the capture's status ---


A_STATUS = {
    "pid": 4242,
    "state": "recording",
    "reason": None,
    "since": "2026-11-25T21:36:39",
    "events": 12,
    "last_event": "2026-11-25T21:40:03",
}

ACCEPTED_STATUSES = [
    pytest.param(A_STATUS, id="recording"),
    pytest.param(
        {**A_STATUS, "state": "connected", "events": 0, "last_event": None}, id="connected"
    ),
    pytest.param({**A_STATUS, "state": "stopped"}, id="stopped"),
    pytest.param(
        {**A_STATUS, "state": "failed", "reason": "OSError: nothing listens on 9"}, id="failed"
    ),
    pytest.param(
        {**A_STATUS, "state": "down", "reason": "Chrome publishes no address for this user"},
        id="down, with its reason",
    ),
    pytest.param(
        {**A_STATUS, "state": "a state a newer capture writes"},
        id="a state this reader does not know: shown as it is",
    ),
    pytest.param(
        {**A_STATUS, "a field a newer capture adds": 1},
        id="a field this reader does not know: left alone",
    ),
    # JSON has one kind of number, so 4242.0 is 4242.
    pytest.param({**A_STATUS, "pid": 4242.0, "events": 12.0}, id="whole numbers written as floats"),
]

REFUSED_STATUSES = [
    pytest.param({}, id="nothing"),
    pytest.param(
        {name: value for name, value in A_STATUS.items() if name != "since"}, id="no since"
    ),
    pytest.param({**A_STATUS, "pid": 0}, id="no process"),
    pytest.param({**A_STATUS, "pid": -1}, id="a negative process"),
    pytest.param({**A_STATUS, "pid": True}, id="true is no process"),
    pytest.param({**A_STATUS, "pid": "4242"}, id="a process as text"),
    pytest.param({**A_STATUS, "pid": 4242.5}, id="a process with a fraction"),
    pytest.param({**A_STATUS, "state": None}, id="no state"),
    pytest.param({**A_STATUS, "state": 7}, id="a state that is not text"),
    pytest.param({**A_STATUS, "reason": 7}, id="a reason that is not text"),
    pytest.param({**A_STATUS, "state": "down", "reason": None}, id="down with no reason"),
    pytest.param({**A_STATUS, "events": -1}, id="a negative count"),
    pytest.param({**A_STATUS, "events": True}, id="a count that is true"),
    pytest.param({**A_STATUS, "events": 12.5}, id="a count with a fraction"),
    pytest.param({**A_STATUS, "last_event": "2026-11-25"}, id="a last event of no time shape"),
    pytest.param({**A_STATUS, "last_event": 7}, id="a last event that is not text"),
    pytest.param({**A_STATUS, "since": "yesterday"}, id="a since that is no time"),
    pytest.param([A_STATUS], id="a list"),
]


def written_status(tmp_path: Path, instance: object) -> None:
    (tmp_path / "watch").mkdir()
    (tmp_path / "watch" / capture.STATUS_FILE).write_text(json.dumps(instance), encoding="utf-8")


@pytest.mark.parametrize("instance", ACCEPTED_STATUSES)
def test_the_schema_and_the_reader_agree_on_every_accepted_status(
    instance: object, tmp_path: Path
) -> None:
    assert accepted(CAPTURE_STATUS, instance)

    written_status(tmp_path, instance)

    assert capture._report(tmp_path) == instance


@pytest.mark.parametrize("instance", REFUSED_STATUSES)
def test_the_schema_and_the_reader_agree_on_every_refused_status(
    instance: object, tmp_path: Path
) -> None:
    assert not accepted(CAPTURE_STATUS, instance)

    written_status(tmp_path, instance)

    assert capture._report(tmp_path) is None


def test_the_rule_between_two_fields_of_the_status_is_written_in_the_schema() -> None:
    """An instance shows only that the two agree on that instance. A capture that is down
    says why: the rule is in the schema, with if and then, once."""
    rules = CAPTURE_STATUS["allOf"]

    assert rules == [
        {
            "if": {"properties": {"state": {"const": "down"}}, "required": ["state"]},
            "then": {"properties": {"reason": {"type": "string"}}},
        }
    ]


def test_the_times_of_the_status_are_the_times_of_an_event() -> None:
    """One shape of a time, wherever it crosses a boundary."""
    said = CAPTURE_STATUS["properties"]

    assert said["since"]["pattern"] == ACTIVITY_EVENT["properties"]["ts"]["pattern"]
    assert (
        said["last_event"]["anyOf"][1]["pattern"] == ACTIVITY_EVENT["properties"]["ts"]["pattern"]
    )
