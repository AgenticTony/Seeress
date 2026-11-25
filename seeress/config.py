"""Seeress configuration: the profiles, the home folder and the settings file.

Two profiles, two doors (never a switch to forget):
  personal : the user's own Chrome. A cloud model is allowed; a local one is the fallback.
  client   : a Chromium Seeress owns. LOCAL ONLY: no cloud endpoint is even listed.

Settings that differ per machine live in SEERESS_HOME/config.toml (by default
~/.seeress/config.toml):

    [routing]
    client_domains = ["portal.corp.example.com"]   # never sent to a cloud model

    [[endpoints]]                                  # tried before the built-in ones
    name = "tenant"
    base_url = "https://models.tenant.example/v1"
    model = "tenant-large"                         # optional
    api_key_env = "TENANT_KEY"                     # optional

    [capture]                                      # every key is optional
    days = ["mon", "tue", "wed", "thu", "fri"]     # Monday to Friday when left out
    from = "08:00"                                 # 08:00 when left out
    to = "18:00"                                   # 18:00 when left out
    keep_days = 30                                 # 30 when left out

A file Seeress cannot understand raises ConfigError. It is never skipped: an ignored
setting could be the one that keeps client pages away from a cloud model.
"""

import ipaddress
import os
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .activity import Schedule
from .errors import ConfigError

CONFIG_FILE = "config.toml"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible chat endpoint. A model of None means SEERESS_MODEL_<NAME> decides."""

    name: str
    base_url: str
    model: str | None
    api_key_env: str | None


@dataclass(frozen=True)
class Profile:
    """A door: which browser it drives and which model endpoints it may use."""

    name: str
    description: str
    endpoints: tuple[Endpoint, ...]
    browser: str


PROFILES: Mapping[str, Profile] = {
    "personal": Profile(
        name="personal",
        description="Everyday work. A cloud model is allowed; a local one is the fallback.",
        endpoints=(
            Endpoint("glm", "https://api.z.ai/api/paas/v4", None, "ZAI_API_KEY"),
            Endpoint("ollama-local", "http://localhost:11434/v1", "nemotron-3.5-lightning", None),
        ),
        browser="chrome",  # attach to the user's Chrome
    ),
    "client": Profile(
        name="client",
        description="Client work. LOCAL ONLY. Nothing leaves the machine.",
        endpoints=(
            Endpoint("ollama-local", "http://localhost:11434/v1", "nemotron-3.5-lightning", None),
        ),
        # slotstream 125B (Anthropic-native, port 11435) is the escalation tier,
        # wired via its own adapter when needed. Still local.
        browser="client-chromium",  # a Chromium on a profile Seeress owns
    ),
}

DEFAULT_PROFILE = "personal"
OLLAMA_URL = "http://localhost:11434"

# The browser-use CLI that ships in this virtual environment (it may not be on PATH).
BU_CLI = os.path.join(os.path.dirname(sys.executable), "browser-use")

CHROME_PORT_FILE = Path("Library/Application Support/Google/Chrome/DevToolsActivePort")

_HOSTNAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.?$"
)


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class CaptureSettings:
    """When the capture records, and for how long it keeps what it recorded.

    start and end are the hours of the schedule, its first and its last. In the settings
    file the end is written as the time the capture stops, so "18:00" is an end of 17.
    """

    days: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    start: int = 8
    end: int = 17
    keep_days: int = 30

    @property
    def schedule(self) -> Schedule:
        """The schedule of the capture, as the miner reads one."""
        return Schedule(self.days, self.start, self.end)


@dataclass(frozen=True)
class Settings:
    """What the settings file says. Without a file, the lists are empty and the capture
    keeps its defaults."""

    client_domains: tuple[str, ...] = ()
    endpoints: tuple[Endpoint, ...] = ()
    capture: CaptureSettings = field(default_factory=CaptureSettings)


def seeress_home() -> Path:
    """Where Seeress keeps its own files. SEERESS_HOME moves it."""
    raw = os.environ.get("SEERESS_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".seeress"


def is_loopback(url: str) -> bool:
    """True when the URL's host is this machine: localhost or a loopback address."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def browser_env(profile: str) -> dict[str, str]:
    """The browser address for a door that attaches to a browser it did not start.

    personal: the user's own Chrome. Chrome hides DevTools behind a per-client approval
    sheet but publishes its live WebSocket address in DevToolsActivePort; read it and pass
    it to the browser tool explicitly.
    """
    if PROFILES[profile].browser != "chrome":
        return {}
    try:
        port, _, ws_path = (Path.home() / CHROME_PORT_FILE).read_text().strip().partition("\n")
    except OSError:
        return {}  # Chrome is not running, or publishes nothing
    return {"BU_CDP_WS": f"ws://127.0.0.1:{port.strip()}{ws_path.strip()}"}


