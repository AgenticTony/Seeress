"""Run one step of Python inside the browser tool and report exactly what came back.

Every start of the browser tool goes through run_browser_tool, so every child gets the
allow-listed environment from seeress.env and nothing else, and every call says which
browser it means (seeress.bodies decides which that may be). And every run is noted in the
activity record: in attach mode the capture cannot tell the agent's tabs from the person's,
so the miner leaves the minutes of a run out of the person's work.

Two calls share that mechanism:
- run_browser_attempt never raises over a failed step; it reports the exit code, the
  output, stderr and whether the step timed out. The agent loop turns the report into
  text the model reads.
- run_browser_command is for Seeress's own steps; it raises BrowserError unless the step
  ran cleanly and exited zero.
"""

import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

from . import config, env
from .activity import note_agent_run
from .errors import BrowserError

STDOUT_CAP = 4000
STDERR_CAP = 2000
REASON_CAP = 400


@dataclass(frozen=True)
class StepResult:
    """What the browser tool printed, and how it ended."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def feedback(self) -> str:
        """The text the model sees next: the output, then the reason when the step failed."""
        output = self.stdout.strip()[:STDOUT_CAP]
        if self.ok:
            return output
        if self.timed_out:
            reason = "timed out"
            tail = ""
        else:
            reason = f"exit code {self.returncode}"
            tail = self.stderr.strip()[-STDERR_CAP:]
        head = f"{output}\n" if output else ""
        if tail:
            return f"{head}[step failed: {reason}]\n{tail}"
        return f"{head}[step failed: {reason}]"


def run_browser_tool(
    args: Sequence[str],
    *,
    profile: str,
    ws: str | None,
    timeout: float,
    code: str | None = None,
    what: str = "probe",
    name: str | None = None,
) -> StepResult:
    """Start the browser tool once and report what it did.

    The run is noted in the activity record, from just before it to just after, however it
    ended. what says what the run was: a task, a flow or a probe. name is the flow's, when
    it was one.

    Raises BrowserError only when the tool could not be started at all.
    """
    started = time.time()
    try:
        return _start(args, profile=profile, ws=ws, timeout=timeout, code=code)
    finally:
        note_agent_run(config.seeress_home(), started, time.time(), what, name)


def _start(
    args: Sequence[str], *, profile: str, ws: str | None, timeout: float, code: str | None
) -> StepResult:
    """Start the browser tool once, and report what it did."""
    try:
        proc = subprocess.run(
            [config.BU_CLI, *args],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env.child_env(profile, ws),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return StepResult(-1, out, err, True)
    except OSError as exc:
        raise BrowserError(f"the browser tool could not be started: {exc}") from exc
    return StepResult(proc.returncode, proc.stdout or "", proc.stderr or "", False)


def run_browser_attempt(
    code: str,
    *,
    profile: str,
    ws: str | None,
    timeout: float = 120.0,
    what: str = "probe",
    name: str | None = None,
) -> StepResult:
    """Run code through the browser tool and report what happened.

    ws is the address of the browser behind the door. It is always given: a caller that
    could forget it would let the tool find a browser of its own, which is the client-door
    defect. None is a deliberate choice that only the personal door makes, when Chrome has
    published no address.

    Raises BrowserError only when the tool could not be started at all.
    """
    return run_browser_tool(
        [], profile=profile, ws=ws, timeout=timeout, code=code, what=what, name=name
    )


def run_browser_command(
    code: str,
    *,
    profile: str,
    ws: str | None,
    timeout: float = 120.0,
    what: str = "probe",
    name: str | None = None,
) -> StepResult:
    """Run Seeress's own step. Raise BrowserError unless it ran cleanly and exited zero."""
    result = run_browser_attempt(
        code, profile=profile, ws=ws, timeout=timeout, what=what, name=name
    )
    if result.timed_out:
        raise BrowserError(f"the browser tool did not finish within {timeout:g} s")
    if result.returncode != 0:
        tail = result.stderr.strip()[-REASON_CAP:]
        raise BrowserError(f"the browser tool exited with code {result.returncode}: {tail}")
    return result
