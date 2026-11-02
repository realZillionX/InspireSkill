# Contributing

Thank you for contributing to InspireSkill. Because the CLI, Python SDK, and reference manuals are designed for both humans and AI agents, changes should keep command behavior reproducible, output contracts clean, and documentation aligned with CLI `--help`.

## Project Scope

InspireSkill keeps the `inspire` CLI, Python SDK, and agent skill references synchronized with live platform behavior. CLI `--help` is the source of truth for command syntax and flags; reference files focus on platform semantics, workflows, and operational guardrails.

## Development Setup

The Python package lives in `cli/` and uses `uv`:

```bash
cd cli
uv sync --dev
uv run inspire --help
```

Install pre-commit hooks:

```bash
uv run pre-commit install --config ../.pre-commit-config.yaml
uv run pre-commit run --config ../.pre-commit-config.yaml --all-files
```

When reinstalling the `inspire` binary from local source for live smoke testing, always pass `--no-cache` so `uv` does not reuse a cached wheel built from an older commit when the version string is unchanged:

```bash
uv tool install --force --no-cache .
```

## Sources of Truth & Output Contracts

1. **CLI `--help` owns command syntax:** When adding, removing, or changing commands or options, update `inspire --help`, `inspire <group> --help`, and `inspire <group> <subcommand> --help`. Do not duplicate full flag tables in reference docs.
2. **Live platform queries own resource state:** `list`, `status`, `events`, `metrics`, quotas, availability, projects, and accounts read live platform state. Local caches only accelerate web sessions, name-to-handle resolution, and SSH connection reuse.
3. **Name-only public surface:** Users and agents address resources by name and local account alias. Opaque platform IDs (`ws-...`, `project-...`, `lcg-...`, `job-...`, etc.) stay inside resolvers and `browser_api` payloads — never leak raw IDs into CLI tables, `--json` output, error hints, or debug reports (except explicitly documented credential/endpoint outputs such as `notebook proxy-url` and `serving api`).
4. **Bounded output budgets:** Discovery lists and batch outputs default to 20 items (`--limit/-n` and `--all` adjust the bound). Job logs default to 100 entries and 16,000 characters with `shown`, `total`, and `truncated` metadata.
5. **Explicit scheduling fields:** Never introduce implicit defaults or repo-local `./.inspire/` configs for `workspace`, `project`, `group`, `quota`, or `image`. Remote paths use explicit `/inspire/...` absolute paths.

## Platform Workflow Conventions

- **Separate public internet from internal mirrors:** Outbound downloads (GitHub, Hugging Face, external datasets) belong in internet-enabled CPU Notebooks in `CPU资源空间`. Package managers (`pip`, Apt, Conda, `npm`, Maven) and internal Docker registries can use the internal mirrors documented in [`references/internal-sources.md`](references/internal-sources.md), including inside offline GPU workspaces.
- **Clean before saving images:** Before committing a Notebook with `notebook save-image`, clean temporary files, caches, and credentials as documented in [`references/notebook.md`](references/notebook.md#6-base-environments-and-saving-images).
- **Keep `INSPIRE.md` out of this repository:** `INSPIRE.md` is an optional asset inventory for downstream research repositories that maintain persistent remote paths, images, or models (see [`references/assets.md`](references/assets.md)). InspireSkill itself is a tool repository and must not contain a root `INSPIRE.md` or account-specific paths/IDs.

## Code & Documentation Maintenance

- Follow existing patterns across `cli/inspire/platform`, `cli/inspire/services`, `cli/inspire/sdk`, and `cli/inspire/cli`.
- Keep changes focused and complete: when modifying a command or API wrapper, update the resolver, formatter, `--help`, unit tests, and relevant reference docs in the same change.
- If you change synchronous SDK facade methods in `cli/inspire/sdk/`, regenerate the async wrappers from `cli/`:
  ```bash
  uv run python scripts/generate_sdk_async.py
  ```
- Only document verified Browser API behavior in [`references/dev/browser-api.md`](references/dev/browser-api.md). Destructive or write operations should be backed by unit tests, frontend bundle evidence, or controlled live smoke tests (always clean up any live test resources).

## Pre-Submission Checks

Run the full check suite from `cli/` before opening a PR:

```bash
cd cli
uv run pytest -q
uv run ruff check inspire tests
uv run mypy
uv build
```

CI runs the full unit suite on Linux/Python 3.10 and Windows/Python 3.12. macOS runs the filesystem, terminal, subprocess, and installer tests. Ruff, mypy, and the platform-independent package build run once on Linux. The Windows job also exercises redirected CLI output and parses the PowerShell installer.

Code, tests, installers, and CI configuration trigger this workflow. For documentation-only changes, run `git diff --check` and verify changed command examples against CLI `--help`.

Keep regression tests focused on observable behavior: requests, responses, output, errors, file permissions, and process lifecycle. Help smoke coverage walks the command tree; avoid repeating exact prose, option ordering, method counts, or assertions that a deleted implementation stays absent.

## Pull Requests & Releases

- Include the purpose and scope of the change, verification commands run, and (if live platform resources were touched) confirmation that all test resources were cleaned up.
- Keep commit messages, PR descriptions, `CHANGELOG.md`, and GitHub Release notes concise and focused on user-visible changes, fixes, and breaking-change migrations. Never include personal account aliases, credentials, internal URLs not already part of the public tool configuration, or real project resource names/paths.
