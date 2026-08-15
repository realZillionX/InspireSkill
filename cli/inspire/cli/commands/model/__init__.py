"""Model registry commands for Inspire CLI."""

from __future__ import annotations

import click

from .model_commands import (
    delete_model_cmd,
    deploy_config_model,
    list_model,
    register_model,
    status_model,
    versions_model,
)


@click.group()
def model() -> None:
    """Use the platform model repository.

    Inspect registered models, inspect versions, register an existing
    platform-visible model directory as a model entry, and delete an entry that
    is no longer referenced. This command group does not upload local files;
    put model files on shared storage first. A registered entry cannot be
    edited -- delete and register again to change one. Use `serving` for
    deployed service lifecycle commands.

    \b
    Examples:
        inspire model list --workspace 分布式训练空间
        inspire model status qwen-demo --workspace 分布式训练空间 --project CI-情境智能
        inspire model versions qwen-demo --workspace 分布式训练空间
        inspire model deploy-config qwen-demo --workspace 分布式训练空间
        inspire model register --name qwen-demo --source-path /inspire/hdd/project/<topic>/public/models/qwen-demo --workspace 分布式训练空间 --project CI-情境智能
        inspire model delete qwen-demo --workspace 分布式训练空间 --yes
    """


model.add_command(list_model)
model.add_command(register_model)
model.add_command(status_model)
model.add_command(versions_model)
model.add_command(deploy_config_model)
model.add_command(delete_model_cmd)


__all__ = ["model"]
