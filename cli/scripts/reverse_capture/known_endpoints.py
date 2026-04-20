"""Known Browser API endpoints — source of truth for diffing captures.

Update this list whenever a new endpoint is added to `references/browser-api.md`
or wrapped by the CLI. The diffing tool (`analyze.py`) compares freshly-captured
traffic against this set to surface newly-appeared or newly-dead endpoints.
"""

from __future__ import annotations

import re

# (METHOD, path_template) — path templates use `{id}` for any variable segment.
KNOWN: set[tuple[str, str]] = {
    # --- User / Permissions ---
    ("GET", "/api/v1/user/detail"),
    ("GET", "/api/v1/user/routes/{id}"),
    ("GET", "/api/v1/user/{id}"),
    ("GET", "/api/v1/user/list"),
    ("GET", "/api/v1/user/permissions/{id}"),
    ("GET", "/api/v1/user/my-api-key/list"),
    ("GET", "/api/v1/user/quota"),

    # --- Project ---
    ("POST", "/api/v1/project/list"),
    ("POST", "/api/v1/project/list_v2"),
    ("POST", "/api/v1/project/list_for_page"),
    ("GET", "/api/v1/project/{id}"),
    ("GET", "/api/v1/project/owners"),

    # --- Workspace ---
    ("POST", "/api/v1/workspace/list"),

    # --- Notebook ---
    ("POST", "/api/v1/notebook/create"),
    ("POST", "/api/v1/notebook/operate"),
    ("POST", "/api/v1/notebook/list"),
    ("POST", "/api/v1/notebook/users"),
    ("GET", "/api/v1/notebook/{id}"),
    ("GET", "/api/v1/notebook/status"),
    ("POST", "/api/v1/notebook/events"),
    ("POST", "/api/v1/lifecycle/list"),
    ("POST", "/api/v1/run_index/list"),
    ("POST", "/api/v1/resource_prices/logic_compute_groups/"),
    ("GET", "/api/v1/notebook/schedule/{id}"),
    ("GET", "/api/v1/notebook/schedule"),

    # --- Image ---
    ("POST", "/api/v1/image/list"),
    ("GET", "/api/v1/image/{id}"),
    ("POST", "/api/v1/image/create"),
    ("DELETE", "/api/v1/image/{id}"),
    ("POST", "/api/v1/mirror/save"),
    ("POST", "/api/v1/image/update"),

    # --- Train Job ---
    ("POST", "/api/v1/train_job/list"),
    ("POST", "/api/v1/train_job/detail"),
    ("POST", "/api/v1/train_job/users"),
    ("POST", "/api/v1/train_job/workdir"),
    ("POST", "/api/v1/train_job/job_event_list"),
    ("POST", "/api/v1/train_job/instance_list"),
    ("POST", "/api/v1/train_job/events/list"),
    ("POST", "/api/v1/logs/train"),

    # --- HPC Jobs ---
    ("POST", "/api/v1/hpc_jobs/list"),
    ("GET", "/api/v1/hpc_jobs/{id}"),
    ("POST", "/api/v1/hpc_jobs/events/list"),
    ("POST", "/api/v1/hpc_jobs/instances/list"),
    ("POST", "/api/v1/logs/hpc"),

    # --- Resources / Compute groups ---
    ("POST", "/api/v1/logic_compute_groups/list"),
    ("GET", "/api/v1/compute_resources/logic_compute_groups/{id}"),
    ("GET", "/api/v1/logic_compute_groups/{id}"),
    ("POST", "/api/v1/cluster_nodes/list"),
    ("GET", "/api/v1/cluster_nodes/workspace/{id}"),
    ("POST", "/api/v1/compute_groups/list"),  # exists (400 on `{workspace_id}` body); body schema unknown

    # --- Model (registry) ---
    ("POST", "/api/v1/model/list"),
    ("POST", "/api/v1/model/detail"),
    ("GET", "/api/v1/model/{id}"),
    ("GET", "/api/v1/model/{id}/versions"),
    ("POST", "/api/v1/model/create"),

    # --- Inference servings ---
    ("POST", "/api/v1/inference_servings/list"),
    ("POST", "/api/v1/inference_servings/user_project/list"),
    ("GET", "/api/v1/inference_servings/configs/workspace/{id}"),
    ("GET", "/api/v1/inference_servings/detail"),

    # --- SSH keys ---
    ("POST", "/api/v1/ssh/list"),
    ("GET", "/api/v1/ssh/keys"),
    ("GET", "/api/v1/ssh/my_keys"),
    ("GET", "/api/v1/ssh/public_keys"),
    ("POST", "/api/v1/ssh/create"),
}

# Endpoints that used to exist but were retired by the platform. Listed here
# so that `analyze.py` can call them out if a new capture unexpectedly
# resurrects one (signal for a rollback on the platform side).
STALE_SINCE_2026_04: set[tuple[str, str]] = {
    ("GET", "/api/v1/notebook/{id}/events"),
    ("GET", "/api/v1/notebook/event/{id}"),
    ("POST", "/api/v1/notebook/compute_groups"),
}


_PREFIXED_ID = re.compile(
    r"/(?:ws|job|hpc-job|sv|project|lcg|cg|image|img|nb|notebook|mdl|model|spec|tk|quota|usr|user|tag|team|org)"
    r"-[0-9a-zA-Z_-]{4,}(?=/|$|\?)"
)
_UUID = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$|\?)")
_NUM = re.compile(r"/\d{4,}(?=/|$|\?)")
_HEX = re.compile(r"/[0-9a-f]{16,}(?=/|$|\?)")


def normalize_path(path: str) -> str:
    """Collapse id-bearing path segments to `{id}` so paths group cleanly."""
    p = path.split("?")[0]
    p = _PREFIXED_ID.sub("/{id}", p)
    p = _UUID.sub("/{id}", p)
    p = _NUM.sub("/{id}", p)
    p = _HEX.sub("/{id}", p)
    return p


def is_known(method: str, path: str) -> bool:
    return (method.upper(), normalize_path(path)) in KNOWN


if __name__ == "__main__":
    tests = [
        ("GET", "/api/v1/user/detail", True),
        ("GET", "/api/v1/user/routes/ws-1177d2a5-aef0-40d3-8777-fed9af13affc", True),
        ("GET", "/api/v1/notebook/facfdc82-b52d-414f-8a9f-cc918c26acbd", True),
        ("POST", "/api/v1/notebook/events", True),
        ("GET", "/api/v1/notebook/event/facfdc82-b52d-414f-8a9f-cc918c26acbd", False),
        ("POST", "/api/v1/completely_new_endpoint", False),
    ]
    for method, path, want in tests:
        got = is_known(method, path)
        mark = "✓" if got == want else "✗"
        print(f"{mark} {method:6s} {path:70s} → known={got} (want={want})")
