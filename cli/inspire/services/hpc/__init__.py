"""HPC Slurm submission rules and views of jobs and their instances.

Entrypoint validation, status, events, logs and output projections belong here.
Slurm-specific policy should not be inferred from training-job defaults. HTTP
Actions belong to inspire.platform.web.browser_api.hpc_jobs; interactive shells
and terminal rendering belong to inspire.cli.
"""
