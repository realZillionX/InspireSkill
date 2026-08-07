"""Helpers for tests that prove cross-process serialization.

These tests need genuinely separate processes: the locks under test are
``flock``-based, so threads inside one interpreter would not exercise them the
way concurrent CLI invocations do.

They use the ``spawn`` start method deliberately. ``fork`` would let workers
inherit the parent's monkeypatched state, but forking a pytest process that has
already touched a threaded library aborts the child on macOS, which made the
whole suite order-dependent. Each worker therefore re-establishes its own
environment from the arguments it is passed.
"""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol


class Barrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


class _CounterLock(Protocol):
    def __enter__(self) -> None: ...

    def __exit__(self, *_args) -> None: ...  # noqa: ANN002


class Counter(Protocol):
    value: int

    def get_lock(self) -> _CounterLock: ...


def worker_context() -> Any:
    """Return the multiprocessing context every concurrency test shares."""
    return multiprocessing.get_context("spawn")


def run_workers(
    context: Any,
    target: Callable[..., None],
    *,
    count: int,
    args_for: Callable[[int], Sequence[Any]],
    timeout: float = 60.0,
) -> list[int | None]:
    """Run *count* workers concurrently and return their exit codes.

    *timeout* is the budget for the whole fleet, not per worker. Anything still
    alive past it is terminated so a wedged worker fails the assertion on exit
    codes instead of leaking into the rest of the session.
    """
    workers = [
        context.Process(target=target, args=tuple(args_for(index))) for index in range(count)
    ]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
    return [worker.exitcode for worker in workers]
