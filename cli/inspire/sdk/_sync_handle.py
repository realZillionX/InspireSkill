"""Helpful diagnostics for accidentally awaiting a synchronous submission."""

from collections.abc import Generator
from typing import Any


class SyncHandle:
    def __await__(self) -> Generator[Any, None, Any]:
        raise TypeError(
            "Synchronous handles cannot be awaited. Use client.jobs.wait(handle.ref) "
            "(or the matching facade wait), or create/bind the handle with "
            "InspireAsyncClient and await that async handle."
        )
