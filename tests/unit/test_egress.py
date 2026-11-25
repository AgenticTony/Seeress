"""Judging the record of what Seeress's processes contacted.

One test per rule. A rule that cannot fail on a made-up record would be of no use on a
real one.

A record is complete when every process Seeress started is also seen starting: Seeress's
own process, and the browser tool's process for each time the tool was run.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from seeress import egress, egress_hook, env

PORT = 54321
SEERESS = "python3.13 -m seeress.cli --profile"
TOOL = "python3.13 browser-use"
LOG = egress_hook.LOG
OFF = {
    "ANONYMIZED_TELEMETRY": "false",
    "BROWSER_USE_NO_UPDATE_CHECK": "1",
    "BH_UPDATE_CHECK": "0",
}
TOOL_ENV = (
    "PATH",
    "HOME",
    LOG,
    "ANONYMIZED_TELEMETRY",
    "BROWSER_USE_NO_UPDATE_CHECK",
    "BH_UPDATE_CHECK",
    "BH_HOME",
    "BU_NAME",
    "BU_CDP_WS",
)
TOOL_SWITCHES = {
    **OFF,
    "BU_NAME": "seeress-client",
    "BU_CDP_WS": "ws://127.0.0.1:1/x",
    "BH_HOME": "/h",
}


def event(
    kind: str,
    host: str | None = None,
    port: int | None = None,
    role: str = SEERESS,
    pid: int = 10,
    parent: int = 1,
    program: str | None = None,
    env: tuple[str, ...] | None = None,
    switches: dict[str, str] | None = None,
    origin: str | None = None,
) -> egress.Event:
    return egress.Event(pid, parent, role, kind, host, port, origin, program, env, switches)


def tool_started(
    env: tuple[str, ...] = TOOL_ENV, switches: dict[str, str] | None = None
) -> egress.Event:
    """Seeress's own process started the browser tool and gave it this environment."""
    return event(
        "spawn",
        program="browser-use",
        env=env,
        switches=TOOL_SWITCHES if switches is None else switches,
    )


def tool_seen(pid: int = 11, role: str = TOOL, by: int = 10) -> egress.Event:
    """The browser tool's process, seen starting as a child of the process that started it."""
    return event("start", role=role, pid=pid, parent=by)


def clean_record(tool: egress.Event | None = None) -> list[egress.Event]:
    """A complete record of a clean run: nothing contacted but this machine."""
    return [
        event("start"),
        event("connect", "127.0.0.1", PORT),
        event("resolve", "127.0.0.1", PORT),
        tool_started() if tool is None else tool,
        tool_seen(),
    ]


def kinds(findings: list[egress.Finding]) -> list[str]:
    return [finding.kind for finding in findings]


def details(findings: list[egress.Finding]) -> str:
    return " ".join(finding.detail for finding in findings)


# -------------------------------------------------------------- verdicts ---


def test_a_clean_record_has_no_findings() -> None:
    record = [
        *clean_record(),
        event("connect", "127.0.0.1", 52000, role=TOOL, pid=11, parent=10),
    ]

    assert egress.judge(record, endpoint_port=PORT) == []


@pytest.mark.parametrize("kind", ["connect", "resolve"])
@pytest.mark.parametrize(
    "host",
    ["eu.i.posthog.com", "pypi.org", "api.z.ai", "192.168.1.9", "seeress.invalid"],
)
def test_a_connection_to_any_host_but_loopback_is_a_finding(kind: str, host: str) -> None:
    """Whoever makes it: Seeress's own process, or a child of it."""
    for who, role, pid, parent in (("Seeress", SEERESS, 10, 1), ("the tool", TOOL, 11, 10)):
        record = [*clean_record(), event(kind, host, 443, role=role, pid=pid, parent=parent)]

        findings = egress.judge(record, endpoint_port=PORT)

        assert kinds(findings) == ["contact"], who
        assert host in details(findings)


def test_a_request_to_another_origin_is_a_finding() -> None:
    record = [*clean_record(), event("request", origin="https://pypi.org")]

    findings = egress.judge(record, endpoint_port=PORT)

    assert kinds(findings) == ["contact"]
    assert "pypi.org" in details(findings)


def test_a_socket_that_is_a_file_on_this_machine_is_not_a_contact() -> None:
    """The browser tool talks to its daemon through one. A real record was full of them."""
    record = [
        *clean_record(),
        event("connect", egress_hook.LOCAL_SOCKET, None, role=TOOL, pid=11, parent=10),
    ]

    assert egress.judge(record, endpoint_port=PORT) == []


