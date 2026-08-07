from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Protocol

from inspire.bridge.tunnel import BridgeProfile
from inspire.cli.commands.notebook.target_resolver import remember_notebook_target

WORKER_COUNT = 16


class _Barrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


def _write_target(index: int, home: str, barrier: _Barrier) -> None:
    os.environ["HOME"] = home
    bridge = BridgeProfile(
        name=f"bench-{index}",
        proxy_url=f"https://proxy.invalid/{index}",
        notebook_name=f"bench-{index}",
        notebook_id=f"notebook-{index}",
        workspace_name="CPU资源空间",
    )
    barrier.wait()
    remember_notebook_target(
        notebook=f"bench-{index}",
        workspace="CPU资源空间",
        account="default",
        bridge=bridge,
    )


def test_concurrent_target_cache_writes_preserve_every_entry(tmp_path: Path) -> None:
    # Given: independent CLI processes share one notebook target cache.
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(WORKER_COUNT)
    workers = [
        context.Process(
            target=_write_target,
            args=(index, str(tmp_path), barrier),
        )
        for index in range(WORKER_COUNT)
    ]

    # When: every process resolves and remembers a target at the same time.
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()

    # Then: no writer crashes, and every independent entry remains cached.
    assert [worker.exitcode for worker in workers] == [0] * WORKER_COUNT
    cache_path = tmp_path / ".inspire" / "notebook-targets.json"
    data = json.loads(cache_path.read_text())
    assert set(data["targets"]) == {
        f"bench-{index}|workspace=CPU资源空间" for index in range(WORKER_COUNT)
    }
