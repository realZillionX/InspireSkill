"""Inference serving (model deployment) commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.batch import serving_batch
from inspire.cli.commands.workload_quota import make_quota_command

from .serving_api_metrics import serving_api_metrics
from .serving_commands import (
    shell_serving,
    configs_serving,
    create_serving,
    delete_serving,
    list_serving,
    rollback_serving,
    scale_history_serving,
    scale_serving,
    status_serving,
    stop_serving,
    versions_serving,
)
from .serving_logs import logs_serving
from .serving_metrics import serving_metrics


@click.group()
def serving() -> None:
    """Manage inference servings (model deployment).

    Deploy a registered model as an HTTP service, inspect the service list
    and detail, scale or roll it back, check resource and request metrics, and
    stop or delete stale deployments. Use `model list/status/versions` first
    when you need to pick a model and version, then `serving create --dry-run`
    to verify the deployment plan.

    `metrics` covers GPU / CPU / memory utilization; `api-metrics` covers
    request traffic (QPS, success rate, latency, tokens).

    \b
    Examples:
        inspire model versions my-model --workspace 分布式训练空间
        inspire serving configs --workspace 分布式训练空间
        inspire serving create --name demo --model my-model --workspace 分布式训练空间 --project <project> --group H200-2号机房 --quota 1,18,200 --image <image> --command "python serve.py" --port 8000 --dry-run
        inspire serving list --workspace 分布式训练空间
        inspire serving status <serving-name> --workspace 分布式训练空间
        inspire serving scale <serving-name> --workspace 分布式训练空间 --replicas 3
        inspire serving scale-history <serving-name> --workspace 分布式训练空间
        inspire serving versions <serving-name> --workspace 分布式训练空间
        inspire serving rollback <serving-name> --workspace 分布式训练空间 --version 2
        inspire serving logs <serving-name> --workspace 分布式训练空间 --tail 50
        inspire serving delete <serving-name> --workspace 分布式训练空间
        inspire serving metrics <serving-name> --workspace 分布式训练空间 --window 30m
        inspire serving api-metrics <serving-name> --workspace 分布式训练空间 --window 30m
    """


serving.add_command(create_serving)
serving.add_command(make_quota_command("serving"))
serving.add_command(serving_batch)
serving.add_command(list_serving)
serving.add_command(status_serving)
serving.add_command(stop_serving)
serving.add_command(scale_serving)
serving.add_command(scale_history_serving)
serving.add_command(versions_serving)
serving.add_command(rollback_serving)
serving.add_command(logs_serving)
serving.add_command(delete_serving)
serving.add_command(configs_serving)
serving.add_command(serving_metrics)
serving.add_command(serving_api_metrics)
serving.add_command(shell_serving)


__all__ = ["serving"]
