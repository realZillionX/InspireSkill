"""Business decisions and public data projections shared by CLI and SDK.

Put workload rules in their domain package, catalogue resolution and shared
indexes in inspire.services.catalog, and remote I/O orchestration in
inspire.services.execution. Keep Click prompts and terminal rendering in
inspire.cli, protocol adapters in inspire.platform, and SDK-bound models in
inspire.sdk. Callers import the owning module; this package is not a compatibility
facade for paths used before the services reorganisation.
"""
