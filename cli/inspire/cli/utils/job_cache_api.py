"""Local job cache for tracking submitted jobs."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class JobCache:
    """Local cache for tracking submitted jobs.

    Jobs are stored in a JSON file with the structure:
    {
        "job-id-1": {
            "name": "pr-123-debug",
            "resource": "4xH200",
            "command": "bash train.sh",
            "created_at": "2025-01-15T10:30:00",
            "status": "RUNNING",
            "updated_at": "2025-01-15T10:35:00"
        },
        ...
    }
    """

    def __init__(self, cache_path: Optional[str] = None):
        """Initialize job cache.

        Args:
            cache_path: Path to cache file. Defaults to ~/.inspire/jobs.json
        """
        if cache_path:
            self.cache_path = Path(os.path.expanduser(cache_path))
        else:
            self.cache_path = Path.home() / ".inspire" / "jobs.json"

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load cache from file."""
        if not self.cache_path.exists():
            return {}

        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        """Save cache to file."""
        try:
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(jobs, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning("Failed to write job cache at %s: %s", self.cache_path, e)

    def add_job(
        self,
        job_id: str,
        name: str,
        resource: str,
        command: str,
        status: str = "PENDING",
        log_path: Optional[str] = None,
        project: Optional[str] = None,
    ) -> None:
        """Add a newly created job to the cache.

        Args:
            job_id: Unique job identifier
            name: Job name
            resource: Resource specification used
            command: Start command
            status: Initial status (default: PENDING)
            project: Project name used for submission
        """
        jobs = self._load()
        jobs[job_id] = {
            "name": name,
            "resource": resource,
            "command": command,
            "created_at": datetime.now().isoformat(),
            "status": status,
            "updated_at": datetime.now().isoformat(),
        }
        if log_path is not None:
            jobs[job_id]["log_path"] = log_path
        if project is not None:
            jobs[job_id]["project"] = project
        self._save(jobs)

    def update_status(self, job_id: str, status: str) -> None:
        """Update job status in cache."""
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["status"] = status
            jobs[job_id]["updated_at"] = datetime.now().isoformat()
            self._save(jobs)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job info from cache."""
        jobs = self._load()
        if job_id in jobs:
            return {"job_id": job_id, **jobs[job_id]}
        return None

    def list_jobs(
        self,
        limit: int = 10,
        status: Optional[str] = None,
        exclude_statuses: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """List recent jobs from cache."""
        jobs = self._load()

        items = [{"job_id": k, **v} for k, v in jobs.items()]

        if status:
            items = [j for j in items if j.get("status") == status]

        if exclude_statuses:
            items = [j for j in items if j.get("status") not in exclude_statuses]

        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        if limit is not None and limit > 0:
            return items[:limit]
        return items

    def remove_job(self, job_id: str) -> bool:
        """Remove a job from cache."""
        jobs = self._load()
        if job_id in jobs:
            del jobs[job_id]
            self._save(jobs)
            return True
        return False

    def clear(self) -> None:
        """Clear all jobs from cache."""
        self._save({})

    def get_log_offset(self, job_id: str) -> int:
        """Get the cached byte offset for a job's log."""
        jobs = self._load()
        if job_id in jobs:
            return jobs[job_id].get("log_byte_offset", 0)
        return 0

    def set_log_offset(self, job_id: str, offset: int) -> None:
        """Update the cached byte offset for a job's log."""
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["log_byte_offset"] = offset
            jobs[job_id]["log_cached_at"] = datetime.now().isoformat()
            self._save(jobs)

    def reset_log_offset(self, job_id: str) -> None:
        """Reset the byte offset for a job's log (used with --refresh)."""
        jobs = self._load()
        if job_id in jobs:
            jobs[job_id]["log_byte_offset"] = 0
            if "log_cached_at" in jobs[job_id]:
                del jobs[job_id]["log_cached_at"]
            self._save(jobs)


__all__ = ["JobCache"]
