"""Seeress command line.

This module parses arguments, prints what the library returns, and turns a SeeressError
into a reason on stderr and exit status 1. It holds no logic of its own.

COMMANDS_HELP is what `seeress --help` prints: every command, and how the door is chosen.
"""

import argparse
import contextlib
import os
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from . import bodies, capture, config, flows, log, mine, watcher
from .agent import DEFAULT_MAX_STEPS, chat_session, run_task
from .errors import BrowserError, SeeressError

Command = Callable[[Sequence[str]], int]

COMMANDS_HELP = """Seeress: your personal browser agent.

  seeress "do a thing"                    one task
  seeress chat [--companion FILE]         talk, with history
  seeress run <flow>                      replay a saved flow
  seeress create-flow <name> <words...>   the agent explores, then writes the flow
  seeress flows                           list saved flows
  seeress schedule <flow> [--daily HH:MM | --every N]
  seeress reload                          restart the browser tool's daemon for a door
  seeress watch-start | watch-stop        the activity logger
  seeress watch-analyze [--hours N] [--no-llm]
  seeress mine [--hours N] [--no-llm]     what repeats in the record, as proposals
  seeress proposal show NAME              the evidence, and what the model thinks the task is
  seeress proposal goal NAME "..."        your own words for the goal. This approves it
  seeress proposal compile NAME           the agent writes the flow, on the personal door
  seeress proposal dismiss NAME           set a proposal aside
  seeress progress                       what was proposed, compiled and run
  seeress capture-review                 what the record holds, day by day
  seeress capture-forget --before DATE [--yes]
                                         delete the record from before a day
  seeress capture-status                 whether the capture is recording

--profile personal or client selects the door. It goes before or after the command, and
beats SEERESS_PROFILE, which beats the default.
"""

# Options that take their value as the next word.
VALUE_OPTIONS = ("--profile",)


def _parser(command: str | None) -> argparse.ArgumentParser:
    """A parser for one command, or for a bare task when command is None."""
    usage = "seeress" if command is None else f"seeress {command}"
    parser = argparse.ArgumentParser(
        prog=usage,
        # The help of the whole tool lists every command. That of one command does not.
        description=None if command is not None else COMMANDS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        default=None,
        choices=tuple(config.PROFILES),
        help="which door: personal or client. Default: SEERESS_PROFILE, else personal.",
    )
    return parser


def _door(args: argparse.Namespace) -> str:
    return args.profile or os.environ.get("SEERESS_PROFILE") or config.DEFAULT_PROFILE


def _task(argv: Sequence[str]) -> int:
    parser = _parser(None)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("task", nargs="+", help="what to do in the browser")
    args = parser.parse_args(argv)
    print(run_task(" ".join(args.task), _door(args), args.max_steps))
    return 0


def _chat(argv: Sequence[str]) -> int:
    parser = _parser("chat")
    parser.add_argument("--companion", type=Path, default=None)
    args = parser.parse_args(argv)
    chat_session(_door(args), companion=args.companion, read=lambda: input("you> "), write=print)
    return 0


def _run(argv: Sequence[str]) -> int:
    parser = _parser("run")
    parser.add_argument("name")
    args = parser.parse_args(argv)
    print(flows.run_flow(args.name, profile=_door(args)))
    return 0


def _create_flow(argv: Sequence[str]) -> int:
    parser = _parser("create-flow")
    parser.add_argument("name")
    parser.add_argument("description", nargs="+")
    args = parser.parse_args(argv)
    door = _door(args)
    path = flows.create_flow(args.name, " ".join(args.description), profile=door)
    print(f"flow saved: {path}")
    print(f"run it:  bin/seeress-{door} run {args.name}")
    return 0


def _flows(argv: Sequence[str]) -> int:
    _parser("flows").parse_args(argv)
    saved = flows.list_flows()
    if not saved:
        print("no flows yet. Create one:  seeress create-flow my-flow <description>")
        return 0
    for path in saved:
        first = path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        print(f"{path.stem:24} {first}")
    return 0


def _schedule(argv: Sequence[str]) -> int:
    parser = _parser("schedule")
    parser.add_argument("name")
    parser.add_argument("--every", type=int, default=None, help="every N minutes")
    parser.add_argument("--daily", default=None, help="daily at HH:MM, 24 hours")
    args = parser.parse_args(argv)
    plist = flows.schedule_plist(args.name, every_minutes=args.every, daily=args.daily)
    print(f"saved {plist}. It is not loaded yet. To run it:  launchctl load {plist}")
    return 0


def _reload(argv: Sequence[str]) -> int:
    args = _parser("reload").parse_args(argv)
    door = _door(args)
    if not bodies.reload_daemon(door, None):
        raise BrowserError(f"the browser tool's daemon for the {door} door could not be stopped")
    print(
        f"the browser tool's daemon for the {door} door was stopped. "
        "It starts again on the next call."
    )
    return 0


def _watch_start(argv: Sequence[str]) -> int:
    _parser("watch-start").parse_args(argv)
    print(watcher.start())
    return 0