def test_a_child_honouring_a_proxy_setting_is_a_finding() -> None:
    """The check points every proxy setting at a port where nothing listens."""
    record = [
        *clean_record(),
        event("connect", "127.0.0.1", egress.DEAD_PROXY_PORT, role=TOOL, pid=11, parent=10),
    ]

    assert kinds(egress.judge(record, endpoint_port=PORT)) == ["proxy"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ZAI_API_KEY", ["environment"]),
        ("HTTPS_PROXY", ["environment"]),
        # Outside the allow-list, and a switch Seeress never gives.
        ("BU_AUTOSPAWN", ["environment", "switch"]),
        ("PYTHONPATH", ["environment"]),
    ],
)
def test_the_browser_tool_given_a_planted_name_is_a_finding(name: str, expected: list[str]) -> None:
    findings = egress.judge(clean_record(tool_started(env=(*TOOL_ENV, name))), endpoint_port=PORT)

    assert kinds(findings) == expected
    assert all(name in finding.detail for finding in findings)


def test_a_planted_name_is_reported_once() -> None:
    """It is outside the allow-list as well as forbidden. Said twice, it said no more."""
    findings = egress.judge(
        clean_record(tool_started(env=(*TOOL_ENV, "ZAI_API_KEY"))), endpoint_port=PORT
    )

    assert [str(finding) for finding in findings] == [
        "environment: browser-use was given ZAI_API_KEY"
    ]


def test_a_child_given_something_outside_the_allow_list_is_a_finding() -> None:
    findings = egress.judge(
        clean_record(tool_started(env=(*TOOL_ENV, "LD_PRELOAD"))), endpoint_port=PORT
    )

    assert kinds(findings) == ["environment"]
    assert "LD_PRELOAD" in details(findings)


@pytest.mark.parametrize(
    ("switches", "told"),
    [
        ({**OFF, "ANONYMIZED_TELEMETRY": "true"}, "ANONYMIZED_TELEMETRY=true"),
        ({**OFF, "BROWSER_USE_NO_UPDATE_CHECK": "0"}, "BROWSER_USE_NO_UPDATE_CHECK=0"),
        ({**OFF, "BH_UPDATE_CHECK": "1"}, "BH_UPDATE_CHECK=1"),
        # A switch left out is the library's own default, which is on.
        (
            {name: value for name, value in OFF.items() if name != "BH_UPDATE_CHECK"},
            "BH_UPDATE_CHECK=nothing",
        ),
    ],
)
def test_the_browser_tool_not_told_to_stay_quiet_is_a_finding(
    switches: dict[str, str], told: str
) -> None:
    findings = egress.judge(clean_record(tool_started(switches=switches)), endpoint_port=PORT)

    assert kinds(findings) == ["switch"]
    assert told in details(findings)


def test_the_library_s_daemon_is_held_to_the_same_switches_as_the_tool() -> None:
    """It is where the browser is driven from, and the library's own code runs there too."""
    daemon = event(
        "spawn",
        program="python3.13 -m browser_harness.daemon",
        env=TOOL_ENV,
        switches={**OFF, "ANONYMIZED_TELEMETRY": "true"},
        role=TOOL,
        pid=11,
        parent=10,
    )

    findings = egress.judge([*clean_record(), daemon], endpoint_port=PORT)

    assert kinds(findings) == ["switch"]
    assert "browser_harness.daemon was told ANONYMIZED_TELEMETRY=true" in details(findings)


def test_what_the_library_adds_for_its_own_daemon_is_allowed_there() -> None:
    """Taken from a real record: how it names itself to its telemetry, and what macOS adds."""
    daemon = event(
        "spawn",
        program="python3.13 -m browser_harness.daemon",
        env=(*TOOL_ENV, *sorted(egress.LIBRARY_OWN)),
        switches=OFF,
        role=TOOL,
        pid=11,
        parent=10,
    )

    assert egress.judge([*clean_record(), daemon], endpoint_port=PORT) == []


@pytest.mark.parametrize("name", sorted(egress.LIBRARY_OWN))
def test_seeress_may_not_give_a_child_what_the_library_adds_for_itself(name: str) -> None:
    findings = egress.judge(clean_record(tool_started(env=(*TOOL_ENV, name))), endpoint_port=PORT)

    assert kinds(findings) == ["environment"]
    assert name in details(findings)


