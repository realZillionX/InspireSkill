"""TensorBoard lifecycle and scalar data, sharing the CLI service cores."""

from __future__ import annotations
import time
from inspire.platform.web.flow import call, perform_sync
from typing import Sequence
from uuid import uuid4
from inspire.services.tensorboard import tensorboards as core
from inspire.services.tensorboard import tensorboard_data as data_core
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api.tensorboards import tensorboard_app_url
from .resources import Service, operation, exact
from .models import ComputeGroupRef, JobRef, Resource, WorkspaceRef, Page
from .models_observations import (
    TensorboardTags, TensorboardScalars, TensorboardScalarSeries, TensorboardScalarPoint,
)
from .models_serving import Tensorboard, TensorboardRef, TensorboardCreateSpec, TensorboardHandle
from .exceptions import ValidationError, SubmissionUncertainError, TensorboardFailedError
from .compute_jobs import duration
from inspire.services.tensorboard.tensorboard_status import normalize_status, WAIT_TARGETS, TERMINAL_STATUSES


class Tensorboards(Service):
    def _board(self, row, workspace_id):
        return Tensorboard(
            row.name,
            self._make_ref(TensorboardRef, row.name, row.tb_id, workspace_id),
            normalize_status(row.status),
            row.summary_path,
            row.url,
            row.job_name,
            row.project_name,
            row.compute_group_name,
            row.auto_stop_ms,
            row.running_time_ms,
            row.created_at,
            row.job_id,
        )

    def _all(self, ws, status=None, keyword=None):
        rows = self._collect_pages(
            lambda page, page_size: api.list_tensorboards(
                workspace_id=ws.ref.key,
                status=status,
                keyword=keyword,
                page_num=page,
                page_size=page_size,
                session=self.session,
            ),
            lambda row: row.tb_id,
        )
        return [self._board(row, ws.ref.key) for row in rows]

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        job: str | JobRef | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Tensorboard]:
        ws = self.client.workspaces.get(workspace)
        rows = self._all(ws, status, keyword)
        if job is not None:
            job_name = job.name if isinstance(job, JobRef) else job
            if isinstance(job, JobRef):
                self.client._validate_ref(job, JobRef, ws.ref.key)
            rows = [
                row
                for row in rows
                if (
                    row.job_id == job.key
                    if isinstance(job, JobRef)
                    else row.job.casefold() == job_name.casefold()
                )
            ]
        return self._page(rows, limit=limit, cursor=cursor, query=(ws.ref.key, status, job, keyword))

    def _resolve(self, ref, workspace=None):
        if isinstance(ref, TensorboardRef):
            ws = self.client.workspaces.get(workspace).ref.key if workspace is not None else None
            self.client._validate_ref(ref, TensorboardRef, ws)
            return ref
        if workspace is None:
            raise ValidationError("workspace is required when selecting a TensorBoard by name.")
        ws = self.client.workspaces.get(workspace)
        return exact(self._all(ws, keyword=ref), ref, TensorboardRef, self.client, ws.ref.key).ref

    @operation
    def get(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> Tensorboard:
        resolved = self._resolve(ref, workspace)
        return self._board(api.get_tensorboard(resolved.key, session=self.session), resolved.workspace_id)

    @operation
    def status(
        self, refs: Sequence[str | TensorboardRef], *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[Tensorboard, ...]:
        """Return one current snapshot per reference, in input order."""
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        return tuple(self.get(ref, workspace=workspace) for ref in refs)

    @operation
    def create(
        self,
        spec: TensorboardCreateSpec,
        *,
        operation_id: str | None = None,
    ) -> TensorboardHandle:
        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        ws = self.client.workspaces.get(spec.workspace)
        project = self.client.projects.get(spec.project, workspace=ws.ref)
        group = spec.group
        if isinstance(group, ComputeGroupRef):
            group = self.client.compute_groups.get(group, workspace=ws.ref).name

        def select_group(rows):
            resources = [
                Resource(
                    str(row.get("name") or ""),
                    self._make_ref(
                        ComputeGroupRef,
                        str(row.get("name") or ""),
                        str(row.get("logic_compute_group_id") or row.get("id") or ""),
                        ws.ref.key,
                    ),
                )
                for row in rows
            ]
            return exact(resources, spec.group, ComputeGroupRef, self.client, ws.ref.key).ref.key

        group_id = core.resolve_group_id(
            self.session,
            workspace_id=ws.ref.key,
            group=group,
            select=select_group,
            groups_loader=lambda: [row[1] for row in self.client.compute_groups._all(ws)],
        )
        job_id = ""
        if isinstance(spec.job, JobRef):
            self.client._validate_ref(spec.job, JobRef, ws.ref.key)
            job_id = spec.job.key
        elif spec.job:
            candidates = core.job_candidates(
                session=self.session,
                name=spec.job,
                workspace_id=ws.ref.key,
                user_id_loader=self._current_user_id,
            )
            job_id = exact(
                [
                    Resource(
                        row["name"], self._make_ref(JobRef, row["name"], row["id"], ws.ref.key)
                    )
                    for row in candidates
                ],
                spec.job,
                JobRef,
                self.client,
                ws.ref.key,
            ).ref.key
        hours = 24.0 if spec.auto_stop_hours is None else spec.auto_stop_hours
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="TensorBoards"):
            api.create_tensorboard(
                name=spec.name,
                workspace_id=ws.ref.key,
                project_id=project.ref.key,
                logic_compute_group_id=group_id,
                summary_path=spec.summary_path or "",
                auto_stop_ms=int(hours * 3_600_000),
                job_id=job_id,
                session=session,
            )
        try:
            board = core.find_created_board(session, workspace_id=ws.ref.key, name=spec.name)
        except Exception as exc:
            raise SubmissionUncertainError(identifier, inspect="TensorBoards") from exc
        if board is None or not board.tb_id:
            raise SubmissionUncertainError(identifier, inspect="TensorBoards")
        return TensorboardHandle(
            board.name,
            self._make_ref(TensorboardRef, board.name, board.tb_id, ws.ref.key),
            identifier,
        )

    def _mutate(self, ref, action, workspace):
        ref = self._resolve(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            action(ref.key, session=session)

    @operation
    def start(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        self._mutate(ref, api.start_tensorboard, workspace)

    @operation
    def stop(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        self._mutate(ref, api.stop_tensorboard, workspace)

    @operation
    def delete(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        self._mutate(ref, api.delete_tensorboard, workspace)

    def wait(
        self,
        ref: str | TensorboardRef,
        *,
        target: str = "RUNNING",
        raise_on_failure: bool = False,
        timeout: float = 60,
        poll_interval: float = 3,
        workspace: str | WorkspaceRef | None = None,
    ) -> Tensorboard:
        """Wait for a lifecycle target; strip whitespace, ignore case and tb_status_.

        Unknown targets raise ValidationError listing accepted values before polling."""
        duration(timeout, "timeout")
        duration(poll_interval, "poll_interval")
        target = normalize_status(target)
        if target not in WAIT_TARGETS:
            raise ValidationError(f"target must be one of: {', '.join(sorted(WAIT_TARGETS))}.")
        with self.client._transport.scope(timeout=timeout):
            resolved = self._resolve(ref, workspace)
            while True:
                self.client._transport.remaining()
                board = self.get(resolved)
                if board.status == target:
                    return board
                if board.status in TERMINAL_STATUSES:
                    if raise_on_failure:
                        raise TensorboardFailedError(board)
                    return board

                perform_sync(call(time.sleep, min(poll_interval, self.client._transport.remaining())))

    def _live(self, ref, workspace):
        board = self.get(ref, workspace=workspace)
        if board.status != "RUNNING":
            raise ValidationError(
                f"TensorBoard {board.name!r} is {board.status}; only a running board serves data."
            )
        return board

    @operation
    def tags(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> TensorboardTags:
        board = self._live(ref, workspace)
        view = dict(
            name=board.name,
            summary_path=board.summary_path,
            runs=api.read_tensorboard_runs(board.url, session=self.session),
            scalar_tags=api.read_tensorboard_scalar_tags(board.url, session=self.session),
        )
        return TensorboardTags.from_view(
            view, runs=tuple(view["runs"]),
            scalar_tags={run: tuple(tags) for run, tags in view["scalar_tags"].items()},
        )

    @operation
    def scalars(
        self,
        ref: str | TensorboardRef,
        *,
        tag: str = "",
        run: str | None = None,
        points: int | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> TensorboardScalars:
        if points is not None and (not isinstance(points, int) or points < 0):
            raise ValidationError("points must be a non-negative integer.")
        board = self._live(ref, workspace)
        series = data_core.collect_series(self.session, board, run=run or "", tag=tag)
        view = dict(
            name=board.name,
            summary_path=board.summary_path,
            series=[
                {
                    **{k: v for k, v in row.items() if k != "points"},
                    **({"points": data_core.tail(row["points"], points)} if points else {}),
                }
                for row in series
            ],
        )
        return TensorboardScalars.from_view(
            view, series=tuple(
                TensorboardScalarSeries.from_view(
                    row, points=tuple(TensorboardScalarPoint(*point) for point in row.get("points", []))
                )
                for row in view["series"]
            ),
        )

    @operation
    def url(
        self, ref: str | TensorboardRef, *, workspace: str | WorkspaceRef | None = None
    ) -> str:
        return tensorboard_app_url(self.get(ref, workspace=workspace).url)
