"""Seeress command line.

This module parses arguments, prints what the library returns, and turns a SeeressError
into a reason on stderr and exit status 1. It holds no logic of its own.

  seeress "do a thing"                    one task
  seeress chat [--companion FILE]         talk, with history
  seeress run <flow>                      replay a saved flow
  seeress create-flow <name> <words...>   the agent explores, then writes the flow
  seeress flows                           list saved flows
  seeress schedule <flow> [--daily HH:MM | --every N]
  seeress reload                          restart the browser tool's daemon for a door
  seeress watch-start | watch-stop        the activity logger
  seeress watch-analyze [--hours N] [--no-llm]

--profile personal or client selects the door. It goes before or after the command, and
beats SEERESS_PROFILE, which beats the default.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import bodies, config, flows, watcher
from .agent import DEFAULT_MAX_STEPS, chat_session, run_task
from .errors import BrowserError, SeeressError

Command = Callable[[Sequence[str]], int]

# Options that take their value as the next word.
VALUE_OPTIONS = ("--profile",)
LOG_FORMAT = "%(message)s"


def _parser(command: str | None) -> argparse.ArgumentParser:
    """A parser for one command, or for a bare task when command is None."""
    usage = "seeress" if command is None else f"seeress {command}"
    parser = argparse.ArgumentParser(prog=usage)
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


def _logging() -> None:
    logger = logging.getLogger("seeress")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the exit status."""
    args = list(sys.argv[1:] if argv is None else argv) or ["chat"]
    command, rest = _split(args)
    _logging()
    try:
        return _task(rest) if command is None else COMMANDS[command](rest)
    except SeeressError as exc:
        print(f"seeress error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
