"""Workspace-level scheduling policy — what the platform does to idle work.

Every workspace declares, per workload, whether the scheduler reclaims work
that stops using its resources and whether there is a hard cap on how long a
workload may run. Nothing else in the CLI reads this, so a notebook that
vanished overnight currently has no explanation the user can look up.

**Three requests cover five workloads.** The platform keeps one shared
scheduling-config record per workspace plus two standalone ones:

* ``notebook.GetScheduleConfig`` returns the whole shared record — the
  notebook, distributed-training and Ray slices all live in it.
* ``hpc.GetHpcScheduleConfig`` and ``inference_serving.GetServingScheduleConfig``
  are separate records (neither carries the shared record's ``config_id``).

The three sibling Actions are therefore deliberately not wrapped:

* ``notebook.GetNotebookScheduleConfig`` and ``ray.GetRayJobScheduleConfig``
  are strict projections of the shared record. Measured across all ten visible
  workspaces, every key they return is present in ``GetScheduleConfig`` with an
  identical value — including the ones that actually vary between workspaces.
* ``train.GetTrainScheduleConfig``'s scheduling fields are likewise identical.
  Its only exclusives are the console form's fault-tolerance defaults, which
  say nothing about what ``inspire job create`` will do because the CLI takes
  its own defaults from ``[job]`` config, and the ``train_enable_*`` workspace
  capability switches, which are platform-side behaviour with no user control.
* ``workspace.GetScheduleConfig`` answers ``AccessForbidden: You are not the
  admin of the <workspace_id> workspace`` to an ordinary member. That is a
  permission boundary and not a scoping mistake: it was retried with top-level
  ``workspace_id`` and PascalCase ``WorkspaceId`` (identical refusal, with the
  workspace id echoed back) and with a nested ``filter`` (``unknown field``).

**Workspace scoping differs per Action.** ``notebook.GetScheduleConfig`` takes
PascalCase ``WorkspaceId``; the other two take snake_case ``workspace_id``.
discovery's ``JSONField`` agrees, and the wrong spelling is rejected outright.

**An absent policy is not a permissive policy.** ``hpc.GetHpcScheduleConfig``
answers a literal ``Result: null`` on workspaces that run no HPC. That has to
surface as "this workspace declares no HPC policy", never as "no reclaim and no
time limit" — the two would send a user to the opposite decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.session.models import WebSession

from .core import _get_base_url, _request_json, _v2_result

# The platform's `crit` vocabulary, mapped to the CLI's own resource words.
# `RUNTIME` is the odd one out: it has no threshold, it simply caps wall time.
_CRITERIA = {
    "GPU": "gpu",
    "CPU": "cpu",
    "MEM": "memory",
    "RUNTIME": "runtime",
}


@dataclass(frozen=True)
class ReclaimCondition:
    """One "resource stayed below X% for N hours" clause of a reclaim rule."""

    criterion: str
    hours: float
    threshold: Optional[float] = None

    def describe(self) -> str:
        if self.criterion == "runtime":
            return f"runtime > {_hours(self.hours)}"
        if self.threshold is None:
            return f"{self.criterion} idle for {_hours(self.hours)}"
        return f"{self.criterion} < {_percent(self.threshold)} for {_hours(self.hours)}"


@dataclass(frozen=True)
class ReclaimRule:
    """A reclaim rule: groups of conditions, each group joined by its own gate.

    The platform nests two levels of boolean gate and both are load-bearing —
    ``CPU < 15% AND GPU < 15%`` reclaims far less than ``CPU < 15% OR GPU <
    15%``, so the gates are carried through rather than flattened away.
    """

    gate: str
    groups: tuple[tuple[str, tuple[ReclaimCondition, ...]], ...]

    def describe(self) -> str:
        parts: list[str] = []
        for gate, conditions in self.groups:
            if not conditions:
                continue
            text = f" {gate.upper()} ".join(c.describe() for c in conditions)
            if len(conditions) > 1 and len(self.groups) > 1:
                text = f"({text})"
            parts.append(text)
        return f" {self.gate.upper()} ".join(parts)


@dataclass(frozen=True)
class WorkloadSchedulePolicy:
    """What a workspace will do to one kind of workload left running.

    ``configured`` is the distinction that matters: ``False`` means the
    platform answered with no policy record for this workload, which is not the
    same as a record that happens to switch everything off.
    """

    workload: str
    configured: bool = True
    auto_reclaim: Optional[bool] = None
    reclaim_rule: Optional[ReclaimRule] = None
    max_runtime_minutes: Optional[int] = None
    daily_shutdown: Optional[str] = None
    auto_save: Optional[bool] = None
    applies_to: Optional[str] = None

    @property
    def reclaim_description(self) -> Optional[str]:
        if self.reclaim_rule is None:
            return None
        described = self.reclaim_rule.describe()
        if not described:
            return None
        if self.applies_to:
            return f"{described} ({self.applies_to})"
        return described


def _hours(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}h"
    return f"{value:g}h"


def _percent(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}%"
    return f"{value:g}%"


def format_duration(minutes: Optional[int]) -> Optional[str]:
    """Render a max-runtime cap the way a user would say it out loud."""
    if not minutes or minutes <= 0:
        return None
    days, remainder = divmod(int(minutes), 24 * 60)
    hours, mins = divmod(remainder, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else ""]
    return " ".join(part for part in parts if part)


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _flag(value: Any) -> bool:
    """Read a switch the platform spells as ``0``/``1`` in some places and
    ``false``/``true`` in others."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes"}


