"""Model registry reads using the CLI's public views."""

from __future__ import annotations

import builtins
from typing import Any, Sequence
from inspire.platform.web import browser_api
from inspire.services.catalog import models as views
from inspire.services.utils.collections import bound_collection
from .exceptions import ResourceNotFoundError, ValidationError
from .models import Page, WorkspaceRef, ProjectRef
from .models_resources import ModelRef, ModelInfo, ModelStatus, ModelVersion, ModelDeployConfig
from .models_serving import ModelRegisterHandle
from .resources import Service, operation, exact


class Models(Service):
    def _all(self, workspace, project=None, keyword=None):
        if isinstance(workspace, str) and workspace.strip().casefold() == "all":
            workspace = "all"
        workspaces = (
            self.client.workspaces._all()
            if workspace == "all"
            else [self.client.workspaces.get(workspace)]
        )
        user_id = self._current_user_id()
        items: list[tuple[ModelInfo, Any]] = []
        matched = project is None
        for ws in workspaces:
            try:
                project_id = (
                    self.client.projects.get(project, workspace=ws.ref).ref.key
                    if project is not None
                    else None
                )
            except ResourceNotFoundError:
                if workspace == "all":
                    continue
                raise
            matched = True
            kwargs = dict(
                workspace_id=ws.ref.key,
                keyword=keyword,
                project_ids=[project_id] if project_id else None,
                user_id=user_id,
                session=self.session,
            )
            rows = self._collect_pages(
                lambda **paging: browser_api.list_models(**paging, **kwargs), lambda x: x.model_id
            )
            items.extend(
                (
                    ModelInfo.from_view(
                        views.model_list_view(x, workspace=ws.name),
                        ref=self._make_ref(ModelRef, x.name, x.model_id, ws.ref.key),
                    ),
                    x,
                )
                for x in rows
            )
        if not matched:
            raise ResourceNotFoundError(
                f"Unknown project name {project!r} in the requested workspaces."
            )
        if workspace == "all":
            items.sort(key=lambda x: str(x[1].updated_at or x[1].created_at or ""), reverse=True)
        return items

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        project: str | ProjectRef | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[ModelInfo]:
        return self._page(
            [x[0] for x in self._all(workspace, project, keyword)],
            limit=limit,
            cursor=cursor,
            query=(workspace, project, keyword),
        )

    @operation
    def get(
        self,
        ref: str | ModelRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
    ) -> ModelInfo:
        if workspace is None:
            if not isinstance(ref, ModelRef):
                raise ValidationError("workspace is required when selecting a model by name.")
            self.client._validate_ref(ref, ModelRef)
            workspace = WorkspaceRef("", ref.account, ref.base_url, ref.workspace_id, ref.workspace_id)
        ws = self.client.workspaces.get(workspace)
        return exact(
            [x[0] for x in self._all(ws.ref, project)],
            ref,
            ModelRef,
            self.client,
            ws.ref.key,
        )

    def _ref(self, selector, workspace, project):
        if isinstance(selector, ModelRef):
            ws_id = self.client.workspaces.get(workspace).ref.key if workspace is not None else None
            self.client._validate_ref(selector, ModelRef, ws_id)
            return selector
        return self.get(selector, workspace=workspace, project=project).ref

    @operation
    def status(
        self, refs: Sequence[str | ModelRef], *, workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
    ) -> tuple[ModelInfo, ...]:
        """Return the same model as get for each reference, in input order."""
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        return tuple(self.get(ref, workspace=workspace, project=project) for ref in refs)

    @operation
    def detail(
        self, ref: str | ModelRef, *, workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
    ) -> ModelStatus:
        """Return detailed status, version compatibility and serving usage."""
        resolved = self._ref(ref, workspace, project)
        kwargs = dict(session=self.session, workspace_id=resolved.workspace_id)
        data = browser_api.get_model_detail(resolved.key, **kwargs)
        records = browser_api.list_model_version_records(resolved.key, **kwargs)
        compatibility = browser_api.get_model_vllm_compatibility(resolved.key, **kwargs)
        view = views.model_detail_view(resolved.name, data, records, vllm_compatibility=compatibility)
        pending = browser_api.check_model_inference_serving_pending(model_id=resolved.key, **kwargs)
        view["pending_serving"] = pending.get("has_pending_serving") is True
        reported = views.reported_version(data, records)
        if reported is not None:
            servings, _ = browser_api.list_model_inference_servings(
                model_id=resolved.key,
                version=reported,
                page=1,
                page_size=views.SERVING_PAGE_SIZE,
                **kwargs,
            )
            page = bound_collection(views.serving_views(servings), limit=20)
            view["servings"] = page.items
            view.update({f"servings_{k}": v for k, v in page.metadata().items()})
        in_use = views.other_versions_in_use(records, reported=reported)
        if in_use:
            view["other_versions_in_use"] = in_use
        return ModelStatus.from_view(view, ref=resolved)

    @operation
    def versions(
        self,
        ref: str | ModelRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
    ) -> tuple[ModelVersion, ...]:
        resolved = self._ref(ref, workspace, project)
        records = browser_api.list_model_version_records(
            resolved.key, session=self.session, workspace_id=resolved.workspace_id
        )
        compatibility = browser_api.get_model_vllm_compatibility(
            resolved.key, session=self.session, workspace_id=resolved.workspace_id
        )
        return tuple(
            ModelVersion.from_view(x)
            for x in views.model_version_views(records, vllm_compatibility=compatibility)
        )

    @operation
    def deploy_config(
        self,
        ref: str | ModelRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
        version: int | None = None,
    ) -> ModelDeployConfig:
        model = self.get(ref, workspace=workspace, project=project)
        if version is None:
            version = views.version_number(model.version)
        if version is None:
            raise ValidationError("Could not infer the model version. Pass version explicitly.")
        kwargs = dict(version=version, session=self.session, workspace_id=model.ref.workspace_id)
        recommended = browser_api.get_model_recommended_config(model.ref.key, **kwargs)
        compatible = browser_api.check_model_vllm_compatible(model.ref.key, **kwargs)
        view = views.model_deploy_config_view(model.name, version, recommended, compatible)
        return ModelDeployConfig.from_view(view)

    @operation
    def register(
        self,
        name: str,
        *,
        source_path: str,
        workspace: str | WorkspaceRef,
        project: str | ProjectRef,
        type: str | builtins.list[str] | None = None,
        tag: str | builtins.list[str] | None = None,
        description: str | None = None,
        operation_id: str | None = None,
    ) -> ModelRegisterHandle:
        from uuid import uuid4
        from inspire.services.catalog.model_writes import created_model_id
        from .exceptions import SubmissionUncertainError

        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        ws = self.client.workspaces.get(workspace)
        proj = self.client.projects.get(project, workspace=ws.ref)
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="model versions"):
            result = browser_api.create_model(
                name=name,
                project_id=proj.ref.key,
                workspace_id=ws.ref.key,
                model_source_path=source_path,
                model_type=[type] if isinstance(type, str) else type,
                tags=[tag] if isinstance(tag, str) else tag,
                description=description or "",
                model_source_type=1,
                session=session,
            )
        key = created_model_id(result)
        if not key:
            raise SubmissionUncertainError(identifier, inspect="model versions")
        return ModelRegisterHandle(name, self._make_ref(ModelRef, name, key, ws.ref.key), identifier)

    @operation
    def delete(
        self,
        ref: str | ModelRef,
        *,
        force: bool = False,
        workspace: str | WorkspaceRef | None = None,
        project: str | ProjectRef | None = None,
    ) -> None:
        from inspire.services.catalog.model_writes import model_usage

        if isinstance(ref, ModelRef):
            ws = self.client.workspaces.get(workspace).ref.key if workspace is not None else None
            self.client._validate_ref(ref, ModelRef, ws)
            resolved = ref
        else:
            if workspace is None:
                raise ValidationError("workspace is required when selecting a model by name.")
            resolved = self._ref(ref, workspace, project)
        assert isinstance(resolved, ModelRef)
        session = self.session
        if not force:
            references, pending = model_usage(
                resolved.key, session=session, workspace_id=resolved.workspace_id
            )
            if references or pending:
                from inspire.services.catalog.model_writes import in_use_message

                raise ValidationError(in_use_message(resolved.name, references, pending=pending))
        with self.client._transport.single_send():
            browser_api.delete_model(resolved.key, session=session, workspace_id=resolved.workspace_id)
