"""Inference serving (model deployment) commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import serving_batch
from inspire.cli.commands.workload_profile import make_profile_command

from .serving_commands import (
    configs_serving,
    create_serving,
    delete_serving,
    list_serving,
    status_serving,
    stop_serving,
)
from .serving_metrics import serving_metrics


@click.group()
def serving() -> None:
    """Manage inference servings (model deployment).

    Covers model deployment services: create, list, status, available configs,
    resource metrics, stop, and delete.

    \b
    Examples:
        inspire serving create --name demo --model my-model --workspace 分布式训练空间 --group H200-2号机房 --quota 1,18,200 --image sandbox-base:ubuntu24.04-py3.12-1.0.0 --command 'python serve.py' --port 8000 --priority 1
        inspire serving list
        inspire serving status <serving-name>
        inspire serving delete <serving-name>
        inspire serving metrics <serving-name> --window 30m
    """


serving.add_command(create_serving)
serving.add_command(make_profile_command("serving"))
serving.add_command(serving_batch)
serving.add_command(list_serving)
serving.add_command(status_serving)
serving.add_command(stop_serving)
serving.add_command(delete_serving)
serving.add_command(configs_serving)
serving.add_command(serving_metrics)  # metrics (资源视图 time-series; per-replica pods)


__all__ = ["serving"]
