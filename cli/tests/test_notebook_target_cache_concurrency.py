from __future__ import annotations

import json
from pathlib import Path

from inspire.bridge.tunnel import BridgeProfile
from inspire.cli.commands.notebook.target_resolver import remember_notebook_target
from multiprocess_workers import Barrier, adopt_home, run_workers, worker_context

WORKER_COUNT = 16


def _write_target(index: int, home: str, barrier: Barrier) -> None:
    adopt_home(home)
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
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)

    # When: every process resolves and remembers a target at the same time.
    exit_codes = run_workers(
        context,
        _write_target,
        count=WORKER_COUNT,
        args_for=lambda index: (index, str(tmp_path), barrier),
    )

    # Then: no writer crashes, and every independent entry remains cached.
    assert exit_codes == [0] * WORKER_COUNT
    cache_path = tmp_path / ".inspire" / "notebook-targets.json"
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(data["targets"]) == {
        f"bench-{index}|workspace=CPU资源空间" for index in range(WORKER_COUNT)
    }
