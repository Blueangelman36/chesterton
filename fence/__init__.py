"""Remember why code exists, and speak up before it's removed."""

__version__ = "0.1.0"


class FenceError(Exception):
    """A problem worth showing the user as-is, without a traceback."""
