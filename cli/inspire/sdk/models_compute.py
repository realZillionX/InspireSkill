"""Typed HPC and Ray specs, plans, handles and observations."""

from __future__ import annotations

from ._sync_handle import SyncHandle
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar
from .models import (
    Image,
    ResourceRef,
    WorkspaceRef,
    ProjectRef,
    ComputeGroupRef,
    ImageRef,
    ImageSelector,
    QuotaRef,
    Quota,
    DatasetMount,
    Resource,
)


class HPCJobRef(ResourceRef):
    kind = "hpc"


class RayJobRef(ResourceRef):
    kind = "ray"


R = TypeVar("R", bound=ResourceRef)


@dataclass(frozen=True)
class WorkloadJob(Generic[R]):
    name: str
    ref: R
    status: str
    raw_status: str
    project: str = ""
    created_at: str = ""
    finished_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    view: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.view)


@dataclass(frozen=True)
class HPCJob(WorkloadJob[HPCJobRef]):
    pass


@dataclass(frozen=True)
class RayJob(WorkloadJob[RayJobRef]):
    pass


@dataclass(frozen=True)
class HPCJobCreateSpec:
    name: str
    entrypoint: str = field(repr=False)
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    quota: str | Quota | QuotaRef
    image: str | ImageRef | ImageSelector
    image_type: str = "SOURCE_PRIVATE"
    instance_count: int = 1
    priority: int | None = None
    number_of_tasks: int = 1
    cpus_per_task: int | None = None
    memory_per_cpu: int | None = None
    enable_hyper_threading: bool = False
    max_time_hours: float | None = None
    keep_after_finish_hours: float | None = None
    datasets: list[str | DatasetMount] = field(default_factory=list)
    description: str | None = None
    enable_notification: bool = False
    public_path_readonly: bool | None = None


@dataclass(frozen=True)
class RayJobCreateSpec:
    name: str
    command: str = field(repr=False)
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    quota: str | Quota | QuotaRef
    image: str | ImageRef | ImageSelector
    image_type: str = "SOURCE_PUBLIC"
    description: str = ""
    priority: int | None = None
    shm_gib: int | None = None
    workers: list[str] = field(default_factory=list)
    public_path_readonly: bool | None = None


@dataclass(frozen=True)
class WorkloadPlan:
    name: str
    workspace: Resource[WorkspaceRef]
    project: Resource[ProjectRef]
    group: Resource[ComputeGroupRef]
    quota: Quota
    image: Image
    priority: int
    create_kwargs: dict[str, Any] = field(repr=False)
    payload: dict[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    @property
    def summary(self) -> str:
        return (
            f"{self.name}: {self.workspace.name}; project={self.project.name}; "
            f"group={self.group.name}; quota={self.quota}; image={self.image.name}; "
            f"priority={self.priority}; no resources reserved."
        )


@dataclass(frozen=True)
class HPCJobPlan(WorkloadPlan):
    instance_count: int
    number_of_tasks: int
    cpus_per_task: int
    memory_per_cpu: int
    datasets: tuple[DatasetMount, ...]


@dataclass(frozen=True)
class RayJobPlan(WorkloadPlan):
    workers: tuple[dict[str, Any], ...]
    shm_gib: int | None


@dataclass(frozen=True)
class HPCJobHandle(SyncHandle):
    name: str
    ref: HPCJobRef
    operation_id: str
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class RayJobHandle(SyncHandle):
    name: str
    ref: RayJobRef
    operation_id: str
    status: str = "UNKNOWN"
