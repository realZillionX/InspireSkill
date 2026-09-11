"""Native waiting for the same cross-process authentication locks."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Generator, cast

from inspire.accounts import cache_lock
from inspire.services.execution.async_output import _finish_io


@asynccontextmanager
async def cache_lock_async(path: Any, transport: Any) -> AsyncIterator[None]:
    descriptor = None

    def open_lock() -> None:
        nonlocal descriptor
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

    acquired = False
    try:
        await _finish_io(open_lock)
        assert descriptor is not None
        while not await _finish_io(cache_lock._try_acquire, descriptor):
            await asyncio.sleep(min(0.05, transport.remaining()))
        acquired = True
        transport.check_deadline()
        yield
    finally:
        if acquired:
            await _finish_io(cache_lock._release, descriptor)
        if descriptor is not None:
            await _finish_io(os.close, descriptor)


@asynccontextmanager
async def authentication_context(context: Any, transport: Any) -> AsyncIterator[None]:
    from inspire.platform.web.session import login_guard, refresh_lock

    name = context.func.__name__
    if name == "exclusive_session_refresh":
        account = context.args[0] if context.args else context.kwds.get("account")
        cache_file = await _finish_io(refresh_lock.get_session_cache_file, account)
        if cache_file is None or not await _finish_io(cache_file.parent.is_dir):
            yield
            return
        async with cache_lock_async(cache_file.with_name(f"{cache_file.name}.refresh"), transport):
            yield
        return
    if name != "guarded_credential_submission":
        # Test/integration contexts may supply a no-op context manager.
        with context:
            yield
        return
    username, password = context.args
    account = context.kwds["account"]
    path = await _finish_io(login_guard.block_file, account)
    if path is None:
        yield
        return
    async with cache_lock_async(path, transport):
        # `_guarded` is the single cooldown/fingerprint decision program.
        guard = cast(
            Generator[None, Any, None],
            login_guard._guarded(
                username,
                password,
                path,
                account=account,
                now=context.kwds.get("now"),
            ),
        )
        await _finish_io(next, guard, None)
        try:
            yield
        except BaseException as error:
            try:
                await _finish_io(_throw_guard, guard, error)
            except StopIteration:
                pass
            raise
        else:
            try:
                await _finish_io(next, guard, None)
            except StopIteration:
                pass
        finally:
            await _finish_io(guard.close)


def _throw_guard(guard: Generator[None, Any, None], error: BaseException) -> None:
    try:
        guard.throw(error)
    except StopIteration:
        pass
