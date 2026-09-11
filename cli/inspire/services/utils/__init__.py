"""Small domain-independent helpers for service inputs and public views.

Text, identifiers, collection budgets, serialization and process checks belong
here when no workload policy is involved. A helper that decides which resource
is valid belongs in that resource's service package, not here because two callers
happen to need it. Click and terminal rendering remain in inspire.cli.
"""
