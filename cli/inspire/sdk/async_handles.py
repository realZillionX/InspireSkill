"""Client-bound polling handles; persist their pure-data refs, not the handles.

Awaiting a handle starts a fresh wait through its facade, even after a previous
wait finished. future() schedules that wait on the running loop; it is not a
shared completion future. Binding a stored ref validates local account/server
identity without submitting work or proving that the resource still exists.

Cancelling a wait stops observation only. Workloads may keep running and charging;
stop them explicitly through their workload facade. Image-save cancellation is a
separate notebooks.cancel_save_image operation, and Images has no stop method.
Workload waits return failures by default; image readiness waits instead use the
image API's error contract and have no raise_on_failure switch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Generic, TYPE_CHECKING, TypeVar

from .models import JobHandle, Job, WorkspaceRef
from .models_compute import HPCJobHandle, HPCJob, RayJobHandle, RayJob
from .models_notebooks import NotebookHandle, Notebook, ImageSaveHandle
from .models_serving import (
    ServingHandle,
    Serving,
    TensorboardHandle,
    Tensorboard,
    ImageRegisterHandle,
)
from inspire.platform.web.browser_api.images import CustomImageInfo

if TYPE_CHECKING:
    from . import async_client

T = TypeVar("T")


class _AwaitableHandle(Generic[T]):
    async def wait(self) -> T:
        raise NotImplementedError

    def __await__(self) -> Generator[Any, None, T]:
        """Start a fresh facade wait with its defaults and request budgets.

        Cancellation stops this wait, not the submitted workload or image work.
        """
        return self.wait().__await__()

    def future(self) -> asyncio.Future[T]:
        """Schedule a fresh wait on the running loop (no cached/shared future).

        Suitable for asyncio.wait/as_completed. Cancelling this future only stops
        waiting: submitted work may continue, including billable workloads.
        This is an asyncio Future, not a concurrent.futures.Future for threads.
        """
        return asyncio.get_running_loop().create_task(self.wait())


@dataclass(frozen=True)
class AsyncJobHandle(_AwaitableHandle[Job], JobHandle):
    """Awaitable jobs submission; cancellation never stops remote work."""

    _facade: async_client.AsyncJobs = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        workspace: str | WorkspaceRef | None = None,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
    ) -> Job:
        """Poll via inspire.sdk.jobs.Jobs.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            workspace=workspace,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )


@dataclass(frozen=True)
class AsyncNotebookHandle(_AwaitableHandle[Notebook], NotebookHandle):
    """Awaitable notebooks submission; cancellation never stops remote work."""

    _facade: async_client.AsyncNotebooks = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        target: str = "RUNNING",
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> Notebook:
        """Poll via inspire.sdk.notebooks.Notebooks.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            target=target,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncHPCJobHandle(_AwaitableHandle[HPCJob], HPCJobHandle):
    """Awaitable hpc submission; cancellation never stops remote work."""

    _facade: async_client.AsyncHPC = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> HPCJob:
        """Poll via inspire.sdk.hpc.HPC.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncRayJobHandle(_AwaitableHandle[RayJob], RayJobHandle):
    """Awaitable ray submission; cancellation never stops remote work."""

    _facade: async_client.AsyncRay = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> RayJob:
        """Poll via inspire.sdk.ray.Ray.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncServingHandle(_AwaitableHandle[Serving], ServingHandle):
    """Awaitable servings submission; cancellation never stops remote work."""

    _facade: async_client.AsyncServings = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
        target: str = "RUNNING",
    ) -> Serving:
        """Poll via inspire.sdk.servings.Servings.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
            workspace=workspace,
            target=target,
        )


@dataclass(frozen=True)
class AsyncTensorboardHandle(_AwaitableHandle[Tensorboard], TensorboardHandle):
    """Awaitable tensorboards submission; cancellation never stops remote work."""

    _facade: async_client.AsyncTensorboards = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        target: str = "RUNNING",
        raise_on_failure: bool = False,
        timeout: float = 60,
        poll_interval: float = 3,
        workspace: str | WorkspaceRef | None = None,
    ) -> Tensorboard:
        """Poll via inspire.sdk.tensorboards.Tensorboards.wait with the same defaults and budgets.

        Cancelling only stops this wait; stop the workload explicitly if needed.
        Failed workloads return normally unless raise_on_failure=True.
        Each call waits again, including after a previous terminal result.
        """
        return await self._facade.wait(
            self.ref,
            target=target,
            raise_on_failure=raise_on_failure,
            timeout=timeout,
            poll_interval=poll_interval,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncImageSaveHandle(_AwaitableHandle[CustomImageInfo], ImageSaveHandle):
    """Observe a saved image becoming ready; this does not wait for the notebook."""

    _facade: async_client.AsyncNotebooks = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        workspace: str | WorkspaceRef | None = None,
    ) -> CustomImageInfo:
        """Poll inspire.sdk.notebooks.Notebooks.wait_image_ready again.

        An unconfirmed image ref raises ValidationError; waiting cannot recover
        the identity. Cancellation only stops polling. Cancelling the save is a
        separate notebooks.cancel_save_image(self.notebook) operation.
        """
        return await self._facade.wait_image_ready(
            self,
            timeout=timeout,
            poll_interval=poll_interval,
            workspace=workspace,
        )


@dataclass(frozen=True)
class AsyncImageRegisterHandle(_AwaitableHandle[CustomImageInfo], ImageRegisterHandle):
    """Observe a registered image becoming ready; registration does not push bytes."""

    _facade: async_client.AsyncImages = field(repr=False, compare=False, kw_only=True)

    async def wait(
        self,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        workspace: str | WorkspaceRef | None = None,
    ) -> CustomImageInfo:
        """Poll inspire.sdk.resources.Images.wait_ready again.

        Cancellation stops observation, not the image push. Images has no stop
        method, and readiness failures follow wait_ready's error contract.
        """
        return await self._facade.wait_ready(
            self.ref,
            timeout=timeout,
            poll_interval=poll_interval,
            workspace=workspace,
        )
