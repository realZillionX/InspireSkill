"""Notebook specs, resolved plans and platform observations."""

from __future__ import annotations

from ._sync_handle import SyncHandle
from dataclasses import dataclass, field, asdict
from typing import Any
from .models import (
    Resource,
    Image,
    ResourceRef,
    WorkspaceRef,
    ProjectRef,
    ComputeGroupRef,
    ImageRef,
    QuotaRef,
    Quota,
    ImageSelector,
    DatasetMount,
)


class NotebookRef(ResourceRef):
    kind = "notebook"


@dataclass(frozen=True)
class Notebook:
    name: str
    ref: NotebookRef
    status: str
    raw_status: str
    workspace: str = ""
    project: str = ""
    image: str = ""
    compute_group: str = ""
    created_by: str = ""
    resource: dict[str, Any] = field(default_factory=dict)
    node: dict[str, Any] = field(default_factory=dict)
    priority: int | str | None = None
    priority_level: str = ""
    shared_memory_gib: int | None = None
    uptime_seconds: int | str | None = None
    auto_stop_in_seconds: int | str | None = None
    datasets: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    sub_status: str = ""

    @property
    def quota(self) -> dict[str, Any]:
        return dict(self.resource)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k not in {"ref", "raw_status"}}


@dataclass(frozen=True)
class NotebookCreateSpec:
    name: str
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    quota: str | Quota | QuotaRef
    image: str | ImageRef | ImageSelector
    shm_gib: int | None = None
    auto_stop: bool = False
    auto_stop_after: int | None = None
    datasets: list[str | DatasetMount] = field(default_factory=list)
    enable_notification: bool | None = None
    public_path_readonly: bool | None = None
    project_path_readonly: bool | None = None
    priority: int | None = None
    node: str | None = None


@dataclass(frozen=True)
class NotebookPlan:
    name: str
    workspace: Resource[WorkspaceRef]
    project: Resource[ProjectRef]
    group: Resource[ComputeGroupRef]
    image: Image
    quota: Quota
    priority: int
    shm_gib: int
    auto_stop: bool
    auto_stop_after: int | None
    datasets: tuple[DatasetMount, ...]
    create_kwargs: dict[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return public review fields, keeping the full payload in create_kwargs."""
        return {
            "name": self.name, "workspace": self.workspace.name,
            "project": self.project.name, "compute_group": self.group.name,
            "image": self.image.name, "quota": str(self.quota), "priority": self.priority,
            "shm_gib": self.shm_gib, "auto_stop": self.auto_stop,
            "auto_stop_after": self.auto_stop_after,
            "datasets": [f"{item.dataset}:{item.version}" for item in self.datasets],
        }

    @property
    def summary(self) -> str:
        return (
            f"{self.name}: {self.workspace.name} / {self.project.name} / {self.group.name}; "
            f"image={self.image.name}; {self.quota}; priority={self.priority}; "
            f"shm={self.shm_gib}; auto_stop={self.auto_stop}; "
            f"auto_stop_after={self.auto_stop_after}; datasets={self.datasets}. No resources reserved."
        )


@dataclass(frozen=True)
class NotebookHandle(SyncHandle):
    name: str
    ref: NotebookRef
    operation_id: str
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class ImageSaveHandle(SyncHandle):
    name: str
    ref: ImageRef | None
    notebook: NotebookRef
    status: str = "saving"
    flatten: bool = False
    estimated_size_bytes: int | None = None
    warning: str | None = None
