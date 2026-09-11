from __future__ import annotations
from typing import Any
from inspire.platform.web import browser_api as browser_api_module


def summarize(points: list[tuple[float, int, float]]) -> dict[str, Any]:
    """Reduce one series to the shape a training-health question actually asks."""
    by_step = sorted(points, key=lambda point: point[1])
    values = [value for _, _, value in by_step]
    first_step, first_value = by_step[0][1], by_step[0][2]
    last_step, last_value = by_step[-1][1], by_step[-1][2]
    return {
        "count": len(by_step),
        "first_step": first_step,
        "first_value": first_value,
        "last_step": last_step,
        "last_value": last_value,
        "min": min(values),
        "max": max(values),
    }


def tail(points: list[tuple[float, int, float]], budget: int) -> list[list[float]]:
    by_step = sorted(points, key=lambda point: point[1])
    return [[step, value] for _, step, value in by_step[-budget:]]


def collect_series(
    session,  # noqa: ANN001
    board,  # noqa: ANN001
    *,
    run: str,
    tag: str,
) -> list[dict[str, Any]]:
    tags_by_run = browser_api_module.read_tensorboard_scalar_tags(board.url, session=session)
    collected: list[dict[str, Any]] = []
    for run_name, tags in sorted(tags_by_run.items()):
        if run and run_name != run:
            continue
        for tag_name in tags:
            if tag and tag_name != tag:
                continue
            points = browser_api_module.read_tensorboard_scalar_series(
                board.url, run=run_name, tag=tag_name, session=session
            )
            if not points:
                continue
            collected.append(
                {
                    "run": run_name,
                    "tag": tag_name,
                    "points": points,
                    **summarize(points),
                }
            )
    return collected
