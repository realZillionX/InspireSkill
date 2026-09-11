"""Immutable public values. Resource references are identities, not credentials."""

from __future__ import annotations

from ._sync_handle import SyncHandle

from dataclasses import dataclass, field, asdict
from typing import Any, Generic, TypeVar, ClassVar

from .exceptions import ValidationError
from inspire.platform.web.browser_api.datasets import DatasetMount
from inspire.platform.web.browser_api.metrics import MetricGroup as MetricGroup

T = TypeVar("T")
R = TypeVar("R", bound="ResourceRef")


@dataclass(frozen=True)
class ResourceRef:
    name: str
    account: str
    base_url: str
    key: str = field(repr=False)
    workspace_id: str = field(default="", repr=False)
    kind: ClassVar[str] = "resource"

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "kind": self.kind, **asdict(self)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]):
        if value.get("version") != 1 or value.get("kind") != cls.kind:
            raise ValidationError("Reference type or version does not match.")
        keys = ("name", "account", "base_url", "key", "workspace_id")
        if any(not isinstance(value.get(k), str) for k in keys):
            raise ValidationError("Invalid reference fields.")
        if any(not value[k].strip() for k in keys[:-1]):
            raise ValidationError("Reference identity is incomplete.")
        return cls(**{k: value[k] for k in keys})


class WorkspaceRef(ResourceRef):
    kind = "workspace"


class ProjectRef(ResourceRef):
    kind = "project"


class ComputeGroupRef(ResourceRef):
    kind = "compute_group"


class ImageRef(ResourceRef):
    kind = "image"


class QuotaRef(ResourceRef):
    kind = "quota"


class JobRef(ResourceRef):
    kind = "job"


@dataclass(frozen=True)
class Resource(Generic[R]):
    name: str
    ref: R


@dataclass(frozen=True)
class Image(Resource[ImageRef]):
    source: str
    url: str = field(repr=False)
    status: str = ""
    framework: str = ""
    visibility: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "framework": self.framework,
            "visibility": self.visibility,
        }


@dataclass(frozen=True)
class Quota:
    gpu: int
    cpu: int
    memory_gib: int

    def __post_init__(self):
        for name, minimum in (("gpu", 0), ("cpu", 1), ("memory_gib", 1)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValidationError(f"{name} must be an integer >= {minimum}.")


@dataclass(frozen=True)
class QuotaOption(Resource[QuotaRef]):
    quota: Quota | None
    group: ComputeGroupRef
    gpu_type: str
    workspace: str = ""
    priority: str = ""
    allowed_priority_levels: tuple[str, ...] | None = None
    points_per_hour: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "compute_group": self.group.name,
            "gpu_type": self.gpu_type,
            "quota": self.name,
            "priority": self.priority,
            "allowed_priority_levels": list(self.allowed_priority_levels)
            if self.allowed_priority_levels is not None
            else None,
            "points_per_hour": self.points_per_hour,
        }


@dataclass(frozen=True)
class ImageSelector:
    name: str
    source: str


@dataclass(frozen=True)
class Job:
    name: str
    ref: JobRef
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
class JobHandle(SyncHandle):
    name: str
    ref: JobRef
    operation_id: str
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None
    total: int | None = None


@dataclass(frozen=True)
class JobCreateSpec:
    name: str
    workspace: str | WorkspaceRef
    project: str | ProjectRef
    group: str | ComputeGroupRef
    quota: str | Quota | QuotaRef
    image: str | ImageRef | ImageSelector
    command: str = field(repr=False)
    nodes: int = 1
    shm_gib: int | None = None
    priority: int | None = None
    max_time_hours: float | None = None
    description: str | None = field(default=None, repr=False)
    framework: str = "pytorch"
    auto_fault_tolerance: bool | None = None
    fault_tolerance_max_retry: int | None = None
    fault_tolerance_retry_interval_sec: int | None = None
    datasets: list[str | DatasetMount] = field(default_factory=list)
    envs: dict[str, str] = field(default_factory=dict, repr=False)
    keep_after_success_hours: float | None = None
    keep_after_failure_hours: float | None = None
    public_path_readonly: bool | None = None
    enable_notification: bool | None = None
    exclude_nodes: list[str] = field(default_factory=list)
    specified_nodes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JobPlan:
    name: str
    workspace: Resource[WorkspaceRef]
    project: Resource[ProjectRef]
    group: Resource[ComputeGroupRef]
    image: Image
    quota: Quota
    priority: int
    nodes: int = 1
    datasets: tuple[DatasetMount, ...] = ()
    envs_count: int = 0
    description: str | None = None
    max_time: str | None = None
    shm: int | None = None
    create_kwargs: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a public review summary; create_kwargs holds the full payload."""
        return {
            "name": self.name, "workspace": self.workspace.name,
            "project": self.project.name, "compute_group": self.group.name,
            "image": self.image.name, "quota": str(self.quota),
            "priority": self.priority, "nodes": self.nodes, "envs_count": self.envs_count,
            "description": self.description, "max_time": self.max_time, "shm": self.shm,
            "datasets": [f"{item.dataset}:{item.version}" for item in self.datasets],
        }

    @property
    def summary(self) -> str:
        return (
            f"{self.name}: {self.workspace.name} / {self.project.name} / "
            f"{self.group.name}; {self.quota}; image={self.image.name}; "
            f"priority={self.priority}; nodes={self.nodes}; datasets={self.datasets}; "
            f"envs={self.envs_count}; description={self.description}; "
            f"max_time={self.max_time}; shm={self.shm}. No resources reserved."
        )


@dataclass(frozen=True)
class LogResult:
    text: str = field(repr=False)
    instances: tuple[str, ...]
    start: str
    end: str
    truncated: bool
    total: int | None = None
    items: tuple[dict[str, Any], ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class EventResult:
    items: tuple[dict[str, Any], ...] = field(repr=False)
    truncated: bool = False


@dataclass(frozen=True)
class Instance:
    """SDK instance identity; print label, never handle, pod or raw platform data."""

    label: str
    handle: str = field(repr=False)
    pod: str = field(default="", repr=False)
    kind: str = ""
    status: str = ""
    node: str = ""
    role: str = ""
    rank: int | str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


JobEvent = dict[str, Any]
