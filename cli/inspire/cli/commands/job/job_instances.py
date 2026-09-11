"""CLI instance labels backed by the shared selection service."""

from inspire.services.job.job_events import (
    JobInstanceSelectionError as JobInstanceSelectionError,
    JobInstanceView as JobInstanceView,
    select_job_instance_views as select_job_instance_views,
    job_instance_views as _views,
)
from .job_commands import _public_job_instances


def job_instance_views(instances):
    return _views(instances, project=_public_job_instances)
