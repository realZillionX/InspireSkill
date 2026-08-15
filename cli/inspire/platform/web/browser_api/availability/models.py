"""Models for browser (web-session) availability APIs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GPUAvailability:
    """Compute-group availability metrics."""

    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    used_gpus: int
    available_gpus: int
    low_priority_gpus: int  # GPUs used by low-priority tasks (can be preempted)
    total_nodes: int = 0
    ready_nodes: int = 0
    free_nodes: int = 0
    gpu_per_node: int = 0
    workspace_id: str = ""
    workspace_name: str = ""
    cpu_total: float = 0.0
    cpu_used: float = 0.0
    cpu_available: float = 0.0
    memory_total_gib: float = 0.0
    memory_used_gib: float = 0.0
    memory_available_gib: float = 0.0
    resource_kind: str = "gpu"

    @property
    def high_priority_available_gpus(self) -> int:
        """GPU capacity visible to high-priority jobs after low-priority preemption."""
        return int(self.available_gpus) + int(self.low_priority_gpus)


@dataclass
class FullFreeNodeCount:
    """Full-free (idle) node counts for a compute group."""

    group_id: str
    group_name: str
    gpu_per_node: int
    total_nodes: int
    ready_nodes: int
    full_free_nodes: int


@dataclass(frozen=True)
class TaskUsage:
    """One live workload and the capacity it is currently holding.

    Answers "who is holding the cards", which no other read exposes:
    availability reports how much is left, never who took the rest.

    ``gpu_usage_rate`` / ``cpu_usage_rate`` are 0..1 fractions of the *held*
    allocation that is actually busy, so a large ``gpus`` next to a near-zero
    rate is capacity parked rather than used.
    """

    task_id: str
    name: str
    task_type: str
    status: str
    user_name: str
    project_name: str
    gpus: int
    cpus: float
    memory_gib: float
    gpu_usage_rate: float
    cpu_usage_rate: float
    node_names: tuple[str, ...]
    created_at: str
    running_time_ms: int


@dataclass(frozen=True)
class MemberUsage:
    """The caller's own footprint in one project of a workspace.

    ``workspace.ListUserDimension`` reports **only the caller**: asking for
    another member's ``user_id`` answers an empty list rather than a denial,
    so this can never grow into a per-member view of the workspace.
    """

    user_name: str
    project_name: str
    gpus: int
    cpus: float
    memory_gib: float
    gpu_nodes: int
    cpu_nodes: int
    hpc_nodes: int


@dataclass(frozen=True)
class NodeSpec:
    """One distinct per-node hardware shape a compute group can schedule onto.

    This is a **spec catalog, not a node inventory** — a group with 292 nodes
    reports 17 shapes — so nothing here may be read as a node count.
    """

    node_type: str
    gpu_type: str
    gpu_count: int
    cpu_count: float
    memory_gib: float
    job_types: tuple[str, ...]

    @property
    def label(self) -> str:
        """Compact human label, e.g. ``H200x8 183C 1888G``."""
        parts: list[str] = []
        if self.gpu_count > 0:
            parts.append(f"{self.gpu_type or 'GPU'}x{self.gpu_count}")
        elif self.gpu_type and self.gpu_type.upper() != "CPU":
            parts.append(self.gpu_type)
        parts.append(f"{self.cpu_count:g}C")
        parts.append(f"{self.memory_gib:g}G")
        return " ".join(parts)


__all__ = [
    "FullFreeNodeCount",
    "GPUAvailability",
    "MemberUsage",
    "NodeSpec",
    "TaskUsage",
]
