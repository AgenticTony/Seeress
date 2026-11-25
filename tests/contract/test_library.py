"""What Seeress relies on in the installed browser-use library, pinned to its source.

Seeress's controls are written against one version of the library. An upgrade that changes
any of this fails here first, instead of failing somewhere that matters: a renamed switch
would silently turn the library's telemetry on again.
"""

import importlib.metadata
import re
from pathlib import Path

import browser_harness
import browser_use
import pytest

from seeress import bodies

HARNESS = Path(browser_harness.__file__).parent
USE = Path(browser_use.__file__).parent


def source(root: Path, relative: str | Path) -> str:
    return (root / relative).read_text(encoding="utf-8")


def test_the_versions_are_the_ones_that_were_read() -> None:
    assert importlib.metadata.version("browser-use") == "1.0.6"
    assert importlib.metadata.version("browser-harness") == "0.1.13"


def reads_environment(text: str, name: str) -> bool:
    """True when the source reads `name` from the environment in any of its usual forms."""
    quoted = f"[\"']{name}[\"']"
    return (
        re.search(
            rf"(os\.environ\.get|os\.getenv)\(\s*{quoted}|os\.environ\[\s*{quoted}\s*\]"
            rf"|DISABLE_ENVS = \([^)]*{quoted}",
            text,
        )
        is not None
    )


@pytest.mark.parametrize(
    ("root", "file", "name"),
    [
        (HARNESS, "telemetry.py", "ANONYMIZED_TELEMETRY"),
        (USE, "config.py", "ANONYMIZED_TELEMETRY"),
        (USE, "update_check.py", "BROWSER_USE_NO_UPDATE_CHECK"),
        (HARNESS, "admin.py", "BH_UPDATE_CHECK"),
        (HARNESS, "paths.py", "BH_HOME"),
        (HARNESS, "paths.py", "BH_AGENT_WORKSPACE"),
        (HARNESS, "run.py", "BU_AUTOSPAWN"),
        (HARNESS, "daemon.py", "BU_CDP_WS"),
        (HARNESS, "admin.py", "BU_NAME"),
        (HARNESS, "admin.py", "BU_CDP_URL"),
    ],
)
def test_the_library_reads_each_switch_seeress_sets(root: Path, file: str, name: str) -> None:
    assert reads_environment(source(root, file), name), f"{file} no longer reads {name}"


def test_the_update_checks_are_off_for_the_values_seeress_forces() -> None:
    update_check = source(USE, "update_check.py")
    admin = source(HARNESS, "admin.py")

    assert "BROWSER_USE_NO_UPDATE_CHECK" in update_check
    assert "('1', 'true', 'yes')" in update_check
    assert "BH_UPDATE_CHECK" in admin or '"BH_UPDATE_CHECK"' in admin
    assert '{"0", "false", "no", "off"}' in admin


def test_the_socket_path_limits_are_the_ones_seeress_checks() -> None:
    text = source(USE, Path("browser") / "browser.py")

    assert "limit = 103 if sys.platform == 'darwin' else 107" in text


def test_the_raw_protocol_call_reports_an_error_and_an_empty_result_differently() -> None:
    """Why the door probe treats an empty answer as an error: a missing key answers {}."""
    text = source(HARNESS, "helpers.py")

    assert 'if "error" in r: raise RuntimeError(r["error"])' in text
    assert '.get("result", {})' in text


@pytest.mark.parametrize("headless", [True, False])
def test_seeress_starts_a_browser_with_the_tool_s_flags_and_then_its_own(headless: bool) -> None:
    """Seeress never loads the tool into its own process, so it keeps a copy of its flags.

    They come first, unchanged. Between them and the start page comes what is Seeress's
    own: the switches that quiet the background services, and the route.
    """
    from browser_use.browser import local

    profile = Path("/a/profile/of/seeress")
    theirs = list(local.chrome_args(profile, headless=headless, fresh=True))

    ours = bodies.browser_arguments(profile, headless=headless, allowed_hosts=("corp.example.com",))

    assert ours[: len(theirs) - 1] == theirs[:-1]
    assert ours[-1] == theirs[-1]
    assert set(ours[len(theirs) - 1 : -1]) >= {
        *bodies.QUIET,
        "--proxy-bypass-list=corp.example.com;*.corp.example.com",
    }


def test_the_tool_looks_for_a_browser_where_seeress_does() -> None:
    text = source(USE, Path("browser") / "local.py")

    # the same applications, in the same order
    assert "['Google Chrome', 'Chromium', 'Microsoft Edge', 'Google Chrome Canary']" in text
    assert "'/Applications'" in text
    assert "'--use-mock-keychain'" in text
    assert "start_new_session=sys.platform != 'win32'" in text


def test_the_library_limits_the_socket_path_where_seeress_expects() -> None:
    text = source(HARNESS, "_ipc.py")

    assert 'def _sock_path(name): return _RUNTIME / f"{_runtime_stem(name)}.sock"' in text


def test_the_library_reads_the_cloud_key_from_the_environment() -> None:
    """The key is not passed to children, which is why no child can start a cloud browser."""
    text = source(HARNESS, "helpers.py")

    assert 'os.environ.get("BROWSER_USE_API_KEY")' in text


def test_telemetry_is_off_when_the_switch_says_so() -> None:
    """The value Seeress forces is one the library treats as off."""
    text = source(HARNESS, "telemetry.py")

    assert '"ANONYMIZED_TELEMETRY"' in text
    assert '{"0", "false", "no", "off"}' in text


def test_a_daemon_reports_only_its_kind_never_its_browser() -> None:
    """Why door isolation is checked through a marker in the browser instead."""
    text = source(HARNESS, "daemon.py")

    assert '"browser_kind": BROWSER_KIND' in text
    assert '"ws_url"' not in text


def test_the_library_names_its_own_chromium_daemon_after_the_door() -> None:
    text = source(USE, "cli.py")

    assert "'BU_NAME'" in text or '"BU_NAME"' in text
    assert "-chromium" in text


def test_model_code_runs_in_the_namespace_of_the_tool() -> None:
    """Why model code passes a name allow-list before it runs (task B5)."""
    text = source(HARNESS, "run.py")

    assert "    exec(code, globals())" in text
    assert "    start_remote_daemon," in text
    assert "    run_update," in text