def test_anything_else_the_library_hands_down_is_a_finding() -> None:
    """A new release of the library may add a name. The first real run then says so."""
    daemon = event(
        "spawn",
        program="python3.13 -m browser_harness.daemon",
        env=(*TOOL_ENV, "BU_CDP_URL", "BH_TAB_MARKER"),
        switches=OFF,
        role=TOOL,
        pid=11,
        parent=10,
    )

    findings = egress.judge([*clean_record(), daemon], endpoint_port=PORT)

    assert kinds(findings) == ["environment", "environment"]
    assert "BU_CDP_URL" in details(findings)
    assert "BH_TAB_MARKER" in details(findings)


def test_a_child_started_without_the_recording_switch_cannot_be_seen() -> None:
    """Whoever starts it: had it been Python, nothing it did would be on the record."""
    daemon = event(
        "spawn",
        program="python3.13 -m browser_harness.daemon",
        env=("PATH", "HOME"),
        switches=OFF,
        role=TOOL,
        pid=11,
        parent=10,
    )

    findings = egress.judge([*clean_record(), daemon], endpoint_port=PORT)

    assert kinds(findings) == ["instrument"]
    assert "without the recording switch" in details(findings)


def test_a_child_the_browser_tool_started_with_the_switch_is_on_record_when_it_runs() -> None:
    """The tool hands its children its environment; one of them records what it does."""
    helper = event(
        "spawn",
        program="python3.13 -m browser_harness.something",
        env=TOOL_ENV,
        switches=OFF,
        role=TOOL,
        pid=11,
        parent=10,
    )
    seen = event("start", role="python3.13 -m browser_harness.something", pid=12, parent=11)

    assert egress.judge([*clean_record(), helper, seen], endpoint_port=PORT) == []


def test_the_browser_seeress_starts_is_held_to_its_own_allow_list() -> None:
    browser = "Google Chrome --user-data-dir"
    plain = event("spawn", program=browser, env=("PATH", "HOME"), switches={})
    with_a_switch = event(
        "spawn", program=browser, env=("PATH", "HOME", "ANONYMIZED_TELEMETRY"), switches={}
    )

    assert egress.judge([*clean_record(), plain], endpoint_port=PORT) == []
    assert kinds(egress.judge([*clean_record(), with_a_switch], endpoint_port=PORT)) == [
        "environment"
    ]


def test_a_child_that_inherits_its_environment_is_not_judged_twice() -> None:
    """Its parent's environment was judged when that was given."""
    daemon = event(
        "spawn",
        program="python3.13 -m browser_harness.daemon",
        env=None,
        role=TOOL,
        pid=11,
        parent=10,
    )

    assert egress.judge([*clean_record(), daemon], endpoint_port=PORT) == []


@pytest.mark.parametrize("loopback", ["::1", "localhost"])
def test_other_loopback_addresses_count_as_this_machine(loopback: str) -> None:
    record = [
        event("start"),
        event("connect", loopback, PORT),
        event("resolve", loopback, PORT),
        tool_started(),
        tool_seen(),
    ]

    assert egress.judge(record, endpoint_port=PORT) == []


def test_a_resolve_without_a_host_is_ignored() -> None:
    assert egress.judge([*clean_record(), event("resolve", None, PORT)], endpoint_port=PORT) == []


# ------------------------------------------------- a record with a hole ---


def test_an_empty_record_proves_nothing() -> None:
    assert kinds(egress.judge([], endpoint_port=PORT)) == ["instrument"]


def test_a_run_that_never_reached_its_model_proves_nothing() -> None:
    record = [event("start"), event("connect", "127.0.0.1", 9999), tool_started(), tool_seen()]

    assert kinds(egress.judge(record, endpoint_port=PORT)) == ["instrument"]


def test_a_run_in_which_no_browser_step_ran_proves_nothing_about_the_browser_tool() -> None:
    record = [event("start"), event("connect", "127.0.0.1", PORT)]

    findings = egress.judge(record, endpoint_port=PORT)

    assert "the browser tool was never started" in details(findings)


def test_the_browser_tool_started_and_never_seen_leaves_a_hole() -> None:
    """This happened: the recording switch was left off the allow-list for two runs."""
    record = [event("start"), event("connect", "127.0.0.1", PORT), tool_started()]

    findings = egress.judge(record, endpoint_port=PORT)

    assert kinds(findings) == ["instrument"]
    assert "record has a hole" in details(findings)


def test_the_tool_must_be_seen_as_many_times_as_it_was_started() -> None:
    findings = egress.judge([*clean_record(), tool_started()], endpoint_port=PORT)

    assert kinds(findings) == ["instrument"]
    assert "started 2 time(s); 1 of its processes were observed" in details(findings)


