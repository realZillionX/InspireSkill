"""Serving, TensorBoard and registry creation values."""

from __future__ import annotations

from ._sync_handle import SyncHandle
from dataclasses import dataclass, field
from .models import (
    ResourceRef,
    WorkspaceRef,
    ProjectRef,
    ComputeGroupRef,
    Quota,
    QuotaRef,
    ImageRef,
    ImageSelector,
    JobRef,
)
from .models_resources import ModelRef
from .models_compute import WorkloadJob, WorkloadPlan


class ServingRef(ResourceRef):
    kind = "serving"


class TensorboardRef(ResourceRef):
    kind = "tensorboard"


@dataclass(frozen=True)
class Serving(WorkloadJob[ServingRef]):
    pass


@dataclass(frozen=True)
class ServingCreateSpec:
    name: str
    model: str | ModelRef
    command: str = field(repr=False)
    port: int
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    quota: str | Quota | QuotaRef
    image: str | ImageRef | ImageSelector
    model_version: int | None = None
    replicas: int = 1
    nodes_per_replica: int = 1
    shm_gib: int | None = None
    priority: int | None = None
    custom_domain: str | None = None
    description: str = ""
    auto_scaling: bool | None = None
    public_path_readonly: bool | None = None


@dataclass(frozen=True)
class ServingPlan(WorkloadPlan):
    model: str
    model_version: int


@dataclass(frozen=True)
class ServingHandle(SyncHandle):
    name: str
    ref: ServingRef
    operation_id: str


@dataclass(frozen=True)
class TensorboardCreateSpec:
    name: str
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    summary_path: str | None = None
    job: str | JobRef | None = None
    auto_stop_hours: float | None = None


@dataclass(frozen=True)
class Tensorboard:
    name: str
    ref: TensorboardRef
    status: str
    summary_path: str
    url: str
    job: str
    project: str
    compute_group: str
    auto_stop_ms: str
    running_time_ms: str
    created_at: str
    job_id: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {k: v for k, v in asdict(self).items() if k not in {"ref", "job_id"}}


@dataclass(frozen=True)
class TensorboardHandle(SyncHandle):
    name: str
    ref: TensorboardRef
    operation_id: str


@dataclass(frozen=True)
class ImageRegisterHandle(SyncHandle):
    name: str
    ref: ImageRef
    operation_id: str
    registry: str = ""


@dataclass(frozen=True)
class ModelRegisterHandle:
    name: str
    ref: ModelRef
    operation_id: str
