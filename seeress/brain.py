"""Seeress brain: which model endpoint a request may go to, and how to ask it.

endpoint_for is the domain guard, and it fails closed. A page Seeress could not check
counts as a client page, and on a client page only a model on this machine may be used.

chat speaks OpenAI-compatible chat completions with the standard library only, so nothing
stands between Seeress and the endpoint.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import urlsplit

from .config import PROFILES, domain_matches, is_loopback, load_settings
from .errors import BrainError, ConfigError, GuardViolation

DETAIL_CAP = 200


class Message(TypedDict):
    """One turn of a chat, in the shape every OpenAI-compatible endpoint takes."""

    role: str
    content: str


@dataclass(frozen=True)
class Chosen:
    """The endpoint a request was routed to."""

    name: str
    base_url: str
    model: str
    api_key: str


def message(role: str, content: str) -> Message:
    """Build one chat turn."""
    return Message(role=role, content=content)


def endpoint_for(profile: str, active_domain: str | None = None) -> Chosen:
    """Choose the endpoint for the next model request.

    active_domain is the hostname of the page the browser is on: "" when no page is
    open, and None when the browser could not be asked. None counts as guarded.
    """
    try:
        door = PROFILES[profile]
    except KeyError:
        raise ConfigError(f"unknown profile {profile!r}") from None

    settings = load_settings()
    guarded = active_domain is None or domain_matches(
        active_domain.lower(), settings.client_domains
    )
    configured = {endpoint.name for endpoint in settings.endpoints}
    candidates = (
        *settings.endpoints,
        *(endpoint for endpoint in door.endpoints if endpoint.name not in configured),
    )
    for endpoint in candidates:
        if guarded and not is_loopback(endpoint.base_url):
            continue  # the guard: a client page may only go to a model on this machine
        if endpoint.api_key_env:
            api_key = os.environ.get(endpoint.api_key_env, "")
            if not api_key:
                continue  # without its key the endpoint is not available
        else:
            api_key = "local"
        model = endpoint.model or os.environ.get(f"SEERESS_MODEL_{endpoint.name.upper()}", "")
        return Chosen(endpoint.name, endpoint.base_url.rstrip("/"), model, api_key)

    raise GuardViolation(
        f"No permitted endpoint for profile={profile!r}, domain={active_domain!r}. "
        "The domain guard removed every endpoint that is not on this machine, and none of "
        "those answered."
    )


def chat(endpoint: Chosen, messages: Sequence[Message], timeout: float = 300) -> str:
    """One chat completion. Returns the assistant's text, or raises BrainError."""
    base_url = endpoint.base_url.rstrip("/")
    if urlsplit(base_url).scheme not in {"http", "https"}:
        raise BrainError(f"{endpoint.name} is not an http or https address; no request was sent")
    body = json.dumps(
        {"model": endpoint.model, "messages": list(messages), "stream": False}
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - the scheme was checked above
        f"{base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {endpoint.api_key}",
        },
    )
    try:
        raw = _send(request, timeout, endpoint.name)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:DETAIL_CAP].decode("utf-8", "replace").strip()
        raise BrainError(f"{endpoint.name} answered HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BrainError(f"{endpoint.name} could not be reached: {exc}") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BrainError(f"{endpoint.name} did not answer with JSON") from exc
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BrainError(f"{endpoint.name} gave no answer in the expected shape") from exc
    return str(answer)


def _direct_opener() -> urllib.request.OpenerDirector:
    """An opener that names no proxy, for a model on this machine.

    urllib's own opener hands a request to a proxy that http_proxy names, a request to
    this machine included: so a proxy would see the prompt, and could answer in the
    model's place. A request to any other address keeps the library's way, since a
    company may need its proxy to reach the model it pays for.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _send(request: urllib.request.Request, timeout: float, name: str) -> bytes:
    """Send one request and return the body. The scheme is checked where the call is made."""
    if urlsplit(request.full_url).scheme not in {"http", "https"}:
        raise BrainError(f"{name} is not an http or https address; no request was sent")
    opening: Any = urllib.request.urlopen
    if is_loopback(request.full_url):
        opening = _direct_opener().open
    with opening(request, timeout=timeout) as response:
        return bytes(response.read())