def test_the_tool_counts_as_seen_only_when_seeress_s_own_process_is_its_parent() -> None:
    record = [event("start"), event("connect", "127.0.0.1", PORT), tool_started(), tool_seen(by=99)]

    assert kinds(egress.judge(record, endpoint_port=PORT)) == ["instrument"]


def test_a_process_that_is_not_the_browser_tool_does_not_stand_in_for_it() -> None:
    record = [
        event("start"),
        event("connect", "127.0.0.1", PORT),
        tool_started(),
        tool_seen(role="python3.13 -m something.else"),
    ]

    assert kinds(egress.judge(record, endpoint_port=PORT)) == ["instrument"]


@pytest.mark.parametrize(
    ("pid", "who"),
    [(99, TOOL), (10, TOOL)],
    ids=["started by another process", "a later process with Seeress's number"],
)
def test_the_browser_tool_counts_as_started_only_by_seeress_s_own_process(
    pid: int, who: str
) -> None:
    """A process is known by what it was started as, not by its number alone."""
    elsewhere = event("spawn", program="browser-use", env=TOOL_ENV, switches=OFF, role=who, pid=pid)
    record = [event("start"), event("connect", "127.0.0.1", PORT), elsewhere]

    findings = egress.judge(record, endpoint_port=PORT)

    assert "the browser tool was never started" in details(findings)


# --------------------------------------------------------------- the record ---


def test_events_are_read_back_as_they_were_written(tmp_path: Path) -> None:
    path = tmp_path / "egress.jsonl"
    path.write_text(
        json.dumps({"pid": 1, "parent": 0, "role": SEERESS, "event": "start"})
        + "\n{not json\n"
        + json.dumps(
            {
                "pid": 1,
                "parent": 0,
                "role": SEERESS,
                "event": "spawn",
                "program": "browser-use",
                "env": ["PATH", "HOME"],
                "switches": {"BU_NAME": "seeress-client"},
            }
        )
        + "\n"
        + json.dumps({"pid": 2, "parent": 0, "role": "x", "event": "resolve", "host": None})
        + "\n"
    )

    events = egress.read_events(path)

    assert events == [
        egress.Event(1, 0, SEERESS, "start"),
        egress.Event(
            1,
            0,
            SEERESS,
            "spawn",
            program="browser-use",
            env=("PATH", "HOME"),
            switches={"BU_NAME": "seeress-client"},
        ),
        egress.Event(2, 0, "x", "resolve"),
    ]


def test_contacts_are_counted_per_destination_and_a_start_is_not_a_contact() -> None:
    events = [
        event("start"),
        event("connect", "127.0.0.1", PORT),
        event("connect", "127.0.0.1", PORT),
        event("request", origin="https://pypi.org"),
        event("connect", egress_hook.LOCAL_SOCKET),
        event("resolve", "127.0.0.1", None),
        event("spawn", program="browser-use"),
    ]

    assert egress.contacts(events) == {
        "127.0.0.1:54321": 2,
        "https://pypi.org": 1,
        "a local socket": 1,
        "127.0.0.1": 1,
    }


# ----------------------------------------------------------------- the hook ---


def test_an_origin_holds_a_scheme_a_host_and_a_port_and_nothing_else() -> None:
    url = "https://user:secret@pypi.org:8443/pypi/browser-use/json?token=abc#frag"

    assert egress_hook.origin(url) == "https://pypi.org:8443"


def test_an_origin_is_none_for_something_that_is_not_a_url() -> None:
    assert egress_hook.origin("pypi.org/json") is None
    assert egress_hook.origin(None) is None
    assert egress_hook.origin(7) is None


def test_a_socket_address_is_split_into_host_and_port() -> None:
    assert egress_hook.endpoint(("127.0.0.1", 11434)) == ("127.0.0.1", 11434)
    assert egress_hook.endpoint(("example.com", "https")) == ("example.com", None)
    assert egress_hook.endpoint("/private/tmp/a.sock") == ("a local socket", None)
    assert egress_hook.endpoint(None) == (None, None)


def test_a_program_is_shown_by_name_and_never_by_what_follows_an_equals_sign() -> None:
    shown = egress_hook.program(
        "x",
        [
            "/Applications/Example.app/Contents/MacOS/Example",
            "--user-data-dir=/Users/someone/private",
            "--headless=new",
        ],
    )

    assert shown == "Example --user-data-dir --headless"