def _parse_rule(raw: Any) -> Optional[ReclaimRule]:
    """Normalize a reclaim ruleset that arrives as a dict *or* a JSON string.

    Only ``recycle_config`` (the notebook slice) is sent as a real object; the
    train, Ray, HPC and serving rulesets are JSON-encoded strings, and the
    platform spells "no rule" as ``""`` on some workspaces and ``"{}"`` on
    others.
    """
    payload = raw
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
    if not isinstance(payload, dict) or not payload:
        return None

    groups: list[tuple[str, tuple[ReclaimCondition, ...]]] = []
    for group in payload.get("conds") or []:
        if not isinstance(group, dict):
            continue
        conditions: list[ReclaimCondition] = []
        for cond in group.get("conds") or []:
            if not isinstance(cond, dict):
                continue
            criterion = _CRITERIA.get(str(cond.get("crit") or "").strip().upper())
            if not criterion:
                continue
            conditions.append(
                ReclaimCondition(
                    criterion=criterion,
                    hours=_number(cond.get("hrs")),
                    threshold=None if criterion == "runtime" else _number(cond.get("thresh")),
                )
            )
        if conditions:
            groups.append((_gate(group.get("gate")), tuple(conditions)))
    if not groups:
        return None
    return ReclaimRule(gate=_gate(payload.get("gate")), groups=tuple(groups))


def _gate(value: Any) -> str:
    return "and" if str(value or "").strip().upper() == "AND" else "or"


def _notebook_rule(shared: dict[str, Any]) -> Optional[ReclaimRule]:
    """Read the notebook reclaim rule, falling back the way the console does.

    ``recycle_config`` is the current shape. When it is empty the console
    rebuilds the rule from the legacy ``recycle_hour`` / ``recycle_standard`` /
    ``recycle_rate`` triple, and it only does so when all three are set. Not
    replicating that would report "no idle reclaim" on a workspace that still
    carries its rule in the old three fields.
    """
    rule = _parse_rule(shared.get("recycle_config"))
    if rule is not None:
        return rule

    hours = _number(shared.get("recycle_hour"))
    criterion = _CRITERIA.get(str(shared.get("recycle_standard") or "").strip().upper())
    threshold = _number(shared.get("recycle_rate"))
    if not hours or not criterion or not threshold:
        return None
    condition = ReclaimCondition(
        criterion=criterion,
        hours=hours,
        threshold=None if criterion == "runtime" else threshold,
    )
    return ReclaimRule(gate="or", groups=(("or", (condition,)),))


def _timed_minutes(payload: dict[str, Any], day: str, hour: str, minute: str) -> Optional[int]:
    """Total a day / hour / minute cap into minutes, or ``None`` when unset.

    The three keys are spelled per workload — ``recycle_train_day`` against
    ``max_running_time_days`` — but the console proves they are one concept: it
    binds ``enable_max_running_time`` to the same ``timedRecycle`` form control
    the train and Ray switches use.
    """
    minutes = (
        _int(payload.get(day)) * 24 * 60 + _int(payload.get(hour)) * 60 + _int(payload.get(minute))
    )
    return minutes or None


def _clock(hour: Any, minute: Any) -> str:
    return f"{_int(hour):02d}:{_int(minute):02d}"


def _notebook_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    timed_shutdown = _flag(shared.get("timed_shutdown"))
    return WorkloadSchedulePolicy(
        workload="notebook",
        auto_reclaim=_flag(shared.get("auto_recycle")),
        reclaim_rule=_notebook_rule(shared),
        # A notebook's cap is a wall-clock shutdown time, not a duration: the
        # console formats `shutdown_hour`/`shutdown_minute` as `H:m`. A running
        # notebook's own duration cap, when there is one, arrives as a RUNTIME
        # clause inside the reclaim rule instead.
        daily_shutdown=_clock(shared.get("shutdown_hour"), shared.get("shutdown_minute"))
        if timed_shutdown
        else None,
        auto_save=_flag(shared.get("shutdown_save")) or _flag(shared.get("recycle_save")),
    )