def _watch_stop(argv: Sequence[str]) -> int:
    _parser("watch-stop").parse_args(argv)
    print(watcher.stop())
    return 0


def _watch_analyze(argv: Sequence[str]) -> int:
    parser = _parser("watch-analyze")
    parser.add_argument("--hours", type=float, default=None, help="the last N hours only")
    parser.add_argument("--no-llm", action="store_true", help="skip the local model's proposals")
    args = parser.parse_args(argv)
    print(watcher.analyze(hours=args.hours, no_llm=args.no_llm))
    return 0


def _mine(argv: Sequence[str]) -> int:
    parser = _parser("mine")
    parser.add_argument("--hours", type=float, default=None, help="the last N hours only")
    parser.add_argument(
        "--no-llm", action="store_true", help="carry the evidence only, and ask no model"
    )
    args = parser.parse_args(argv)
    print(mine.run(args.hours, no_llm=args.no_llm))
    return 0


def _proposal(argv: Sequence[str]) -> int:
    parser = _parser("proposal")
    parser.add_argument("action", choices=("show", "goal", "dismiss", "compile"))
    parser.add_argument("name")
    parser.add_argument("goal", nargs="?", default=None, help="the goal, in quotes")
    args = parser.parse_args(argv)
    if args.action == "show":
        print(mine.show(args.name))
    elif args.action == "goal":
        print(mine.set_goal(args.name, args.goal))
    elif args.action == "dismiss":
        print(mine.dismiss(args.name))
    else:
        print(flows.compile_proposal(None, args.name))
        print(f"Run it with: seeress run {args.name}")
    return 0


def _capture_review(argv: Sequence[str]) -> int:
    _parser("capture-review").parse_args(argv)
    print(capture.review())
    return 0


def _capture_forget(argv: Sequence[str]) -> int:
    parser = _parser("capture-forget")
    parser.add_argument("--before", required=True, help="a day, YYYY-MM-DD: the record before it")
    parser.add_argument(
        "--yes", action="store_true", help="delete them. Without it, only say what would go"
    )
    args = parser.parse_args(argv)
    print(capture.forget(None, args.before, yes=args.yes))
    return 0


def _capture_status(argv: Sequence[str]) -> int:
    _parser("capture-status").parse_args(argv)
    print(capture.status())
    return 0


def _progress(argv: Sequence[str]) -> int:
    _parser("progress").parse_args(argv)
    print(mine.progress())
    return 0


COMMANDS: dict[str, Command] = {
    "chat": _chat,
    "run": _run,
    "create-flow": _create_flow,
    "flows": _flows,
    "schedule": _schedule,
    "reload": _reload,
    "watch-start": _watch_start,
    "watch-stop": _watch_stop,
    "watch-analyze": _watch_analyze,
    "mine": _mine,
    "proposal": _proposal,
    "progress": _progress,
    "capture-review": _capture_review,
    "capture-forget": _capture_forget,
    "capture-status": _capture_status,
}


def _split(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """The command, and every argument the parser for it should see.

    Options may come before the command (`seeress --profile client run alpha`). They are
    handed on, so the command's parser sees them too.
    """
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        index += 2 if argv[index] in VALUE_OPTIONS else 1
    if index < len(argv) and argv[index] in COMMANDS:
        return argv[index], [*argv[:index], *argv[index + 1 :]]
    return None, list(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the exit status.

    A signal that asks the process to end is turned into an ordinary exit, so that whatever
    is in hand is closed and noted before the process goes, as when an error ends it. Until
    2026-11-25 closing the terminal ended the process with no note of a run of the agent,
    and its minutes were taken for the person's.
    """
    args = list(sys.argv[1:] if argv is None else argv) or ["chat"]
    command, rest = _split(args)
    log.to_stderr()
    with _ending_cleanly():
        try:
            return _task(rest) if command is None else COMMANDS[command](rest)
        except SeeressError as exc:
            print(f"seeress error: {exc}", file=sys.stderr)
            return 1
        except Stopped:
            print("seeress: stopped. Whatever was in hand is noted as ended.", file=sys.stderr)
            return 1


# The signals by which a process is asked to end. Closing a terminal sends the second.
ENDING_SIGNALS = tuple(
    number
    for number in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
    if number is not None
)

Handler = int | Callable[[int, FrameType | None], Any] | None


class Stopped(BaseException):
    """A signal asked the process to end. It is raised where the program is, so that
    whatever is in hand is closed and noted. It is not an Exception: nothing catches it by
    accident on the way up."""


def _stopped(signum: int, frame: FrameType | None) -> None:
    raise Stopped(signum)


@contextlib.contextmanager
def _ending_cleanly() -> Iterator[None]:
    """While it holds, a signal that asks the process to end raises Stopped where the
    program is. The handlers there were before are put back after."""
    before: dict[int, Handler] = {}
    try:
        for number in ENDING_SIGNALS:
            before[number] = signal.signal(number, _stopped)
    except ValueError:  # not the main thread, where signals cannot be caught
        pass
    try:
        yield
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
