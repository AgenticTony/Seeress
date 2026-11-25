"""Errors Seeress reports to the user as a reason, instead of a traceback.

Every error the command line handles derives from SeeressError. The command line exits
non-zero on each of them.
"""


class SeeressError(Exception):
    """Base class for every error the command line reports as a reason."""


class ConfigError(SeeressError):
    """The settings file cannot be read, or asks for something Seeress will not do."""


class GuardViolation(SeeressError):
    """No model endpoint is permitted for the page the browser is on."""


class IsolationError(SeeressError):
    """The browser behind a door is not the one the door allows."""


class UnsafeCodeError(SeeressError):
    """Code from the model or from a flow uses something Seeress does not allow."""


class BrowserError(SeeressError):
    """The browser tool could not run a step, or did not finish in time."""


class BrainError(SeeressError):
    """A model endpoint could not be reached, or gave an unusable answer."""


class FlowError(SeeressError):
    """A saved flow is missing or cannot be used."""


class CaptureError(SeeressError):
    """The capture cannot run, or was asked to do what it must not."""


class MiningError(SeeressError):
    """The activity record could not be mined, or a proposal is not in a state a command
    needs."""