def test_nothing_after_a_c_option_is_shown() -> None:
    """It is code, and code can hold a value."""
    code = "import os; key = '/a/secret/value'; os.system(key)"

    assert egress_hook.program("x", ["/p/python", "-c", code]) == "python -c"
    assert egress_hook.program("x", ["/p/python", "-I", "-c", code, "more"]) == "python -I -c"


def test_a_shell_command_is_never_shown() -> None:
    assert egress_hook.program("sh", "curl https://user:secret@example.com") == "a shell command"


def test_a_role_names_the_module_and_one_option_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original command line is used: under -m, sys.argv starts with the module file."""
    monkeypatch.setattr(
        sys,
        "orig_argv",
        ["/Users/someone/.venv/bin/python", "-m", "seeress.cli", "--profile", "client", "a task"],
    )

    assert egress_hook.role() == "python -m seeress.cli --profile"


def test_a_role_stops_at_a_c_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", ["/p/python", "-c", "key = '/a/secret/value'"])

    assert egress_hook.role() == "python -c"


def test_the_hook_does_nothing_without_being_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(egress_hook.LOG, raising=False)
    added: list[object] = []
    monkeypatch.setattr(sys, "addaudithook", added.append)
    log = tmp_path / "egress.jsonl"

    egress_hook.install()

    assert added == []
    assert not log.exists()


def ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[object]]:
    """Ask for recording in a fresh process-like state, and count the hooks added."""
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv(egress_hook.LOG, str(log))
    monkeypatch.setattr(egress_hook, "_installed", False)
    added: list[object] = []
    monkeypatch.setattr(sys, "addaudithook", added.append)
    return log, added


def test_the_hook_is_installed_once_however_often_it_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start-up file that loads the hook is run more than once in some Python builds."""
    log, added = ready(tmp_path, monkeypatch)

    egress_hook.install()
    egress_hook.install()

    assert len(added) == 1
    assert len(egress.read_events(log)) == 1


def test_the_first_thing_a_process_records_is_that_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _ = ready(tmp_path, monkeypatch)

    egress_hook.install()

    (start,) = egress.read_events(log)
    assert start == egress.Event(os.getpid(), os.getppid(), egress_hook.role(), "start")


def test_the_recording_switch_reaches_the_children_seeress_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It was once left off the allow-list, and the browser tool's processes were silent."""
    expected = "/a/log"
    monkeypatch.setenv(egress_hook.LOG, expected)

    assert env.child_env("client", base=os.environ)[egress_hook.LOG] == expected
    assert env.child_env("personal", base=os.environ)[egress_hook.LOG] == expected


# ---------------------------------------------------------- a capture, not a task ---

CAPTURE = "python3.13 -m seeress.capture ws:"


def capture_record(port: int = PORT) -> list[egress.Event]:
    """A capture's record: its process exists, and it connected to the browser it was given."""
    return [event("start", role=CAPTURE), event("connect", "127.0.0.1", port, role=CAPTURE)]


def judged_as_a_capture(record: list[egress.Event]) -> list[egress.Finding]:
    return egress.judge(
        record, endpoint_port=PORT, seeress_process="seeress.capture", starts_browser_tool=False
    )


def test_a_capture_that_reached_its_browser_and_started_nothing_is_clean() -> None:
    """A capture asks no model and starts no browser tool. It connects to the browser it
    was given, and that is all it need have done."""

    assert judged_as_a_capture(capture_record()) == []


def test_a_capture_that_never_reached_its_browser_proves_nothing() -> None:
    assert kinds(judged_as_a_capture(capture_record(port=9999))) == ["instrument"]


def test_a_capture_that_contacted_another_machine_is_a_finding() -> None:
    record = [*capture_record(), event("connect", "93.184.216.34", 443, role=CAPTURE)]

    assert kinds(judged_as_a_capture(record)) == ["contact"]


def test_a_capture_whose_process_started_the_browser_tool_is_no_capture() -> None:
    """The capture has nothing to do with the browser tool. If its process started it,
    something else ran in its place, and that is said."""
    record = [
        *capture_record(),
        event("spawn", program="browser-use", env=TOOL_ENV, switches=TOOL_SWITCHES, role=CAPTURE),
        tool_seen(),
    ]

    assert kinds(judged_as_a_capture(record)) == ["task"]


def test_a_task_is_still_asked_for_the_browser_tool_it_started() -> None:
    """The default is a task: the browser tool must have been started, and seen."""
    record = [event("start"), event("connect", "127.0.0.1", PORT)]

    assert "the browser tool was never started" in details(egress.judge(record, endpoint_port=PORT))
