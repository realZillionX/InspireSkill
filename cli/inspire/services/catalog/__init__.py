"""Discover and resolve resources used across workload domains.

Catalogues, image/model writes, quotas, scheduling policy and live capacity views
belong here, along with the shared identity index and refresh leases. Catalogue
snapshots are reusable; workload status is not catalogue data. Workload submission
and lifecycle rules belong to their workload package, while account configuration
belongs to inspire.services.account.
"""
