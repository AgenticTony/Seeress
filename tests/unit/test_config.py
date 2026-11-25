"""Seeress configuration: the settings file, the profiles and the two doors' browser addresses.

A settings file Seeress cannot understand is an error, never a silent fallback: an
ignored setting could be the one that keeps client pages away from a cloud model.
"""

import os
import sys
from pathlib import Path

import pytest

from seeress import config
from seeress.errors import ConfigError

PORT_FILE = Path("Library/Application Support/Google/Chrome/DevToolsActivePort")

CLIENT_DOMAINS = """
[routing]
client_domains = ["Portal.Corp.Example.COM.", ".intranet"]
"""

ENDPOINTS = """
[[endpoints]]
name = "tenant"
base_url = "https://models.tenant.example/v1"
model = "tenant-large"
api_key_env = "TENANT_KEY"

[[endpoints]]
name = "local-smart"
base_url = "http://127.0.0.1:11435/v1"
"""


def settings_file(home: Path, text: str) -> Path:
    path = home / ".seeress" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ------------------------------------------------------- settings file ---


def test_without_a_settings_file_everything_is_empty(home: Path) -> None:
    assert config.load_settings() == config.Settings()


def test_client_domains_are_read_and_normalised(home: Path) -> None:
    settings_file(home, CLIENT_DOMAINS)

    settings = config.load_settings()

    assert settings.client_domains == ("portal.corp.example.com", "intranet")
    assert settings.endpoints == ()


def test_extra_endpoints_are_read(home: Path) -> None:
    settings_file(home, ENDPOINTS)

    settings = config.load_settings()

    assert settings.endpoints == (
        config.Endpoint("tenant", "https://models.tenant.example/v1", "tenant-large", "TENANT_KEY"),
        config.Endpoint("local-smart", "http://127.0.0.1:11435/v1", None, None),
    )
    assert settings.client_domains == ()


@pytest.mark.parametrize(
    "domain",
    [
        "https://portal.corp.example.com",
        "portal.corp.example.com/inbox",
        "portal corp example com",
        "corp..example.com",
        "-leading.example.com",
        "",
    ],
)
def test_a_client_domain_that_is_not_a_hostname_is_refused(home: Path, domain: str) -> None:
    settings_file(home, f'[routing]\nclient_domains = ["{domain}"]\n')

    with pytest.raises(ConfigError, match="not a domain name"):
        config.load_settings()


def test_invalid_toml_is_refused(home: Path) -> None:
    settings_file(home, "[routing\nclient_domains = [\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        config.load_settings()


def test_a_misnamed_key_is_refused_instead_of_ignored(home: Path) -> None:
    settings_file(home, '[routing]\nclient_domain = ["portal.corp.example.com"]\n')

    with pytest.raises(ConfigError, match="unknown setting 'client_domain'"):
        config.load_settings()


def test_an_unknown_top_level_table_is_refused(home: Path) -> None:
    settings_file(home, "[routing2]\nclient_domains = []\n")

    with pytest.raises(ConfigError, match="unknown setting 'routing2'"):
        config.load_settings()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://models.example/v1",
        "models.example/v1",
        "https://user:secret@models.example/v1",
        "https://models.example/v1?api_key=abc",
        "https://models.example/v1#fragment",
        "https:///v1",
    ],
)
def test_an_endpoint_url_that_is_not_plain_http_is_refused(home: Path, url: str) -> None:
    settings_file(home, f'[[endpoints]]\nname = "x"\nbase_url = "{url}"\n')

    with pytest.raises(ConfigError, match="endpoint 'x'"):
        config.load_settings()


def test_an_endpoint_without_a_name_or_url_is_refused(home: Path) -> None:
    settings_file(home, '[[endpoints]]\nmodel = "m"\n')

    with pytest.raises(ConfigError, match="needs a name and a base_url"):
        config.load_settings()


def test_the_home_folder_can_be_moved_for_tests_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEERESS_HOME", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "config.toml").write_text(CLIENT_DOMAINS)

    assert config.seeress_home() == tmp_path / "elsewhere"
    assert config.load_settings().client_domains == ("portal.corp.example.com", "intranet")


# ------------------------------------------------------------- profiles ---


def test_two_profiles_exist_with_the_documented_names() -> None:
    assert set(config.PROFILES) == {"personal", "client"}
    assert config.DEFAULT_PROFILE == "personal"


def test_personal_attaches_to_chrome_and_lists_the_cloud_endpoint_first() -> None:
    profile = config.PROFILES["personal"]

    assert profile.browser == "chrome"
    assert [endpoint.name for endpoint in profile.endpoints] == ["glm", "ollama-local"]


def test_client_lists_only_a_loopback_endpoint() -> None:
    profile = config.PROFILES["client"]

    assert profile.browser == "client-chromium"
    assert all(config.is_loopback(endpoint.base_url) for endpoint in profile.endpoints)


def test_browser_tool_sits_next_to_the_interpreter() -> None:
    assert os.path.join(os.path.dirname(sys.executable), "browser-use") == config.BU_CLI


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://127.5.4.3:11435/v1", True),
        ("http://[::1]:11434/v1", True),
        ("https://localhost:8443/v1", True),
        ("https://localhost.evil.example/v1", False),
        ("https://127.0.0.1.evil.example/v1", False),
        ("https://api.z.ai/api/paas/v4", False),
        ("https://2130706433.example/v1", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_loopback_is_decided_by_the_hostname(url: str, expected: bool) -> None:
    assert config.is_loopback(url) is expected


# ------------------------------------------------- the user's own Chrome ---


def test_personal_env_reads_the_live_websocket_address_from_chrome(home: Path) -> None:
    port_file = home / PORT_FILE
    port_file.parent.mkdir(parents=True)
    port_file.write_text("9333\n/devtools/browser/abc-def\n")

    env = config.browser_env("personal")

    assert env == {"BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/abc-def"}


def test_personal_env_is_empty_when_chrome_publishes_nothing(home: Path) -> None:
    assert config.browser_env("personal") == {}


def test_client_env_is_empty_even_when_chrome_is_running(home: Path) -> None:
    port_file = home / PORT_FILE
    port_file.parent.mkdir(parents=True)
    port_file.write_text("9333\n/devtools/browser/abc-def\n")

    assert config.browser_env("client") == {}
