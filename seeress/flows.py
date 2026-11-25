"""Seeress flows: reusable automations.

A flow is a Python file in flows/ that runs inside the browser tool with no model in the
loop: free, fast and the same every time. The agent writes one from a description
(`seeress create-flow`), `seeress run` replays it, and `seeress schedule` writes a
LaunchAgent so it runs without you.

A flow runs on a door, so it passes the same door check and the same name check as a task.
"""

import re
import time
from pathlib import Path

from . import activity, bodies, config
from .agent import (
    code_block,
    ensure_browser,
    observation,
    page_hostname,
    refused,
    run_browser_step,
)
from .brain import Message, chat, endpoint_for, message
from .browser_api import SYSTEM_PROMPT, check_code
from .errors import FlowError, SeeressError, UnsafeCodeError
from .runner import run_browser_command

FLOWS_DIR = Path(__file__).resolve().parent.parent / "flows"

# A flow name becomes a file name and part of a LaunchAgent label.
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CLOCK = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
DEFAULT_TIMEOUT = 300.0
FIRST_LINE = "# seeress-flow"
EXPLORE_OR_FINISH = "Explore (python block) or output the final flow script."

CREATE_PROMPT = (
    SYSTEM_PROMPT
    + """

SPECIAL TASK — WRITE A FLOW. You may first EXPLORE: execute up to 3 python
blocks to visit the relevant pages and inspect their structure (selectors,
urls, form names). Then output your FINAL answer as ONE complete,
self-contained python script that:
- performs the described workflow start-to-finish using only the helpers
- is deterministic: no LLM calls, waits only via wait_for_load() /
  wait_for_element() / time.sleep()
- passes BARE EXPRESSIONS to js() (e.g. js("document.title")) — never the
  word 'return', never return_by_value
- verifies loads: after wait_for_load(), check js("document.title") is not
  an error page; if it is, goto_url(...) once more and wait_for_load()
- starts with the comment:  # seeress-flow: <short name>
- ends by print()ing a one-line summary of what it did
Your final message must be ONLY that fenced python block."""
)


def _check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise FlowError(
            f"{name!r} is not a flow name. Use 1 to 40 lower-case letters, digits or hyphens."
        )


def list_flows() -> list[Path]:
    """The saved flows, in name order."""
    return sorted(FLOWS_DIR.glob("*.py"))


def run_flow(name: str, profile: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run a saved flow once and return what it printed.

    The run is noted in the activity record, from the moment the flow is found: a flow that
    is not there did not run. A run that a SeeressError ends is noted as failed, with the
    class of the error, and the error goes on raising.

    Raises FlowError when there is no such flow or the file is not allowed, and
    BrowserError when the browser tool did not exit zero.
    """
    _check_name(name)
    path = FLOWS_DIR / f"{name}.py"
    if not path.is_file():
        existing = ", ".join(flow.stem for flow in list_flows()) or "(none)"
        raise FlowError(f"no flow named {name!r}. existing: {existing}")
    code = path.read_text(encoding="utf-8")
    started = time.time()
    home = config.seeress_home()
    try:
        # a saved file can be edited by anything
        check_code(code)
        ensure_browser(profile)
        result = run_browser_command(
            code,
            profile=profile,
            ws=bodies.address_for(profile),
            timeout=timeout,
            what="flow",
            name=name,
        )
    except SeeressError as error:
        activity.note_flow_run(home, activity.clock(started), name, 1, type(error).__name__)
        raise
    activity.note_flow_run(home, activity.clock(started), name, 0, None)
    return result.stdout.strip()


def create_flow(name: str, description: str, profile: str, max_steps: int = 6) -> Path:
    """Have the agent explore the live pages, then write a replayable flow.

    Returns the flow's path. Raises FlowError when the name is not allowed, when the
    model's script does not parse or is not allowed, or when it never produced one.
    """
    _check_name(name)
    ensure_browser(profile)
    endpoint = endpoint_for(profile, page_hostname(profile))
    history: list[Message] = [
        message("system", content=CREATE_PROMPT),
        message("user", content=f"Write a flow named {name!r} that: {description}"),
    ]
    for _ in range(max_steps):
        reply = chat(endpoint, history)
        code = code_block(reply)
        if code is None:
            history += [
                message("assistant", content=reply),
                message("user", content=EXPLORE_OR_FINISH),
            ]
            continue
        if not code.lstrip().startswith(FIRST_LINE):
            # an exploratory step
            try:
                check_code(code)
            except UnsafeCodeError as exc:
                history += refused(reply, exc)
                continue
            history += [
                message("assistant", content=reply),
                observation(run_browser_step(code, profile=profile)),
            ]
            endpoint = endpoint_for(profile, page_hostname(profile))
            continue
        try:
            compile(code, f"{name}.py", "exec")
        except SyntaxError as exc:
            raise FlowError(
                f"the model wrote code that does not parse ({exc}); refine the description"
            ) from exc
        try:
            check_code(code)
        except UnsafeCodeError as exc:
            raise FlowError(
                f"the model wrote code Seeress does not allow ({exc}); refine the description"
            ) from exc
        FLOWS_DIR.mkdir(exist_ok=True)
        path = FLOWS_DIR / f"{name}.py"
        path.write_text(code.strip() + "\n", encoding="utf-8")
        return path
    raise FlowError("the model did not produce a final flow script; refine the description")


def schedule_plist(name: str, every_minutes: int | None = None, daily: str | None = None) -> Path:
    """Write a LaunchAgent plist that runs a flow. Returns its path; nothing is loaded.

    Without either option the flow runs every day at 08:00.
    """
    _check_name(name)
    daily = "08:00" if daily is None else daily
    if not CLOCK.fullmatch(daily):
        raise FlowError(f"{daily!r} is not a time. Write HH:MM, 24 hours.")
    if every_minutes is not None and every_minutes < 1:
        raise FlowError("--every needs a number of minutes of at least 1")

    run_script = FLOWS_DIR.parent / "bin" / "seeress-client"  # flows run client-side
    if every_minutes is not None:
        timing = f"<key>StartInterval</key><integer>{every_minutes * 60}</integer>"
    else:
        hour, minute = daily.split(":")
        timing = (
            "<key>StartCalendarInterval</key><dict>"
            f"<key>Hour</key><integer>{int(hour)}</integer>"
            f"<key>Minute</key><integer>{int(minute)}</integer></dict>"
        )
    logs = _log_folder()
    label = f"ai.seeress.{name}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>{run_script}</string>
    <string>run</string><string>{name}</string>
  </array>
  {timing}
  <key>StandardOutPath</key><string>{logs}/{name}.log</string>
  <key>StandardErrorPath</key><string>{logs}/{name}.err</string>
</dict></plist>
""",
        encoding="utf-8",
    )
    return plist


def _log_folder() -> Path:
    from . import config

    folder = config.seeress_home()
    folder.mkdir(parents=True, exist_ok=True)
    return folder
