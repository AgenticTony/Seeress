"""The environment every child process gets: an allow-list, never the inherited one.

The browser tool and the daemon it starts read their settings from the environment. An
inherited environment could therefore hand them a cloud key, a proxy, or a switch that
turns their usage telemetry on. Seeress builds the child's environment from scratch, and
keeps everything the library writes inside the Seeress home.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from . import config
from .errors import ConfigError

# What a browser Seeress starts is given: what an application needs, and nothing of Seeress's.
# A browser lives long and has no use for a model key or a library switch.
BROWSER_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
)

# Copied from Seeress's own environment when they are set. Nothing else ever is.
PASSTHROUGH: tuple[str, ...] = (
    "PATH",  # the browser tool finds the browser binary through it on Linux
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",  # text encoding
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
    # Set only by tools/egress_check.py: where a process records what it contacts. Without
    # it here, the children were never recorded, and the record proved nothing.
    "SEERESS_EGRESS_LOG",
    # Windows: the browser tool looks for Chrome through these
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
)

# Set on every child, whatever Seeress's own environment says.
FORCED: Mapping[str, str] = {
    "ANONYMIZED_TELEMETRY": "false",  # the library's usage telemetry
    "BROWSER_USE_NO_UPDATE_CHECK": "1",  # its check for a newer release on PyPI
    "BH_UPDATE_CHECK": "0",  # browser-harness's own check for a newer release
}

# The browser tool's daemon listens on a Unix socket whose path the operating system caps.
# These are the limits the library enforces; a contract test pins them to its source.
SOCKET_LIMIT = 103 if sys.platform == "darwin" else 107


def socket_path(daemon: str | None) -> Path:
    """Where the browser tool's daemon for a door listens."""
    return config.seeress_home() / "browser-harness" / "runtime" / f"bu-{daemon or 'default'}.sock"


def check_socket_path(daemon: str | None) -> None:
    """Refuse a Seeress home too deep for that socket, and say what to do about it.

    Left to the library, this surfaces as its own error from inside a step.
    """
    if sys.platform == "win32":  # the daemon uses TCP loopback there
        return
    path = socket_path(daemon)
    size = len(str(path).encode())
    if size > SOCKET_LIMIT:
        raise ConfigError(
            f"SEERESS_HOME is too deep for the browser tool: its socket path would be {size} "
            f"bytes, over this system's limit of {SOCKET_LIMIT} ({path}). Move the Seeress home "
            "to a shorter path, for example SEERESS_HOME=~/.seeress."
        )


# A loopback port where nothing listens. Only an administrator could make something listen
# there, so pointing a browser's proxy at it means every connection that is not exempt
# fails. The egress check plants proxy settings that point here as well.
DEAD_PROXY_PORT = 9

# Names that must never reach a child, whatever Seeress's own environment holds. The
# egress check plants each of them and then looks for them in what a child was given.
DENIED_EXAMPLES: tuple[str, ...] = (
    "BROWSER_USE_API_KEY",
    "BU_AUTOSPAWN",
    "BU_BROWSER_ID",
    "BU_CDP_URL",
    "ZAI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "BH_TELEMETRY",
)

# The daemon name of each door that owns its browser. A door without an entry uses the
# library's default name. Two doors must never share a daemon: a daemon is attached to
# exactly one browser, and that browser is the door's whole guarantee.
DOOR_DAEMON: Mapping[str, str] = {"client": "seeress-client"}


def child_env(
    profile: str,
    ws: str | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for one call of the browser tool on a door.

    ws is the address of the browser the door attaches to. base is where the copied names
    come from; it is Seeress's own environment unless a test says otherwise.
    """
    daemon = DOOR_DAEMON.get(profile)
    check_socket_path(daemon)
    source = os.environ if base is None else base
    child = {name: source[name] for name in PASSTHROUGH if source.get(name)}
    child.update(FORCED)
    # One setting moves everything the library persists: its daemon sockets, telemetry
    # and update files, its cloud sign-in, and the helper files it would load as code.
    child["BH_HOME"] = str(config.seeress_home() / "browser-harness")
    if daemon:
        child["BU_NAME"] = daemon
    if ws:
        child["BU_CDP_WS"] = ws
    return child


def browser_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a browser Seeress starts: an allow-list, like every child."""
    source = os.environ if base is None else base
    return {name: source[name] for name in BROWSER_PASSTHROUGH if source.get(name)}
