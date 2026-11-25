"""The environment every child process gets.

The browser tool and the daemon it starts read their settings from the environment, so an
inherited environment could hand them a cloud key, a proxy, or a switch that turns their
telemetry on. Seeress builds the child's environment from scratch.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from seeress import env
from seeress.errors import ConfigError

FORCED_NAMES = set(env.FORCED)


def everything_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in env.PASSTHROUGH:
        monkeypatch.delenv(name, raising=False)


def test_the_environment_is_built_from_the_allow_list_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    everything_unset(monkeypatch)
    for name in ("PATH", "HOME", "TMPDIR", "LANG", "USER"):
        monkeypatch.setenv(name, f"{name}-value")
    for name in (
        "SOME_SECRET",
        "ZAI_API_KEY",
        "BROWSER_USE_API_KEY",
        "BU_AUTOSPAWN",
        "BU_BROWSER_ID",
        "BU_CDP_URL",
        "BU_NAME",
        "BU_CDP_WS",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "BH_TELEMETRY",
        "ANONYMIZED_TELEMETRY",
    ):
        monkeypatch.setenv(name, "inherited")

    child = env.child_env("personal")

    assert set(child) == {"PATH", "HOME", "TMPDIR", "LANG", "USER", "BH_HOME"} | FORCED_NAMES
    assert child["PATH"] == "PATH-value"
    assert child["HOME"] == "HOME-value"


def test_the_library_switches_are_forced_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "true")
    monkeypatch.setenv("BROWSER_USE_NO_UPDATE_CHECK", "0")
    monkeypatch.setenv("BH_UPDATE_CHECK", "1")

    child = env.child_env("personal")

    assert child["ANONYMIZED_TELEMETRY"] == "false"
    assert child["BROWSER_USE_NO_UPDATE_CHECK"] == "1"
    assert child["BH_UPDATE_CHECK"] == "0"


def test_every_switch_the_library_reads_can_be_switched_off_by_this_environment() -> None:
    """The library is checked against these names in tests/contract/test_library.py."""
    assert env.FORCED["ANONYMIZED_TELEMETRY"] == "false"
    assert env.FORCED["BROWSER_USE_NO_UPDATE_CHECK"] == "1"
    assert env.FORCED["BH_UPDATE_CHECK"] == "0"


def test_the_library_keeps_its_files_inside_the_seeress_home(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setenv("BH_HOME", "/somewhere/else")
    monkeypatch.setenv("BH_CONFIG_DIR", "/elsewhere")
    monkeypatch.setenv("BH_RUNTIME_DIR", "/elsewhere")
    monkeypatch.setenv("BH_TMP_DIR", "/elsewhere")
    monkeypatch.setenv("BH_AGENT_WORKSPACE", "/elsewhere")

    child = env.child_env("personal")

    assert child["BH_HOME"] == str(home / ".seeress" / "browser-harness")
    assert not {"BH_CONFIG_DIR", "BH_RUNTIME_DIR", "BH_TMP_DIR", "BH_AGENT_WORKSPACE"} & set(child)


def test_the_seeress_home_setting_moves_the_library_home_too(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = home / "moved"
    moved.mkdir()
    monkeypatch.setenv("SEERESS_HOME", str(moved))

    assert env.child_env("personal", base={})["BH_HOME"] == str(moved / "browser-harness")


def test_the_personal_door_passes_the_browser_address_it_was_given(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:1/inherited")
    monkeypatch.delenv("BU_NAME", raising=False)

    explicit = env.child_env("personal", "ws://127.0.0.1:9222/devtools/browser/abc")

    assert explicit["BU_CDP_WS"] == "ws://127.0.0.1:9222/devtools/browser/abc"
    assert "BU_NAME" not in explicit
    assert "BU_CDP_WS" not in env.child_env("personal")


def test_the_client_door_names_its_own_daemon_so_no_other_door_is_reused(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setenv("BU_NAME", "inherited")

    assert env.child_env("client")["BU_NAME"] == "seeress-client"
    assert "BU_NAME" not in env.child_env("personal")


@pytest.mark.parametrize("name", env.DENIED_EXAMPLES)
def test_a_setting_that_must_not_reach_a_child_never_does(
    monkeypatch: pytest.MonkeyPatch, home: Path, name: str
) -> None:
    monkeypatch.setenv(name, "inherited")

    assert name not in env.child_env("client", base=os.environ)
    assert name not in env.child_env("personal", base=os.environ)
    assert name not in env.browser_env()


def test_a_variable_that_is_empty_is_left_out(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("LANG", "")

    assert "LANG" not in env.child_env("personal")


def test_the_default_home_leaves_room_for_the_daemon_s_socket(home: Path) -> None:
    env.child_env("client")
    env.child_env("personal")

    assert len(str(env.socket_path("seeress-client")).encode()) <= env.SOCKET_LIMIT


def test_a_home_too_deep_for_the_socket_is_refused_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the end-to-end tests: the library refuses a socket path over its limit."""
    deep = tmp_path / ("a-very-long-folder-name-" * 6)
    monkeypatch.setenv("SEERESS_HOME", str(deep))

    with pytest.raises(ConfigError, match="SEERESS_HOME is too deep"):
        env.child_env("client", base={})


def test_nothing_can_start_with_a_home_too_deep_even_for_the_personal_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEERESS_HOME", str(tmp_path / ("x" * 120)))

    with pytest.raises(ConfigError):
        env.child_env("personal", base={})


def test_a_browser_seeress_starts_gets_an_allow_listed_environment_too(home: Path) -> None:
    """A browser lives long, and has no use for a model key or a library switch."""
    base = {
        "PATH": "/usr/bin",
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "en_GB.UTF-8",
        "ZAI_API_KEY": "a-cloud-key",
        "BROWSER_USE_API_KEY": "a-library-key",
        "HTTPS_PROXY": "http://127.0.0.1:9/",
        "ANONYMIZED_TELEMETRY": "true",
        "BH_HOME": "/somewhere",
        "SEERESS_HOME": "/somewhere",
        "SEERESS_EGRESS_LOG": "/a/log",
        "PYTHONPATH": "/somewhere",
    }

    child = env.browser_env(base)

    assert child == {
        "PATH": "/usr/bin",
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "en_GB.UTF-8",
    }
    assert set(child) <= set(env.BROWSER_PASSTHROUGH)


def test_another_source_can_be_given_so_tests_need_no_environment_of_their_own(home: Path) -> None:
    base: dict[str, Any] = {"PATH": "/bin", "HOME": str(home), "ZAI_API_KEY": "k"}

    child = env.child_env("client", base=base)

    assert child["PATH"] == "/bin"
    assert "ZAI_API_KEY" not in child
