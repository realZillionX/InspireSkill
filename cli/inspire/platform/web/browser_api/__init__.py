"""Browser (web-session) API façade.

Historically all SSO-only endpoints lived in one large module. The implementation is now split
into smaller domain modules, and this file re-exports the public API to keep import paths stable.
"""

from __future__ import annotations

from .availability import (
    FullFreeNodeCount,
    GPUAvailability,
    find_best_compute_group_accurate,
    get_accurate_resource_availability,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)
from .jobs import (
    JobInfo,
    delete_job,
    get_current_user,
    get_train_job_workdir,
    list_job_events,
    list_job_instance_events,
    list_job_users,
    list_jobs,
)
from .hpc_jobs import (
    HPCJobInfo,
    delete_hpc_job,
    list_hpc_jobs,
    list_hpc_job_events,
)
from .ray_jobs import (
    RayJobInfo,
    create_ray_job,
    delete_ray_job,
    get_ray_job_detail,
    list_ray_job_events,
    list_ray_job_instances,
    list_ray_job_scaling_histories,
    list_ray_job_users,
    list_ray_jobs,
    stop_ray_job,
)
from .metrics import (
    INTERVAL_CHOICES,
    METRIC_TYPES,
    TASK_TYPE_BY_RESOURCE,
    MetricGroup,
    MetricSample,
    get_resource_metrics_by_time,
)
from .notebooks import (
    ImageInfo,
    NotebookFailedError,
    create_notebook,
    delete_notebook,
    get_notebook_detail,
    get_notebook_schedule,
    get_resource_prices,
    list_images,
    list_notebook_compute_groups,
    list_notebook_events,
    list_notebook_lifecycle,
    list_notebook_runs,
    start_notebook,
    stop_notebook,
    wait_for_notebook_running,
)
from .playwright_notebooks import run_command_in_notebook
from .images import (
    CustomImageInfo,
    create_image,
    delete_image,
    get_image_detail,
    list_images_by_source,
    save_notebook_as_image,
    update_image,
    wait_for_image_ready,
)
from .rtunnel import RtunnelMissingInContainerError, setup_notebook_rtunnel
from .projects import (
    ProjectInfo,
    check_scheduling_health,
    get_project_detail,
    list_project_owners,
    list_projects,
    select_project,
)
from .models import (
    ModelInfo,
    get_model_detail,
    list_model_versions,
    list_models,
)
from .users import (
    get_user_permissions,
    get_user_quota,
    list_user_api_keys,
)
from .servings import (
    ServingInfo,
    get_serving_configs,
    get_serving_detail,
    list_serving_user_project,
    list_servings,
)

__all__ = [
    # Jobs / users
    "JobInfo",
    "delete_job",
    "get_current_user",
    "get_train_job_workdir",
    "list_job_events",
    "list_job_instance_events",
    "list_job_users",
    "list_jobs",
    # HPC jobs
    "HPCJobInfo",
    "delete_hpc_job",
    "list_hpc_jobs",
    "list_hpc_job_events",
    # Ray jobs (弹性计算 — CPU decode + GPU inference streaming pipelines)
    "RayJobInfo",
    "create_ray_job",
    "delete_ray_job",
    "get_ray_job_detail",
    "list_ray_job_events",
    "list_ray_job_instances",
    "list_ray_job_scaling_histories",
    "list_ray_job_users",
    "list_ray_jobs",
    "stop_ray_job",
    # Metrics (cluster_metric/resource_metric_by_time — 资源视图 tab)
    "INTERVAL_CHOICES",
    "METRIC_TYPES",
    "TASK_TYPE_BY_RESOURCE",
    "MetricGroup",
    "MetricSample",
    "get_resource_metrics_by_time",
    # Availability
    "FullFreeNodeCount",
    "GPUAvailability",
    "find_best_compute_group_accurate",
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_compute_groups",
    # Projects
    "ProjectInfo",
    "check_scheduling_health",
    "get_project_detail",
    "list_project_owners",
    "list_projects",
    "select_project",
    # Images
    "CustomImageInfo",
    "create_image",
    "delete_image",
    "get_image_detail",
    "list_images_by_source",
    "save_notebook_as_image",
    "update_image",
    "wait_for_image_ready",
    # Notebooks
    "ImageInfo",
    "NotebookFailedError",
    "create_notebook",
    "delete_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "get_resource_prices",
    "list_images",
    "list_notebook_compute_groups",
    "list_notebook_events",
    "list_notebook_lifecycle",
    "list_notebook_runs",
    "run_command_in_notebook",
    "RtunnelMissingInContainerError",
    "setup_notebook_rtunnel",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
    # Servings (inference / model deployment)
    "ServingInfo",
    "get_serving_configs",
    "get_serving_detail",
    "list_serving_user_project",
    "list_servings",
    # Model registry
    "ModelInfo",
    "get_model_detail",
    "list_model_versions",
    "list_models",
    # User utilities
    "get_user_permissions",
    "get_user_quota",
    "list_user_api_keys",
]
