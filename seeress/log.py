"""How Seeress speaks to the person.

What the library has to say is logged under one logger, `seeress`, and reaches the person
on standard error, a message on a line. The library prints nothing: what a command returns
is printed by the command line, and what is reported while it runs is logged.
"""

import logging
import sys

FORMAT = "%(message)s"


def to_stderr() -> None:
    """See that what is logged under `seeress` reaches standard error, a message on a line.

    Once a process: called again, it changes nothing. What is only noted in debug is not
    shown.
    """
    logger = logging.getLogger("seeress")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
