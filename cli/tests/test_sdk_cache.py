"""Catalog caching contracts with fake Action counters; real HTTP stays blocked."""

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_sdk import client as client
from inspire.sdk import (
    InspireClient,
    JobCreateSpec,
    NotebookCreateSpec,
    ImageSelector,
    ImageRef,
    Quota,
    AmbiguousResourceError,
    ResourceNotFoundError,
    ResolutionIncompleteError,
    ValidationError,
    NotebookRef,
    WorkspaceRef,
)
from inspire.sdk.cache import CatalogCache
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api import availability, notebooks, workspaces
from inspire.platform.web.browser_api.images import CustomImageInfo
from inspire.platform.web.browser_api.projects import ProjectInfo


@pytest.fixture
def catalog(client, monkeypatch):
    calls = Counter()
    sources = []
    routes = [{"id": "ws-test", "name": "Workspace", "is_fair_workspace": False}]
    projects = [ProjectInfo("p", "Project", "ws-test", priority_name="10")]
    groups = [
        {
            "id": "g",
            "name": "Group",
            "support_job_type_list": '["distributed_training", "interactive_modeling", "hpc_job", "ray_job"]',
        }
    ]
    prices = [{"quota_id": "q", "gpu_count": 0, "cpu_count": 8, "memory_size_gib": 32}]
    images = [
        CustomImageInfo(
            "i",
            "registry/image:v1",
            "Image",
            "",
            "v1",
            "SOURCE_PRIVATE",
            "READY",
            "",
            "",
            "VISIBILITY_PRIVATE",
        )
    ]

    def counted(action, result):
        def fetch(*args, **kwargs):
            calls[action] += 1
            return result

        return fetch

    def image_source(*, source, **kwargs):
        calls["ListImages"] += 1
        sources.append(source)
        return images if source == "private" else []

    monkeypatch.setattr(workspaces, "try_enumerate_workspaces", counted("GetRoutes", routes))
    # In production GetRoutes populates this session capability, so no second Action is needed.
    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", lambda *a: False)
    monkeypatch.setattr(api, "list_projects", counted("ListProjects", projects))
    monkeypatch.setattr(api, "list_all_projects", counted("ListProjects", projects))
    monkeypatch.setattr(
        availability, "list_compute_groups", counted("ListLogicComputeGroups", groups)
    )
    monkeypatch.setattr(
        api, "list_notebook_compute_groups", counted("ListLogicComputeGroups", groups)
    )
    load_prices = counted("GetLogicComputeGroupResourceSpecPrices", prices)
    monkeypatch.setattr(notebooks, "get_resource_prices", load_prices)
    monkeypatch.setattr(api, "get_resource_prices", load_prices)
    levels = counted("GetScheduleConfig", {})
    monkeypatch.setattr(availability, "get_quota_priority_levels", levels)
    monkeypatch.setattr(api, "get_quota_priority_levels", levels)
    monkeypatch.setattr(api, "list_images_by_source", image_source)
    monkeypatch.setattr(api, "get_image_detail", counted("GetImageById", images[0]))
    monkeypatch.setattr(api, "get_current_user", counted("GetCurrentUser", {"id": "user"}))
    monkeypatch.setattr(api, "create_image", counted("CreateImage", {"image_id": "new"}))
    monkeypatch.setattr(api, "delete_image", counted("DeleteImage", None))
    monkeypatch.setattr(api, "update_image", counted("UpdateImage", None))
    spec = JobCreateSpec(
        name="Example",
        command="echo hello",
        workspace="Workspace",
        project="Project",
        group="Group",
        quota=Quota(0, 8, 32),
        image="Image:v1",
    )
    return SimpleNamespace(
        calls=calls,
        sources=sources,
        routes=routes,
        projects=projects,
        groups=groups,
        prices=prices,
        images=images,
        spec=spec,
        counted=counted,
    )


COLD_JOB = Counter(
    {
        "GetRoutes": 1,
        "ListProjects": 1,
        "ListLogicComputeGroups": 1,
        "ListImages": 4,
        "GetLogicComputeGroupResourceSpecPrices": 1,
        "GetScheduleConfig": 1,
    }
)


