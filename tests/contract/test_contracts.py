"""Each schema agrees with the code that reads its message.

Whatever a schema accepts, the reader accepts, with the same fields. Whatever it refuses,
the reader refuses. A schema that drifts from its reader is a contract nobody can rely on.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from seeress import bodies

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
