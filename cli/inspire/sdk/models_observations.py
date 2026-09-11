"""Frozen observations retaining the shared CLI projections in ``to_dict()``.

Conditional keys are retained by ResourceView, including present empty values.
Opaque rule strings and flat invocation fields keep their service representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models_resources import ResourceView


@dataclass(frozen=True)
class ServingVersion(ResourceView):
    version: int | str | None = None
    status: str | None = None
    model: str | None = None
    command: str | None = field(default=None, repr=False)
    created_at: str | None = None
    replicas: int | None = None
    port: int | None = None
    resource: str | None = None


@dataclass(frozen=True)
class ServingScaleHistoryEntry(ResourceView):
    replicas_from: int | None = None
    replicas_to: int | None = None
    status: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class ServingConfigItem(ResourceView):
    name: str | None = None
    gpu_count_min: int | float | str | None = None
    gpu_count_max: int | float | str | None = None
    cpu_count_min: int | float | str | None = None
    cpu_count_max: int | float | str | None = None
    memory_gib_min: int | float | str | None = None
    memory_gib_max: int | float | str | None = None
    replicas: int | str | None = None
    auto_stop_rules: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ServingConfigs(ResourceView):
    items: tuple[ServingConfigItem, ...] = field(default=(), repr=False)
    auto_stop: bool | None = None


@dataclass(frozen=True)
class ServingInvocationCredentials(ResourceView):
    credential_env: str
    auth_header: str
    auth_scheme: str
    affinity_header: str


@dataclass(frozen=True)
class ServingInvocationInfo(ServingInvocationCredentials):
    name: str
    status: str
    type: str
    endpoint: str
    note: str
    base_url: str | None = None
    example: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ServingAPIMetricSeries(ResourceView):
    metric: str
    count: int
    unit: str | None = None
    group: str | None = None
    min: float | None = None
    max: float | None = None
    avg: float | None = None
    last: float | None = None
    total: float | None = None


@dataclass(frozen=True)
class ServingAPIMetricTimeRange(ResourceView):
    start: int
    end: int
    interval: str


@dataclass(frozen=True)
class ServingAPIMetrics(ResourceView):
    resource: str
    name: str
    metrics: tuple[str, ...]
    time_range: ServingAPIMetricTimeRange
    series: tuple[ServingAPIMetricSeries, ...] = field(repr=False)


@dataclass(frozen=True)
class TensorboardTags(ResourceView):
    name: str
    summary_path: str
    runs: tuple[str, ...] = field(repr=False)
    scalar_tags: dict[str, tuple[str, ...]] = field(repr=False)


@dataclass(frozen=True)
class TensorboardScalarPoint:
    step: int
    value: float

    def to_list(self) -> list[int | float]:
        """The existing scalar projection encodes points as [step, value]."""
        return [self.step, self.value]


@dataclass(frozen=True)
class TensorboardScalarSeries(ResourceView):
    run: str
    tag: str
    count: int
    first_step: int
    first_value: float
    last_step: int
    last_value: float
    min: float
    max: float
    points: tuple[TensorboardScalarPoint, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class TensorboardScalars(ResourceView):
    name: str
    summary_path: str
    series: tuple[TensorboardScalarSeries, ...] = field(repr=False)


@dataclass(frozen=True)
class RayScalingEvent(ResourceView):
    time: str
    event: str
    group: str | None = None
    replicas_before: int | None = None
    replicas_after: int | None = None


@dataclass(frozen=True)
class NotebookRun(ResourceView):
    index: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    status: str | None = None
