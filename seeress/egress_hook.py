"""Records what a Python process contacts. Installed only by tools/egress_check.py.

The check puts a .pth file into the environment for the duration of a run, so every
Python process started from that environment runs this module at start-up. In an
ordinary run SEERESS_EGRESS_LOG is not set, and this does nothing at all.

The record holds hosts, ports, program names and the names of environment variables. It
never holds a path, a query, a header, or a value that could carry data or a secret.
Nothing after a -c option is shown: it is code, and code can hold a value.
"""

import contextlib
import json
import os
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

LOG = "SEERESS_EGRESS_LOG"

# What a Unix socket is recorded as. Its name is a path, and is not recorded.
LOCAL_SOCKET = "a local socket"

# Settings whose values say whether Seeress's controls reached a child. None is a secret.
SHOWN = (
    "ANONYMIZED_TELEMETRY",
    "BROWSER_USE_NO_UPDATE_CHECK",
    "BH_UPDATE_CHECK",
    "BU_AUTOSPAWN",
    "BU_NAME",
    "BU_CDP_WS",
    "BH_HOME",
)


def origin(url: object) -> str | None:
    """The scheme, host and port of a URL, and nothing else."""
    text = getattr(url, "full_url", url)
    if not isinstance(text, str):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if not parts.scheme or parts.hostname is None:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def endpoint(address: object) -> tuple[str | None, int | None]:
    """The host and port a socket was told to connect to."""
    if isinstance(address, tuple) and address:
        host, port = address[0], address[1]
        shown = str(host) if host is not None else None
        return shown, port if isinstance(port, int) else None
    if isinstance(address, (str, bytes)):
        return LOCAL_SOCKET, None
    return None, None


def _name(item: object) -> str:
    """A program's name, or an option's name. A path or a value is never shown."""
    text = str(item)
    if text.startswith("-"):
        return text.split("=", 1)[0]
    return os.path.basename(text)


def _shown(items: Iterable[object], limit: int) -> str:
    """The first items of a command line, in names only, and nothing after a -c option."""
    shown: list[str] = []
    for item in items:
        shown.append(_name(item))
        if item == "-c" or len(shown) == limit:
            break
    return " ".join(shown)[:120]


def program(executable: object, arguments: object) -> str:
    """What was started: names only. Anything after an = sign is a value, and is left out."""
    if isinstance(arguments, str):
        return "a shell command"  # its text is code
    items = list(arguments) if isinstance(arguments, (list, tuple)) else [executable]
    return _shown(items, 3)


def role() -> str:
    """How this process was started: in names only, and nothing after a -c option.

    The original command line is used because under -m the first entry of sys.argv is
    not the interpreter.
    """
    return _shown(getattr(sys, "orig_argv", sys.argv), 4)


_installed = False


def install() -> None:
    """Record this process's connections to the file SEERESS_EGRESS_LOG names.

    The first thing it records is that this process exists. The judge requires every
    process Seeress started to be on the record: a process that was started and never seen
    makes the record incomplete, and an incomplete record proves nothing.
    """
    global _installed
    path = os.environ.get(LOG)
    if not path or _installed:
        return
    # Once per process: the start-up file that loads this module can be run more than once.
    _installed = True
    try:
        # Opened before the hook exists, so the hook never records its own file.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    pid, parent, name = os.getpid(), os.getppid(), role()

    def write(event: str, **fields: object) -> None:
        line = json.dumps({"pid": pid, "parent": parent, "role": name, "event": event, **fields})
        with contextlib.suppress(OSError):
            os.write(descriptor, (line + "\n").encode("utf-8"))

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            host, port = endpoint(args[1] if len(args) > 1 else None)
            write("connect", host=host, port=port)
        elif event == "socket.getaddrinfo":
            write("resolve", host=str(args[0]) if args and args[0] is not None else None)
        elif event == "subprocess.Popen":
            env = args[3] if len(args) > 3 else None
            write(
                "spawn",
                program=program(args[0], args[1] if len(args) > 1 else None),
                env=None if env is None else sorted(env),
                switches={key: env[key] for key in SHOWN if key in env} if env else None,
            )
        elif event == "urllib.Request":
            write("request", origin=origin(args[0]) if args else None)

    write("start")
    sys.addaudithook(hook)
