"""Data plaza discovery and console mount validation."""

from __future__ import annotations

from collections.abc import Sequence

from inspire.platform.web import plaza
from inspire.platform.web.browser_api import datasets as mounts_api
from inspire.services.catalog import dataset_catalog as views
from .exceptions import ValidationError
from .models import DatasetMount, Page, WorkspaceRef
from .models_resources import (
    DatasetInfo,
    DatasetDetail,
    DatasetRef,
    DatasetTag,
    DatasetTagRef,
    DatasetVersion,
    DatasetVersionRef,
    DatasetApplication,
    DatasetApplicationRef,
    DatasetValidation,
)
from .resources import Service, operation, positive


class Datasets(Service):
    @operation
    def list(
        self,
        keyword: str | None = None,
        *,
        tag: str | Sequence[str] | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[DatasetInfo]:
        tags = [tag] if isinstance(tag, str) else list(tag or ())
        tag_ids = plaza.resolve_tag_ids(tags, session=self.session)
        rows = self._collect_pages(
            lambda **paging: plaza.list_datasets(
                keyword=keyword, tag_ids=tag_ids, session=self.session, **paging
            ),
            lambda x: x.dataset_id,
        )
        items = [
            DatasetInfo.from_view(
                views.dataset_row(x), ref=self._make_ref(DatasetRef, x.code, x.dataset_id)
            )
            for x in rows
        ]
        return self._page(items, limit=limit, cursor=cursor, query=(keyword, tags))

    @operation
    def get(self, ref: str | DatasetRef) -> DatasetDetail:
        if isinstance(ref, DatasetRef):
            self.client._validate_ref(ref, DatasetRef)
            key = int(ref.key)
        else:
            key = plaza.resolve_dataset_by_code(ref, session=self.session).dataset_id
        detail = plaza.get_dataset_detail(key, session=self.session)
        version_views = views.version_views(detail)
        versions = tuple(
            DatasetVersion.from_view(
                view,
                ref=self._make_ref(DatasetVersionRef, version.code, version.version_id)
                if version.version_id
                else None,
            )
            for view, version in zip(version_views, detail.versions)
        )
        return DatasetDetail.from_view(
            {**views.dataset_detail_view(detail), "versions": version_views},
            ref=self._make_ref(DatasetRef, detail.code, key),
            versions=versions,
        )

    @operation
    def tags(self) -> tuple[DatasetTag, ...]:
        return tuple(
            DatasetTag.from_view(
                {"name": x.name, "category": x.category},
                ref=self._make_ref(DatasetTagRef, x.name, x.tag_id),
            )
            for x in plaza.list_dataset_tags(session=self.session)
        )

    @operation
    def applications(
        self,
        name: str | None = None,
        *,
        to_approve: bool = False,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[DatasetApplication]:
        if name is not None:
            positive(limit)
            offset, query = self._cursor_offset(cursor, (name, to_approve, keyword))
            rows = plaza.find_dataset_applications(
                name, incoming=to_approve, session=self.session, limit=offset + limit + 1
            )
        else:
            lister = plaza.list_dataset_approvals if to_approve else plaza.list_dataset_applications
            rows = self._collect_pages(
                lambda **paging: lister(keyword=keyword, session=self.session, **paging),
                lambda x: x.application_id,
            )
        items = [
            DatasetApplication.from_view(
                views.application_detail_view(x)
                if name is not None
                else views.application_row(x, incoming=to_approve),
                ref=self._make_ref(DatasetApplicationRef, x.dataset, x.application_id),
            )
            for x in rows
        ]
        if name is not None:
            end = offset + limit
            return Page(
                tuple(items[offset:end]),
                self._encode_cursor(end, query) if len(items) > end else None,
                None,
            )
        return self._page(items, limit=limit, cursor=cursor, query=(name, to_approve, keyword))

    @operation
    def validate(
        self,
        specs: Sequence[str | DatasetMount],
        *,
        workspace: str | WorkspaceRef,
    ) -> tuple[DatasetValidation, ...]:
        from inspire.services.catalog.datasets import DatasetSpecError, parse_dataset_spec

        if isinstance(specs, str):
            raise ValidationError("specs must be a sequence, not a string.")
        mounts = []
        seen = set()
        for spec in specs:
            mount = parse_dataset_spec(spec, field="specs") if isinstance(spec, str) else spec
            key = (mount.dataset, mount.version)
            if key in seen:
                raise DatasetSpecError(
                    f"specs {mount.dataset}:{mount.version} was given more than once"
                )
            seen.add(key)
            mounts.append(mount)
        ws = self.client.workspaces.get(workspace)
        verdicts = mounts_api.validate_dataset_mounts(
            mounts, workspace_id=ws.ref.key, session=self.session
        )
        return tuple(
            DatasetValidation(
                x.dataset, x.version, x.ok, x.mount_path if x.ok else "", "" if x.ok else x.error
            )
            for x in verdicts
        )
