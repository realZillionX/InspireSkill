"""Map local creation name validation to the CLI validation contract."""

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error
from inspire.services.utils.workload_names import WorkloadNameError, validate_workload_name


def validate_create_name(ctx: Context, name: str | None, kind: str) -> None:
    try:
        validate_workload_name(name or "", kind)
    except WorkloadNameError as exc:
        exit_with_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