def load_settings(home: Path | None = None) -> Settings:
    """Read the settings file. Raise ConfigError when it cannot be understood."""
    path = (home or seeress_home()) / CONFIG_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Settings()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    routing = _table(document, "routing", path)
    endpoints = document.get("endpoints", [])
    if not isinstance(endpoints, list):
        raise ConfigError(f"{path}: 'endpoints' must be a list of tables")
    capture = document.get("capture", {})
    if not isinstance(capture, dict):
        raise ConfigError(f"{path}: 'capture' must be a table")
    return Settings(
        client_domains=_client_domains(routing, path),
        endpoints=tuple(_endpoint(item, path) for item in endpoints),
        capture=_capture(capture, path),
    )


def _table(document: Mapping[str, object], key: str, path: Path) -> Mapping[str, object]:
    extra = sorted(set(document) - {"routing", "endpoints", "capture"})
    if extra:
        raise ConfigError(f"{path}: unknown setting {extra[0]!r}")
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: {key!r} must be a table")
    return value


_HOUR = re.compile(r"([01][0-9]|2[0-3]):00")


def _capture(raw: Mapping[str, object], path: Path) -> CaptureSettings:
    """The [capture] table. Every key is optional, and each is refused unless it is whole:
    a schedule with minutes in it, or a day of the week misspelt, would record at hours
    nobody chose."""
    unknown = sorted(set(raw) - {"days", "from", "to", "keep_days"})
    if unknown:
        raise ConfigError(f"{path}: unknown setting {unknown[0]!r}")
    days = raw.get("days", list(DAYS[:5]))
    if not isinstance(days, list) or not _are_days(days):
        raise ConfigError(
            f"{path}: 'days' must be a list of day names, and not empty: "
            "mon, tue, wed, thu, fri, sat or sun, in lower case."
        )
    start = _hour(raw.get("from", "08:00"), "from", path)
    stop = _hour(raw.get("to", "18:00"), "to", path)
    if stop <= start:
        raise ConfigError(
            f"{path}: 'to' must be after 'from'. A schedule that runs past midnight is not taken."
        )
    keep_days = raw.get("keep_days", 30)
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ConfigError(f"{path}: 'keep_days' must be a whole number of days, 1 or more.")
    return CaptureSettings(frozenset(DAYS.index(day) for day in days), start, stop - 1, keep_days)


def _are_days(days: list[object]) -> bool:
    return bool(days) and all(isinstance(day, str) and day in DAYS for day in days)


def _hour(value: object, key: str, path: Path) -> int:
    """A whole hour written HH:00, as its number."""
    if not isinstance(value, str) or (found := _HOUR.fullmatch(value)) is None:
        raise ConfigError(f'{path}: {key!r} must be a whole hour written HH:00, as "08:00".')
    return int(found.group(1))


def _client_domains(routing: Mapping[str, object], path: Path) -> tuple[str, ...]:
    unknown = sorted(set(routing) - {"client_domains"})
    if unknown:
        raise ConfigError(f"{path}: unknown setting {unknown[0]!r}")
    raw = routing.get("client_domains", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{path}: 'client_domains' must be a list of domain names")
    return tuple(_hostname(item, path) for item in raw)


def _hostname(raw: str, path: Path) -> str:
    value = raw.strip().lower().lstrip(".").rstrip(".")
    if not _HOSTNAME.fullmatch(value):
        raise ConfigError(
            f"{path}: {raw!r} is not a domain name. Write a hostname such as "
            '"portal.corp.example.com": no scheme, no path, no spaces.'
        )
    return value


def _endpoint(raw: object, path: Path) -> Endpoint:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: each endpoint must be a table")
    allowed = {"name", "base_url", "model", "api_key_env"}
    if sorted(set(raw) - allowed):
        raise ConfigError(f"{path}: endpoint {raw.get('name', '')!r} has an unknown setting")
    for item in raw.values():
        if not isinstance(item, str):
            raise ConfigError(f"{path}: endpoint settings must be strings")
    name, base_url = raw.get("name"), raw.get("base_url")
    if not name or not base_url:
        raise ConfigError(f"{path}: an endpoint needs a name and a base_url")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(
            f"{path}: endpoint {name!r} needs an http or https base_url without "
            "credentials, a query or a fragment"
        )
    return Endpoint(
        name=name,
        base_url=base_url.rstrip("/"),
        model=raw.get("model") or None,
        api_key_env=raw.get("api_key_env") or None,
    )


def domain_matches(hostname: str, client_domains: Iterable[str]) -> bool:
    """True when the hostname is a client domain or one of its subdomains.

    The match is on a hostname boundary: corp.example.com covers portal.corp.example.com
    and neither notcorp.example.com nor corp.example.com.evil.example.
    """
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in client_domains)