def _train_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    return WorkloadSchedulePolicy(
        workload="job",
        auto_reclaim=_flag(shared.get("auto_recycle_train")),
        reclaim_rule=_parse_rule(shared.get("auto_recycle_train_ruleset")),
        max_runtime_minutes=_timed_minutes(
            shared, "recycle_train_day", "recycle_train_hour", "recycle_train_minute"
        )
        if _flag(shared.get("timed_recycle_train"))
        else None,
    )


def _ray_policy(shared: dict[str, Any]) -> WorkloadSchedulePolicy:
    return WorkloadSchedulePolicy(
        workload="ray",
        auto_reclaim=_flag(shared.get("auto_recycle_rayjob")),
        reclaim_rule=_parse_rule(shared.get("auto_recycle_rayjob_ruleset")),
        max_runtime_minutes=_timed_minutes(
            shared, "recycle_rayjob_day", "recycle_rayjob_hour", "recycle_rayjob_minute"
        )
        if _flag(shared.get("timed_recycle_rayjob"))
        else None,
    )


def _hpc_policy(payload: dict[str, Any]) -> WorkloadSchedulePolicy:
    if not payload:
        # `Result: null`. The workspace declares no HPC policy at all, which is
        # a different answer from "reclaim off, no cap".
        return WorkloadSchedulePolicy(workload="hpc", configured=False)
    return WorkloadSchedulePolicy(
        workload="hpc",
        auto_reclaim=_flag(payload.get("enable_auto_stop")),
        reclaim_rule=_parse_rule(payload.get("auto_stop_ruleset")),
        max_runtime_minutes=_timed_minutes(
            payload,
            "max_running_time_days",
            "max_running_time_hours",
            "max_running_time_minutes",
        )
        if _flag(payload.get("enable_max_running_time"))
        else None,
    )


def _serving_policies(payload: dict[str, Any]) -> list[WorkloadSchedulePolicy]:
    """Serving states its reclaim rule per GPU-count band, so one row per band.

    Every workspace reachable today declares at most one band, but the shape is
    a list and each entry carries its own ruleset, so collapsing it would drop
    whichever band the caller's deployment actually falls into.
    """
    if not payload:
        return [WorkloadSchedulePolicy(workload="serving", configured=False)]

    enabled = _flag(payload.get("enable_auto_stop"))
    bands = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not bands:
        return [WorkloadSchedulePolicy(workload="serving", auto_reclaim=enabled)]

    rows: list[WorkloadSchedulePolicy] = []
    for band in bands:
        low, high = _int(band.get("gpu_count_min")), _int(band.get("gpu_count_max"))
        rows.append(
            WorkloadSchedulePolicy(
                workload="serving",
                auto_reclaim=enabled,
                reclaim_rule=_parse_rule(band.get("auto_stop_ruleset")),
                applies_to=f"{low}-{high} GPU" if high else None,
            )
        )
    return rows


def get_workspace_schedule_policy(
    workspace_id: str,
    *,
    session: WebSession,
) -> list[WorkloadSchedulePolicy]:
    """Read a workspace's reclaim and runtime policy for all five workloads.

    Three requests, all read-only and all available to an ordinary workspace
    member. Failures propagate: a workload row is only reported once the
    platform has answered for it, so "the request failed" can never be read as
    "this workspace reclaims nothing".
    """
    base_url = _get_base_url()

    shared = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/notebook?Action=GetScheduleConfig",
            referer=f"{base_url}/jobs/interactiveModeling",
            # PascalCase here and snake_case below; this Action is the only one
            # of the family that spells the key that way.
            body={"WorkspaceId": workspace_id},
            timeout=20,
        )
    )
    hpc = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/hpc?Action=GetHpcScheduleConfig",
            referer=f"{base_url}/jobs/highPerformanceComputing",
            body={"workspace_id": workspace_id},
            timeout=20,
        )
    )
    serving = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/inference_serving?Action=GetServingScheduleConfig",
            referer=f"{base_url}/jobs/modelDeployment",
            body={"workspace_id": workspace_id},
            timeout=20,
        )
    )

    return [
        _notebook_policy(shared),
        _train_policy(shared),
        _hpc_policy(hpc),
        _ray_policy(shared),
        *_serving_policies(serving),
    ]


__all__ = [
    "ReclaimCondition",
    "ReclaimRule",
    "WorkloadSchedulePolicy",
    "format_duration",
    "get_workspace_schedule_policy",
]
