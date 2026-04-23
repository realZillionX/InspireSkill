"""CLI command modules."""

from inspire.cli.commands.account import account
from inspire.cli.commands.job import job
from inspire.cli.commands.resources import resources
from inspire.cli.commands.config import config
from inspire.cli.commands.run import run
from inspire.cli.commands.notebook import notebook
from inspire.cli.commands.init import init
from inspire.cli.commands.image import image
from inspire.cli.commands.project import project
from inspire.cli.commands.hpc import hpc
from inspire.cli.commands.model import model
from inspire.cli.commands.ray import ray
from inspire.cli.commands.serving import serving
from inspire.cli.commands.update import update
from inspire.cli.commands.user import user

__all__ = [
    "account",
    "job",
    "resources",
    "config",
    "run",
    "notebook",
    "init",
    "image",
    "project",
    "hpc",
    "model",
    "ray",
    "serving",
    "update",
    "user",
]
