"""Inference serving (model deployment) commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import serving_batch
from inspire.cli.commands.workload_quota import make_quota_command
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

    Deploy a registered model as an HTTP service, inspect the service list
    and detail, check resource metrics, and stop or delete stale deployments.
    Use `model list/status/versions` first when you need to pick a model and
    version, then `serving create --dry-run` to verify the deployment plan.

    \b
    Examples:
        inspire model versions my-model --workspace 分布式训练空间
        inspire serving configs --workspace 分布式训练空间
        inspire serving create --name demo --model my-model --workspace 分布式训练空间 --project CI-情境智能 --group H200-2号机房 --quota 1,18,200 --image serve-base:v1 --command "python serve.py" --port 8000 --dry-run
        inspire serving list --workspace 分布式训练空间
        inspire serving status <serving-name> --workspace 分布式训练空间
        inspire serving delete <serving-name> --workspace 分布式训练空间
        inspire serving metrics <serving-name> --workspace 分布式训练空间 --window 30m
    """


serving.add_command(create_serving)
serving.add_command(make_quota_command("serving"))
serving.add_command(make_profile_command("serving"))
serving.add_command(serving_batch)
serving.add_command(list_serving)
serving.add_command(status_serving)
serving.add_command(stop_serving)
serving.add_command(delete_serving)
serving.add_command(configs_serving)
serving.add_command(serving_metrics)


__all__ = ["serving"]
