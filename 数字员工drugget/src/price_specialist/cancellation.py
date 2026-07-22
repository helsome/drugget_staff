"""Cancellation token for cooperative cancellation across async worker threads."""

from __future__ import annotations

import threading


class CancelledError(Exception):
    """Raised when a cancellation is requested during an async operation."""
    pass


class CancellationToken:
    """Thread-safe cancellation token for cooperative cancellation.

    Usage:
        ct = CancellationToken()
        # In worker thread:
        while not ct.is_cancelled:
            do_work()
        # Or:
        ct.raise_if_cancelled()  # raises CancelledError
        # In main thread:
        ct.cancel()
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. This is idempotent and thread-safe."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise CancelledError if cancellation has been requested."""
        if self._event.is_set():
            raise CancelledError()