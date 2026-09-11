"""Resumable HTTP workflows shared by the blocking and native async drivers.

A workflow yields calls, receiving either their result or their original
exception. Branches, exception boundaries and credential guards run once in
this description; interpreters supply the I/O.
"""

from __future__ import annotations

import asyncio
from greenlet import greenlet, getcurrent

from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Generator, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
Program = Generator["Call", Any, T]


@dataclass
class Call:
    function: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    http: bool = False
    blocking: bool = False


def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    return Call(function, args, kwargs)


def http_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    return Call(function, args, kwargs, http=True)


async_call: ContextVar[Callable[[Call], Any] | None] = ContextVar("inspire_async_call", default=None)


def workflow(function: Callable[P, Program[T]]) -> Callable[P, T]:
    @wraps(function)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        bridge = async_call.get()
        if bridge is not None:
            return bridge(call(execute, *args, **kwargs))
        return drive_program(function(*args, **kwargs))

    execute.__workflow__ = function  # type: ignore[attr-defined]
    return execute


def program_for(action: Call) -> Program[Any] | None:
    function = action.function
    program = getattr(function, "__workflow__", None)
    if program is None:
        return None
    owner = getattr(function, "__self__", None)
    args = (owner, *action.args) if owner is not None else action.args
    return program(*args, **action.kwargs)


def enter_context(context: Any) -> Any:
    return context.__enter__()


def exit_context(context: Any, *error: Any) -> Any:
    return context.__exit__(*error)


def perform_sync(action: Call) -> Any:
    bridge = async_call.get()
    if bridge is not None:
        return bridge(action)
    if not action.http:
        return action.function(*action.args, **action.kwargs)
    from inspire.platform.web.runtime import active_transport

    owner = active_transport.get()
    if owner is not None:
        owner.check_deadline()
    try:
        return action.function(*action.args, **action.kwargs)
    finally:
        if owner is not None:
            owner.check_deadline()


def blocking_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    """An indivisible local I/O operation; async drivers finish it off the loop."""
    return Call(function, args, kwargs, blocking=True)


def blocking_io(function: Callable[P, T]) -> Callable[P, T]:
    """Keep one implementation of local I/O for both execution modes."""
    @wraps(function)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        return perform_sync(blocking_call(function, *args, **kwargs))
    return execute


def drive_program(program: Program[T]) -> T:
    try:
        action = next(program)
        while True:
            try:
                result = perform_sync(action)
            except BaseException as error:
                action = program.throw(error)
            else:
                action = program.send(result)
    except StopIteration as done:
        return done.value
    finally:
        program.close()


async def run_sync(driver: Any, function: Callable[[], Any]) -> Any:
    """A stack switch keeps business logic on the caller loop; I/O may be offloaded.

    Each suspended stack keeps its own ContextVars. The I/O interpreter inherits
    those values, but disables the bridge while interpreting native workflows.
    Cancellation is thrown back through the original synchronous finally blocks.
    """
    parent = getcurrent()

    def invoke() -> Any:
        token = async_call.set(parent.switch)
        try:
            return function()
        finally:
            async_call.reset(token)

    child = greenlet(invoke)
    child.gr_context = copy_context()
    action = child.switch()
    while not child.dead:
        context = child.gr_context.copy()
        context.run(async_call.set, None)
        task = context.run(asyncio.create_task, driver.execute(action))
        try:
            result = await task
        except BaseException as error:
            action = child.throw(error)
        else:
            action = child.switch(result)
    return action
