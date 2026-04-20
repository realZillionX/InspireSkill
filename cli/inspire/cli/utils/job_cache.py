"""Local job cache for tracking submitted jobs (façade).

The actual cache implementation lives in `job_cache_api.py`; this module keeps the original
import path stable for callers.
"""

from __future__ import annotations

from inspire.cli.utils.job_cache_api import JobCache

__all__ = ["JobCache"]
