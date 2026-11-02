# Optional agent operation examples

These standalone examples supplement the [operations guide](../../references/agent-ops.md).
They are not CLI commands, are not installed by `inspire update`, and do not
intercept normal `inspire` invocations. Run them from a source checkout with
Python 3.10+ and `inspire` on `PATH`. No additional Python dependencies are needed.

## Compare compute groups

```sh
python examples/agent_ops/pick_zone.py \
  --workspace "$WORKSPACE" --account "$ACCOUNT" \
  --gpus 8 --nodes 1 --gpu-type "$GPU_MODEL" \
  --priority-level high --json
```

Specify the `low` or `high` capacity class of the effective priority according to
that workspace and project's current policy. This helper deliberately takes no
numeric `--priority`: it cannot verify workspace mappings or project caps from
these two catalog queries. Check the numeric value with the core CLI's create
dry-run. It does not infer the class from a group name or default workspace.
A multi-node request supplies
the total GPU count and instance count separately, for example `--gpus 80 --nodes 10`.

The helper queries availability once and **all** job quota rows once for the same
account/workspace, then matches each instance's GPU/CPU/host-memory requirements
and the quota's accepted priority levels. Use `--min-cpu` and `--min-memory-gib`
when these constrain placement. The latter is host memory, not GPU memory.

- Full 8-GPU nodes: high-priority capacity uses `high_priority_free_nodes`, which
  already includes `full_free_nodes`; low-priority capacity uses `full_free_nodes`
  alone. The general `free_nodes` and maximum `gpus_per_node` fields can describe
  mixed hardware, so they cannot replace the full-node counters. Multi-node Job
  requests follow the current CLI's 8-GPU-per-instance contract.
- Partial nodes: capacity uses the selected priority's GPU balance as a ranking
  signal. This is not evidence that a particular physical node can fit the job.
- `allowed_priority_levels: null` stays unknown, while `[]` is unrestricted at
  the quota-row layer. Unknown eligibility is shown without a recommendation.
- A zero guaranteed GPU allocation does not remove a GPU group from consideration.
- No project/member quota, budget, image compatibility, network topology, or
  queue-time guarantee is inferred. Validate the selected live row with a create
  dry-run and inspect scheduling events after submission.

Exit 0 means a candidate fits the snapshot and its row-level priority check;
exit 1 means no confirmed candidate or a query failure (distinguished by
`success` in JSON). Invalid arguments use exit 2; CLI errors preserve their
nonzero code where possible. This tool never submits, stops, resizes, or moves a
job. Refresh evidence rather than automatically lowering requirements or
cancelling a queued job after a fixed wait.

## Record command attempts across sessions

```sh
python examples/agent_ops/job_ops.py create --ledger ./job-events.jsonl \
  --name "$RUN_NAME" --workspace "$WORKSPACE" --project "$PROJECT" \
  --group "$GROUP" --quota "$QUOTA" --image "$IMAGE" --command "$COMMAND"

python examples/agent_ops/job_ops.py stop --ledger ./job-events.jsonl \
  "$RUN_NAME" --workspace "$WORKSPACE"

python examples/agent_ops/job_ops.py delete --ledger ./job-events.jsonl \
  "$RUN_NAME" --workspace "$WORKSPACE" --yes
```

The wrapper strips its `--ledger` option before forwarding arguments. It inherits
stdin, stdout, and stderr, so interactive confirmations remain visible and CLI
output, including JSON, remains untouched. `--help` and `--dry-run` do not create
ledger events. `job stop` takes no confirmation option; for `job delete`, use
`--yes` only when deletion has already been authorized.

The default event file is `~/.inspire/job-events.jsonl` (override it with
`--ledger`). Legacy ledger environment variables are deliberately ignored so an
old `JOBS.md` override cannot mix JSONL records into a curated Markdown file. It
is separate from any human-maintained
`JOBS.md` run plan, so automatic writes cannot replace the plan. Local file locks
serialize JSONL appends across processes; all writers must use this helper and
the same path. This is local process coordination, not a distributed scheduler.

A pending command record is persisted before mutation, then a command-result
record includes the exit code. If the pending record cannot be written, the CLI
is not launched. If recording fails after launch, the wrapper warns that the
operation may have happened: reconcile live state before retrying. A crash can
leave only a pending record; it is not evidence of failure.

**Command exit is not workload success.** A cancelled confirmation, a retained
container, or a batch with mixed item results must be reconciled against CLI
output and live lists/status. The wrapper records a batch invocation, not
per-item success. It does not parse tables, claim exactly-once submission, or
prevent duplicate names. Keep a manifest of intended items and review each batch
result before retrying unresolved items.

Only allowlisted scheduling metadata is recorded, not full command lines,
command text, environment-variable values, credentials, or output. Names and
project labels may still be private. Keep event files and run plans outside Git
and public issue reports. Document purpose, output paths, ownership and release
conditions in the local plan; live state remains authoritative.

## Tests

From `cli/`:

```sh
uv run pytest -q tests/test_agent_ops_pick_zone.py tests/test_agent_ops_job_ops.py
uv run ruff check ../examples/agent_ops tests/test_agent_ops_pick_zone.py tests/test_agent_ops_job_ops.py
```

Tests use synthetic capacity/quota rows and a fake subprocess CLI. They cover
concurrent writers, argument forwarding, visible delete prompts, query failure,
unknown priority metadata, full quota pagination, and capacity without double
counting idle nodes. They do not create platform resources.
