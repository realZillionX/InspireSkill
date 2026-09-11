"""Frozen values for account and resource discovery.

``to_dict`` returns the shared CLI JSON view, excluding SDK references.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
from typing import Any, TypeVar

from .models import ResourceRef, ProjectRef, ImageRef, ComputeGroupRef
from inspire.platform.web.browser_api.schedule_config import (
    WorkloadSchedulePolicy as WorkloadSchedulePolicy,
)

V = TypeVar("V", bound="ResourceView")


@dataclass(frozen=True)
class ResourceView:
    _view: dict[str, Any] = field(default_factory=dict, repr=False, compare=False, kw_only=True)

    @classmethod
    def from_view(cls: type[V], view: dict[str, Any], **extra: Any) -> V:
        names = {f.name for f in fields(cls)}
        values = {k: deepcopy(v) for k, v in view.items() if k in names}
        values.update(extra)
        return cls(**values, _view=deepcopy(view))

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._view)


class APIKeyRef(ResourceRef):
    kind = "api_key"


class DatasetRef(ResourceRef):
    kind = "dataset"


class DatasetVersionRef(ResourceRef):
    kind = "dataset_version"


class DatasetTagRef(ResourceRef):
    kind = "dataset_tag"


class DatasetApplicationRef(ResourceRef):
    kind = "dataset_application"


class ModelRef(ResourceRef):
    kind = "model"


class ProjectOwnerRef(ResourceRef):
    kind = "project_owner"


@dataclass(frozen=True)
class AccountInfo:
    alias: str
    username: str
    base_url: str
    user_id: str
    user_name: str


@dataclass(frozen=True)
class AccountCheck:
    ok: bool
    user: dict[str, Any] | None
    session_created_at: float | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class AccountContext(ResourceView):
    active: dict[str, Any]
    projects: list[dict[str, str]]
    workspaces: list[str]
    compute_groups: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    truncated: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Permission:
    workspace: str
    permission: str


@dataclass(frozen=True)
class APIKeyInfo(ResourceView):
    name: str
    ref: APIKeyRef | None = None
    created_at: str = ""


@dataclass(frozen=True)
class ProjectInfo(ResourceView):
    name: str
    ref: ProjectRef
    priority: str = ""
    my_remaining_budget: int | float | str | None = None
    project_remaining_budget: int | float | str | None = None


@dataclass(frozen=True)
class ProjectDetail(ResourceView):
    name: str
    ref: ProjectRef
    english_name: str = ""
    description: str = ""
    budget: int | float | str | None = None
    remaining_budget: int | float | str | None = None
    spent_budget: int | float | str | None = None
    spent_on_training: int | float | str | None = None
    spent_on_inference: int | float | str | None = None
    spent_on_storage: int | float | str | None = None
    spent_on_private_workspace: int | float | str | None = None
    priority: str = ""
    created_at: str = ""
    creator: str = ""


@dataclass(frozen=True)
class ProjectOwner(ResourceView):
    name: str
    ref: ProjectOwnerRef | None = None


@dataclass(frozen=True)
class ImageDetail(ResourceView):
    name: str
    ref: ImageRef
    status: str = ""
    framework: str = ""
    visibility: str = ""


@dataclass(frozen=True)
class DatasetInfo(ResourceView):
    name: str
    ref: DatasetRef
    project: str = ""
    grade: str = ""
    state: str = ""
    access: str = ""
    tags: list[str] = field(default_factory=list)
    updated_at: str = ""


@dataclass(frozen=True)
class DatasetVersion(ResourceView):
    version: str
    ref: DatasetVersionRef | None = None
    state: str = ""
    size: str = ""
    files: int | str = 0
    formats: list[str] = field(default_factory=list)
    updated_at: str = ""
    mount: str = ""
    path: str = ""


@dataclass(frozen=True)
class DatasetDetail(DatasetInfo):
    owner: str = ""
    maintainer: str = ""
    data_type: str = ""
    source_type: str = ""
    license: str = ""
    license_url: str = ""
    description: str = ""
    versions: tuple[DatasetVersion, ...] = ()


@dataclass(frozen=True)
class DatasetTag(ResourceView):
    name: str
    category: str
    ref: DatasetTagRef


@dataclass(frozen=True)
class DatasetApplication(ResourceView):
    name: str
    ref: DatasetApplicationRef
    state: str = ""
    authority: str = ""
    applicant: str = ""
    project: str = ""
    reason: str = ""
    approver: str = ""
    applied_at: str = ""
    decided_at: str = ""


@dataclass(frozen=True)
class DatasetValidation:
    name: str
    version: str
    mountable: bool
    path: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v != ""}


@dataclass(frozen=True)
class ModelInfo(ResourceView):
    name: str
    ref: ModelRef
    status: str = ""
    project: str = ""
    workspace: str = ""
    version: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass(frozen=True)
class ModelStatus(ResourceView):
    name: str
    ref: ModelRef
    status: str = ""
    version: str = ""
    description: str = ""
    type: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    published: bool = False
    project: str = ""
    owner: str = ""
    created_at: str = ""
    updated_at: str = ""
    vllm_ready: bool | None = None
    pending_serving: bool = False
    servings: list[dict[str, Any]] = field(default_factory=list)
    servings_shown: int | None = None
    servings_total: int | None = None
    servings_truncated: bool = False
    other_versions_in_use: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelVersion(ResourceView):
    version: str = ""
    status: str = ""
    size: str = ""
    vllm_ready: bool | None = None
    running_servings: int | str | None = None


@dataclass(frozen=True)
class ModelDeployConfig(ResourceView):
    model: str
    version: int
    vllm_compatible: bool
    min_nodes: int | None = None
    min_gpu_per_node: int | None = None
    min_cpu_per_node: int | None = None
    min_memory_gib_per_node: int | None = None
    min_quota: str = ""


@dataclass(frozen=True)
class ResourceAvailability(ResourceView):
    workspace: str
    compute_group: str
    kind: str
    ref: ComputeGroupRef | None = None
    gpu_type: str = ""
    total_gpus: int = 0
    used_gpus: int = 0
    available_gpus: int = 0
    high_priority_available_gpus: int = 0
    low_priority_gpus: int = 0
    total_nodes: int = 0
    ready_nodes: int = 0
    free_nodes: int = 0
    gpus_per_node: int = 0
    full_free_nodes: int = 0
    reclaimable_nodes: int = 0
    high_priority_free_nodes: int = 0
    full_free_gpus: int = 0
    high_priority_free_gpus: int = 0
    node_specs: list[dict[str, Any]] = field(default_factory=list)
    cpu_total: float = 0
    cpu_used: float = 0
    cpu_available: float = 0
    memory_total_gib: float = 0
    memory_used_gib: float = 0
    memory_available_gib: float = 0


@dataclass(frozen=True)
class ResourceUsage(ResourceView):
    scope: str
    items: list[dict[str, Any]]
    filters: dict[str, str] = field(default_factory=dict)
    compute_groups: list[str] = field(default_factory=list)
    shown: int | None = None
    total: int | None = None
    truncated: bool = False
