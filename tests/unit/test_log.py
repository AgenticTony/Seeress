"""How Seeress speaks to the person: one logger, to standard error, a message on a line."""

import logging

import pytest

from seeress import log


def test_what_is_logged_reaches_standard_error_a_message_on_a_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("seeress").handlers.clear()  # a fresh stream for this test

    log.to_stderr()

    logging.getLogger("seeress.agent").info("step 1: print(page_info())")

    assert capsys.readouterr().err == "step 1: print(page_info())\n"


def test_it_is_set_up_once_a_process() -> None:
    logger = logging.getLogger("seeress")
    logger.handlers.clear()

    log.to_stderr()
    log.to_stderr()

    assert len(logger.handlers) == 1
    assert logger.level == logging.INFO


def test_what_is_only_noted_in_debug_is_not_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = logging.getLogger("seeress")
    logger.handlers.clear()

    log.to_stderr()
    logger.debug("-> the first two hundred letters of a result")

    assert capsys.readouterr().err == ""
