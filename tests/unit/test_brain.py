"""The brain: which model endpoint a request may go to, and how to ask it.

The guard's rule is capability. A page Seeress cannot check is treated as a client page,
and a client page may only be sent to a model on this machine.
"""

import io
import json
import urllib.error
import urllib.request
from email.message import Message as Headers
from pathlib import Path
from typing import Any

import pytest

from seeress import brain, config
from seeress.errors import BrainError, ConfigError, GuardViolation

CLOUD = brain.Chosen("glm", "https://api.z.ai/api/paas/v4", "", "secret-key")
LOCAL = brain.Chosen("ollama-local", "http://localhost:11434/v1", "nemotron-3.5-lightning", "local")
USER = brain.Message(role="user", content="hi")


def guard(home: Path, *domains: str) -> None:
    """List client domains in the settings file."""
    path = home / ".seeress" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = ", ".join(json.dumps(domain) for domain in domains)
    path.write_text(f"[routing]\nclient_domains = [{listed}]\n")


def extra_endpoints(home: Path, text: str) -> None:
    path = home / ".seeress" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ------------------------------------------------- choosing an endpoint ---


def test_personal_uses_the_cloud_endpoint_when_its_key_is_present(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")

    assert brain.endpoint_for("personal", "") == CLOUD


def test_personal_falls_back_to_the_local_endpoint_without_the_key(home: Path) -> None:
    assert brain.endpoint_for("personal") == LOCAL


def test_client_never_offers_a_cloud_endpoint_even_with_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")

    assert brain.endpoint_for("client") == LOCAL


def test_model_name_comes_from_the_environment_when_the_profile_has_none(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    monkeypatch.setenv("SEERESS_MODEL_GLM", "glm-4.7")

    assert brain.endpoint_for("personal", "").model == "glm-4.7"


def test_no_available_endpoint_raises_guard_violation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        config.PROFILES, "client", config.Profile("client", "", (GLM,), "client-chromium")
    )

    with pytest.raises(GuardViolation, match="No permitted endpoint for profile='client'"):
        brain.endpoint_for("client")


def test_an_unknown_profile_is_a_configuration_error(home: Path) -> None:
    with pytest.raises(ConfigError, match="unknown profile 'guest'"):
        brain.endpoint_for("guest")


# ------------------------------------------------------ the domain guard ---


def test_a_client_domain_removes_cloud_endpoints_from_the_menu(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    guard(home, "corp.example.com")

    assert brain.endpoint_for("personal", "corp.example.com") == LOCAL


def test_a_subdomain_of_a_client_domain_is_guarded(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    guard(home, "corp.example.com")

    assert brain.endpoint_for("personal", "portal.Corp.Example.com") == LOCAL


def test_the_guard_decides_on_the_hostname_not_on_text(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that merely contains the client domain is a different site."""
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    guard(home, "corp.example.com")

    assert brain.endpoint_for("personal", "notcorp.example.com") == CLOUD
    assert brain.endpoint_for("personal", "portal.corp.example.com.evil.example") == CLOUD


def test_no_page_open_is_unguarded(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")

    assert brain.endpoint_for("personal", "") == CLOUD


def test_a_page_that_cannot_be_checked_counts_as_a_client_page(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix: None used to mean unguarded, so a dead browser unlocked the cloud endpoint."""
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")

    assert brain.endpoint_for("personal", None) == LOCAL


def test_a_page_that_cannot_be_checked_leaves_only_this_machine(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    monkeypatch.setitem(
        config.PROFILES,
        "personal",
        config.Profile("personal", "", (GLM,), "chrome"),
    )

    with pytest.raises(GuardViolation, match="domain=None"):
        brain.endpoint_for("personal", None)


def test_guard_with_no_loopback_endpoint_left_raises(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    guard(home, "corp.example.com")
    monkeypatch.setitem(
        config.PROFILES,
        "personal",
        config.Profile("personal", "", (GLM,), "chrome"),
    )

    with pytest.raises(GuardViolation, match=r"domain='corp\.example\.com'"):
        brain.endpoint_for("personal", "corp.example.com")


def test_a_loopback_endpoint_is_recognised_by_its_hostname(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far = config.Endpoint("x", "http://127.0.0.1:11435/v1", "big", None)
    built_in = config.Endpoint(
        "ollama-local", "http://localhost:11434/v1", "nemotron-3.5-lightning", None
    )
    monkeypatch.setitem(
        config.PROFILES, "client", config.Profile("client", "", (far, built_in), "c")
    )

    assert brain.endpoint_for("client", "anything").name == "x"


def test_a_settings_file_seeress_cannot_read_blocks_instead_of_being_ignored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    extra_endpoints(home, "[routing]\nclient_domains = [broken\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        brain.endpoint_for("personal", "")


# ------------------------------------------------- extra model endpoints ---


TENANT = """
[[endpoints]]
name = "tenant"
base_url = "https://models.tenant.example/v1"
model = "tenant-large"
api_key_env = "TENANT_KEY"
"""


def test_an_endpoint_from_the_settings_file_is_tried_first(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENANT_KEY", "t")
    extra_endpoints(home, TENANT)

    chosen = brain.endpoint_for("personal", "")

    assert chosen.name == "tenant"
    assert chosen.model == "tenant-large"
    assert chosen.api_key == "t"


def test_an_endpoint_from_the_settings_file_needs_its_key_like_any_other(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-key")
    extra_endpoints(home, TENANT)

    assert brain.endpoint_for("personal", "").name == "glm"


def test_a_configured_endpoint_replaces_the_built_in_one_of_the_same_name(
    home: Path,
) -> None:
    extra_endpoints(
        home, '[[endpoints]]\nname = "ollama-local"\nbase_url = "http://localhost:9999/v1"\n'
    )

    assert brain.endpoint_for("client").base_url == "http://localhost:9999/v1"


def test_the_guard_removes_a_configured_endpoint_that_is_not_on_this_machine(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENANT_KEY", "t")
    extra_endpoints(home, TENANT)
    guard(home, "corp.example.com")

    assert brain.endpoint_for("client", "corp.example.com") == LOCAL


# --------------------------------------------------------- the request ---


class Exchange:
    """Stands in for the network and records the one request chat() makes."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self.status = 200
        self.body: bytes = json.dumps(
            {"choices": [{"message": {"content": "hello there"}}]}
        ).encode()

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "Server Error", Headers(), io.BytesIO(self.body)
            )
        return io.BytesIO(self.body)


def test_chat_sends_one_openai_shaped_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = Exchange()
    monkeypatch.setattr(urllib.request, "urlopen", exchange)

    reply = brain.chat(LOCAL, [USER], timeout=7)

    assert reply == "hello there"
    (request,) = exchange.requests
    assert request.full_url == "http://localhost:11434/v1/chat/completions"
    assert exchange.timeouts == [7]
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == "Bearer local"
    data = request.data
    assert isinstance(data, bytes)
    body: Any = json.loads(data)
    assert body == {
        "model": "nemotron-3.5-lightning",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }


def test_an_http_error_names_the_endpoint_and_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = Exchange()
    exchange.status = 503
    exchange.body = b"upstream overloaded"
    monkeypatch.setattr(urllib.request, "urlopen", exchange)

    with pytest.raises(BrainError, match=r"ollama-local answered HTTP 503: upstream overloaded"):
        brain.chat(LOCAL, [USER])


def test_an_unreachable_endpoint_is_reported_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        raise urllib.error.URLError(OSError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    with pytest.raises(BrainError, match="ollama-local could not be reached"):
        brain.chat(LOCAL, [USER])


def test_an_answer_that_is_not_json_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = Exchange()
    exchange.body = b"<html>proxy login</html>"
    monkeypatch.setattr(urllib.request, "urlopen", exchange)

    with pytest.raises(BrainError, match="did not answer with JSON"):
        brain.chat(LOCAL, [USER])


def test_an_answer_without_a_choice_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = Exchange()
    exchange.body = b'{"error": "model not found"}'
    monkeypatch.setattr(urllib.request, "urlopen", exchange)

    with pytest.raises(BrainError, match="no answer"):
        brain.chat(LOCAL, [USER])


def test_an_endpoint_that_is_not_http_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = Exchange()
    monkeypatch.setattr(urllib.request, "urlopen", exchange)
    local_file = brain.Chosen("files", "file:///etc/passwd", "m", "local")

    with pytest.raises(BrainError, match="not an http or https address"):
        brain.chat(local_file, [USER])

    assert exchange.requests == []


def test_no_secret_appears_in_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        raise urllib.error.URLError(OSError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    with pytest.raises(BrainError) as caught:
        brain.chat(CLOUD, [USER])

    assert "secret-key" not in str(caught.value)


GLM = config.Endpoint("glm", "https://api.z.ai/api/paas/v4", None, "ZAI_API_KEY")
