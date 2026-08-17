"""`inspire serving api-metrics <name>` — request-traffic time series.

`serving metrics` answers "is the box busy" from `GetTaskMetric` (GPU, CPU,
memory, I/O). This command answers the other half — "is the service actually
serving anything, and how fast" — from `GetServingApiMetric`: QPS, success and
failure rates, latency, time-to-first-token, and token throughput. The two
metric families share no metric name and no backing Action.

Two wire differences from the resource metrics keep this command separate
rather than folded into `build_metrics_command`: this Action takes the whole
`metric_types` list in one request instead of needing a per-metric fan-out, and
it needs no compute-group handle, so there is no detail lookup to do first.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.metrics import INTERVAL_CHOICES
from inspire.platform.web.browser_api.servings import SERVING_API_METRIC_TYPES
from inspire.platform.web.session import SessionExpiredError, get_web_session

# The traffic question an Agent normally has is "are requests arriving, are they
# succeeding, and how slow are they". Everything else is opt-in via --metric.
_CORE_API_METRICS: tuple[str, ...] = ("QPS", "SUCCESS_RATE", "LATENCY")

_API_METRIC_ALIASES: dict[str, str] = {
    "qps": "QPS",
    "success_qps": "SUCCESS_QPS",
    "fail_qps": "FAIL_QPS",
    "success_rate": "SUCCESS_RATE",
    "fail_rate": "FAIL_RATE",
    "requests": "REQUEST_COUNT",
    "latency": "LATENCY",
    "ttft": "TTFT",
    "ttlt": "TTLT",
    "input_tokens": "INPUT_TOKENS",
    "output_tokens": "OUTPUT_TOKENS",
}

_WINDOW_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_window(text: str) -> int:
    raw = text.strip().lower()
    if len(raw) < 2 or raw[-1] not in _WINDOW_MULT or not raw[:-1].isdigit():
        raise click.BadParameter(
            f"unrecognized window '{text}' — use e.g. 30m / 1h / 6h / 24h / 7d"
        )
    return int(raw[:-1]) * _WINDOW_MULT[raw[-1]]


def _resolve_api_metrics(selector: Optional[str]) -> list[str]:
    if not selector or selector.strip().lower() == "core":
        return list(_CORE_API_METRICS)
    if selector.strip().lower() == "all":
        return list(SERVING_API_METRIC_TYPES)
    out: list[str] = []
    for token in selector.split(","):
        token = token.strip()
        if not token:
            continue
        normalized = _API_METRIC_ALIASES.get(token.lower(), token.upper())
        if normalized not in SERVING_API_METRIC_TYPES:
            raise click.BadParameter(
                f"unknown serving API metric '{token}' — valid aliases: "
                f"{', '.join(sorted(_API_METRIC_ALIASES))} or raw: "
                f"{', '.join(SERVING_API_METRIC_TYPES)}"
            )
        if normalized not in out:
            out.append(normalized)
    if not out:
        raise click.BadParameter("no metrics selected")
    return out


def _samples(group: dict[str, Any]) -> list[float]:
    series = group.get("time_series")
    if not isinstance(series, list):
        return []
    values: list[float] = []
    for row in series:
        if not isinstance(row, dict):
            continue
        try:
            values.append(float(row.get("data", 0)))
        except (TypeError, ValueError):
            continue
    return values


def _group_summary(group: dict[str, Any]) -> dict[str, Any]:
    values = _samples(group)
    summary: dict[str, Any] = {
        "metric": str(group.get("metric_type") or ""),
        "count": len(values),
    }
    unit = str(group.get("data_unit") or "").strip()
    if unit:
        summary["unit"] = unit
    label = scrub_raw_ids(str(group.get("group_name") or "")).strip()
    if label and "<redacted>" not in label:
        summary["group"] = label
    if values:
        summary.update(
            {
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "last": values[-1],
                "total": sum(values),
            }
        )
    return summary


def _format_api_metrics(name: str, metrics: list[str], rows: list[dict[str, Any]]) -> str:
    header = f"Serving API Metrics — {scrub_raw_ids(name)}"
    if not rows:
        return f"{header}\nNo API traffic reported in this window."

    columns = [("metric", "Metric")]
    columns.extend(
        (key, label)
        for key, label in (
            ("group", "Group"),
            ("unit", "Unit"),
            ("count", "Samples"),
            ("avg", "Avg"),
            ("max", "Max"),
            ("last", "Last"),
        )
        if any(row.get(key) not in (None, "") for row in rows)
    )

    def _cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        if value in (None, ""):
            return "-"
        if isinstance(value, float):
            return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
        return str(value)

    table_rows = [tuple(_cell(row, key) for key, _label in columns) for row in rows]
    widths = [
        column_width(label, [row[index] for row in table_rows], max_width=32)
        for index, (_key, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _key, label in columns),
        table_rows,
        widths,
        line_char="─",
    )
    missing = [m for m in metrics if not any(row.get("metric") == m for row in rows)]
    body = "\n".join([header, "", rendered[1], rendered[2], *rendered[3:-1]])
    if missing:
        body += f"\nNo data: {', '.join(missing)}"
    return body


@click.command("api-metrics")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--metric",
    "metric_selector",
    default=None,
    help=(
        "Metrics to query: 'core' (default — QPS, SUCCESS_RATE, LATENCY), "
        "'all', or comma-separated aliases/raw names. Aliases: qps, "
        "success_qps, fail_qps, success_rate, fail_rate, requests, latency, "
        "ttft, ttlt, input_tokens, output_tokens."
    ),
)
@click.option(
    "--window",
    default="1h",
    show_default=True,
    help="Lookback ending now. Format: <qty><s|m|h|d>, e.g. 30m / 6h / 7d.",
)
@click.option(
    "--interval",
    type=click.Choice(list(INTERVAL_CHOICES)),
    default="1m",
    show_default=True,
    help="Sample interval.",
)
@pass_context
def serving_api_metrics(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    metric_selector: Optional[str],
    window: str,
    interval: str,
) -> None:
    """Query request traffic for an inference serving.

    \b
    Use it once a deployment is RUNNING to tell "nobody is calling it" apart
    from "it is being called and failing": QPS shows arrival rate, SUCCESS_RATE
    shows how many make it, and LATENCY / TTFT show how slow they are. For GPU,
    CPU and memory utilization use `inspire serving metrics <name>` instead.

    \b
    Examples:
        inspire serving api-metrics qwen-demo --workspace 分布式训练空间
        inspire serving api-metrics qwen-demo --workspace 分布式训练空间 --metric ttft,output_tokens --window 6h
        inspire --json serving api-metrics qwen-demo --workspace 分布式训练空间 --metric all
    """
    from inspire.cli.commands.serving import serving_commands as _sv

    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        metrics = _resolve_api_metrics(metric_selector)
        window_seconds = _parse_window(window)
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    end_ts = int(time.time())
    start_ts = end_ts - window_seconds

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _sv._resolve_workspace_id(workspace, session=session)
        groups = _sv._run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=lambda serving_id, live_session: (
                browser_api_module.get_serving_api_metrics(
                    serving_id,
                    metric_types=metrics,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    interval_second=INTERVAL_CHOICES[interval],
                    session=live_session,
                )
            ),
        )
        rows = [_group_summary(group) for group in groups]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "resource": "serving",
                        "name": scrub_raw_ids(name),
                        "metrics": metrics,
                        "time_range": {
                            "start": start_ts,
                            "end": end_ts,
                            "interval": interval,
                        },
                        "series": rows,
                    }
                )
            )
            return

        click.echo(_format_api_metrics(name, metrics, rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["serving_api_metrics"]