def test_second_plan_has_zero_catalog_actions(client, catalog):
    first = client.jobs.plan(catalog.spec)
    assert catalog.calls == COLD_JOB
    catalog.calls.clear()
    assert client.jobs.plan(catalog.spec) == first
    assert catalog.calls == {}
    assert client.cache.stats()["hits"] > 0


def test_ttl_expiry_refetches_every_catalog(client, catalog, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inspire.sdk.cache.time.monotonic", lambda: now[0])
    client.jobs.plan(catalog.spec)
    now[0] += 59
    client.jobs.plan(catalog.spec)
    assert catalog.calls == COLD_JOB
    now[0] += 1
    catalog.calls.clear()
    client.jobs.plan(catalog.spec)
    assert catalog.calls == COLD_JOB


def test_zero_ttl_never_caches(client, catalog):
    with InspireClient(catalog_ttl=0) as other:
        other._transport._session = client._transport._session
        other.jobs.plan(catalog.spec)
        assert catalog.calls == COLD_JOB
        catalog.calls.clear()
        other.jobs.plan(catalog.spec)
        assert catalog.calls == COLD_JOB
        assert other.cache.stats()["entries"] == 0
        assert other.cache.stats()["hits"] == 0


def test_clear_refetches_and_preserves_counters(client, catalog):
    client.jobs.plan(catalog.spec)
    before = client.cache.stats()
    client.cache.clear()
    assert client.cache.stats() == {**before, "entries": 0}
    catalog.calls.clear()
    client.jobs.plan(catalog.spec)
    assert catalog.calls == COLD_JOB


@pytest.mark.parametrize("write", ["register", "delete", "set_visibility", "save_image"])
def test_image_writes_invalidate(client, catalog, monkeypatch, write):
    image = client.images.get("Image:v1", workspace="Workspace")
    if write == "register":
        client.images.register("New", workspace="Workspace")
    elif write == "save_image":
        ref = NotebookRef("nb", client.account, client.base_url, "nb", "ws-test")
        monkeypatch.setattr(api, "estimate_notebook_image_size", lambda **kw: None)
        monkeypatch.setattr(api, "save_notebook_as_image", lambda **kw: {"image_id": "new"})
        client.notebooks.save_image(ref, name="New")
    else:
        getattr(client.images, write)(
            image.ref, **({"visibility": "public"} if write == "set_visibility" else {})
        )
    catalog.calls.clear()
    client.images.get("Image:v1", workspace="Workspace")
    assert catalog.calls == {"ListImages": 4}


def test_selector_reads_only_one_source(client, catalog):
    spec = replace(catalog.spec, image=ImageSelector("Image:v1", source="private"))
    client.jobs.plan(spec)
    assert catalog.calls == {**COLD_JOB, "ListImages": 1}
    assert catalog.sources == ["private"]
    catalog.calls.clear()
    client.jobs.plan(spec)
    assert catalog.calls == {}


@pytest.mark.parametrize("selector", ["ref", "url"])
def test_reference_and_url_skip_image_catalogs(client, catalog, selector):
    image = (
        ImageRef("Image:v1", client.account, client.base_url, "i", "ws-test")
        if selector == "ref"
        else "registry/image:v1"
    )
    client.jobs.plan(replace(catalog.spec, image=image))
    assert catalog.calls["ListImages"] == 0
    assert catalog.calls["GetRoutes"] == 1
    assert catalog.calls["GetImageById"] == (selector == "ref")


def test_cached_ambiguity_and_not_found_keep_semantics(client, catalog):
    catalog.images.append(replace(catalog.images[0], image_id="other"))
    candidates = []
    for _ in range(2):
        with pytest.raises(AmbiguousResourceError) as error:
            client.images.get("Image:v1", workspace="Workspace")
        candidates.append([row.ref.key for row in error.value.candidates])
    assert candidates == [["i", "other"], ["i", "other"]]
    assert catalog.calls == {"GetRoutes": 1, "ListImages": 4}
    with pytest.raises(ResourceNotFoundError):
        client.images.get("Missing:v1", workspace="Workspace")
    assert catalog.calls == {"GetRoutes": 1, "ListImages": 4}


def test_cache_is_per_client(client, catalog):
    client.jobs.plan(catalog.spec)
    with InspireClient() as other:
        other._transport._session = client._transport._session
        other.jobs.plan(catalog.spec)
        assert catalog.calls == COLD_JOB + COLD_JOB
        other.cache.clear()
        assert client.cache.stats()["entries"] > 0


def test_stats_counts_hits_misses_and_live_entries(client, catalog):
    assert client.cache.stats() == {"hits": 0, "misses": 0, "entries": 0}
    client.workspaces.list()
    client.workspaces.get("Workspace")
    assert client.cache.stats() == {"hits": 1, "misses": 1, "entries": 1}


def test_failed_catalog_is_retried(client, catalog, monkeypatch):
    count = 0

    def fetch(**kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise ResolutionIncompleteError("unfinished")
        return catalog.images

    monkeypatch.setattr(api, "list_images_by_source", fetch)
    selector = ImageSelector("Image:v1", source="private")
    with pytest.raises(ResolutionIncompleteError):
        client.images.get(selector, workspace="Workspace")
    assert client.images.get(selector, workspace="Workspace").ref.key == "i"
    assert count == 2


def test_missing_catalog_identity_is_not_cached(client, catalog):
    catalog.projects[0].project_id = ""
    with pytest.raises(ResolutionIncompleteError):
        client.projects.get("Project", workspace="Workspace")
    catalog.projects[0].project_id = "p"
    assert client.projects.get("Project", workspace="Workspace").ref.key == "p"
    assert catalog.calls["ListProjects"] == 2


def test_partial_sources_retry_only_failed_source(client, catalog, monkeypatch):
    original = api.list_images_by_source
    failed = [True]

    def fetch(*, source, **kwargs):
        if source == "private" and failed[0]:
            failed[0] = False
            raise ResolutionIncompleteError("temporary")
        return original(source=source, **kwargs)

    monkeypatch.setattr(api, "list_images_by_source", fetch)
    with pytest.raises(ResolutionIncompleteError):
        client.images.get("Image:v1", workspace="Workspace")
    client.images.get("Image:v1", workspace="Workspace")
    assert catalog.sources == ["official", "public", "project", "private"]


def test_scope_and_workload_keys(client, catalog):
    ws = client.workspaces.get("Workspace")
    other_ws = replace(
        ws, ref=WorkspaceRef("Other", client.account, client.base_url, "other", "other")
    )
    client.projects._all()
    client.projects._all(ws)
    client.projects._all(other_ws)
    client.compute_groups._all(ws)
    client.notebooks._groups(ws)
    client.hpc._groups(ws)
    client.notebooks._prices(ws, "g")
    client.notebooks._prices(other_ws, "g")
    client.notebooks._prices(ws, "other-group")
    client.hpc._prices(ws, "g")
    client.ray._prices(ws, "g")
    client.notebooks._priority_levels(ws)
    client.servings._priority_levels(ws)
    assert catalog.calls["ListProjects"] == 3
    assert catalog.calls["ListLogicComputeGroups"] == 1
    assert catalog.calls["GetLogicComputeGroupResourceSpecPrices"] == 5
    assert catalog.calls["GetScheduleConfig"] == 2


def test_returned_data_cannot_modify_snapshot(client, catalog):
    ws = client.workspaces.get("Workspace")
    client.notebooks._groups(ws)[0]["name"] = "Changed"
    assert client.notebooks._groups(ws)[0]["name"] == "Group"


def test_notebook_plan_uses_catalog_cache(client, catalog):
    spec = NotebookCreateSpec(
        "Example",
        "Workspace",
        "Project",
        "Group",
        Quota(0, 8, 32),
        ImageSelector("Image:v1", source="private"),
    )
    first = client.notebooks.plan(spec)
    assert catalog.calls == {
        "GetRoutes": 1,
        "ListProjects": 1,
        "ListLogicComputeGroups": 1,
        "ListImages": 1,
        "GetLogicComputeGroupResourceSpecPrices": 1,
        "GetScheduleConfig": 1,
    }
    catalog.calls.clear()
    assert client.notebooks.plan(spec) == first
    assert catalog.calls == {}


def test_current_user_cached_but_not_notebook_pages_or_details(client, catalog, monkeypatch):
    monkeypatch.setattr(api, "list_notebooks", catalog.counted("ListNotebooks", ([], 0)))
    monkeypatch.setattr(
        api, "get_notebook_detail", catalog.counted("GetNotebook", {"status": "RUNNING"})
    )
    ref = NotebookRef("nb", client.account, client.base_url, "nb", "ws-test")
    for _ in range(2):
        client.notebooks.list("Workspace")
        client.notebooks.get(ref)
        client.account_info.current()
    assert catalog.calls == {
        "GetRoutes": 1,
        "GetCurrentUser": 1,
        "ListNotebooks": 2,
        "GetNotebook": 2,
    }


def test_job_pages_are_not_cached(client, catalog, monkeypatch):
    from inspire.platform.web.browser_api import jobs

    monkeypatch.setattr(jobs, "list_jobs", catalog.counted("ListJobs", ([], 0)))
    for _ in range(2):
        client.jobs.list("Workspace")
    assert catalog.calls == {"GetRoutes": 1, "ListJobs": 2}


def test_session_renewal_keeps_catalog_snapshots(client, catalog):
    client.jobs.plan(catalog.spec)
    client._transport._session = replace(client._transport._session)
    catalog.calls.clear()
    client.jobs.plan(catalog.spec)
    assert catalog.calls == {}


@pytest.mark.parametrize("ttl", [-1, float("nan"), float("inf"), True, "60"])
def test_invalid_ttl_rejected(ttl):
    with pytest.raises(ValidationError):
        CatalogCache(ttl)


def test_fair_flag_expires_even_with_a_session_snapshot(client, catalog, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inspire.sdk.cache.time.monotonic", lambda: now[0])
    ws = client.workspaces.get("Workspace")
    flags = {"ws-test": True}
    client._transport._session.all_workspace_fair_scheduling = flags
    calls = []

    def fair(session, key):
        assert key not in session.all_workspace_fair_scheduling
        calls.append(key)
        return len(calls) == 1

    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", fair)
    assert client.jobs._fair_scheduling(ws) is True
    assert client.jobs._fair_scheduling(ws) is True
    now[0] += 60
    assert client.jobs._fair_scheduling(ws) is False
    assert len(calls) == 2


def test_stats_excludes_expired_entries(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("inspire.sdk.cache.time.monotonic", lambda: now[0])
    cache = CatalogCache(ttl=1)
    cache._get(("kind", "account", "origin"), lambda: False)
    now[0] = 1.0
    assert cache.stats() == {"hits": 0, "misses": 1, "entries": 0}


@pytest.mark.parametrize("kind", ["hpc", "ray"])
def test_compute_job_plans_share_catalogs(client, catalog, kind):
    from inspire.sdk import HPCJobCreateSpec, RayJobCreateSpec

    spec = (
        HPCJobCreateSpec(
            "Example", "echo hello", "Workspace", "Project", "Group", Quota(0, 8, 32), "Image:v1"
        )
        if kind == "hpc"
        else RayJobCreateSpec(
            "Example",
            "echo hello",
            "Workspace",
            "Project",
            "Group",
            Quota(0, 8, 32),
            "Image:v1",
            workers=["name=worker;image=Image:v1;group=Group;quota=0,8,32;min=1;max=1"],
        )
    )
    facade = getattr(client, kind)
    first = facade.plan(spec)
    assert catalog.calls == {
        "GetRoutes": 1,
        "ListProjects": 1,
        "ListLogicComputeGroups": 1,
        "GetLogicComputeGroupResourceSpecPrices": 1,
        "ListImages": 4,
    }
    catalog.calls.clear()
    assert facade.plan(spec) == first
    assert catalog.calls == {}


def test_image_get_warm_cache_has_no_requests(client, catalog):
    client.images.get("Image:v1", workspace="Workspace")
    assert catalog.calls == {"GetRoutes": 1, "ListImages": 4}
    catalog.calls.clear()
    client.images.get("Image:v1", workspace="Workspace")
    assert catalog.calls == {}
