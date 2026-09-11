"""Public SDK errors, preserving platform and validation messages."""


from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models_notebooks import Notebook


from inspire.platform.errors import (
    InspireError as InspireError,
    ConfigurationError as ConfigurationError,
    AuthenticationError as AuthenticationError,
    AuthenticationCooldownError as AuthenticationCooldownError,
    ClientClosedError as ClientClosedError,
    ClientThreadError as ClientThreadError,
    ValidationError as ValidationError,
    ResourceNotFoundError as ResourceNotFoundError,
    AmbiguousResourceError as AmbiguousResourceError,
    ResolutionIncompleteError as ResolutionIncompleteError,
    TransportError as TransportError,
    SubmissionUncertainError as SubmissionUncertainError,
    MutationUncertainError as MutationUncertainError,
    WaitTimeoutError as WaitTimeoutError,
)


class JobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"Job reached terminal state {job.status}.")
        self.job = job


class NotebookFailedError(InspireError):
    def __init__(self, notebook: Notebook):
        super().__init__(f"Notebook reached terminal state {notebook.status}.")
        self.notebook = notebook


class HPCJobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"HPC job reached terminal state {job.status}.")
        self.job = job


class RayJobFailedError(InspireError):
    def __init__(self, job):
        super().__init__(f"Ray job reached terminal state {job.status}.")
        self.job = job


class ServingFailedError(InspireError):
    def __init__(self, serving):
        self.serving = serving
        super().__init__(f"Serving {serving.name!r} reached {serving.status}.")


class TensorboardFailedError(InspireError):
    def __init__(self, tensorboard):
        self.tensorboard = tensorboard
        super().__init__(f"TensorBoard {tensorboard.name!r} reached {tensorboard.status}.")
