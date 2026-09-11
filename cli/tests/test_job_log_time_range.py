"""Shared training log windows obey the platform's one-month limit."""

import pytest

from inspire.services.job import job_logs
from inspire.platform.web.browser_api.jobs import JOB_LOG_MAX_WINDOW_MS


@pytest.mark.parametrize("finished", [True, False])
def test_default_long_job_window_is_clamped(monkeypatch, finished):
    now_ms = 100 * 86400 * 1000
    monkeypatch.setattr(job_logs.time, "time", lambda: now_ms / 1000)
    end_ms = now_ms - 86400 * 1000 if finished else now_ms
    data = {"created_at": str(end_ms - 47 * 86400 * 1000)}
    if finished:
        data["finished_at"] = str(end_ms)
        end_ms += 10 * 60 * 1000
    start, end = job_logs.web_log_time_range(data, None)
    assert end == end_ms
    assert end - start == JOB_LOG_MAX_WINDOW_MS


@pytest.mark.parametrize("minutes", [30, 30 * 24 * 60, 47 * 24 * 60])
def test_explicit_window_is_capped(monkeypatch, minutes):
    monkeypatch.setattr(job_logs.time, "time", lambda: 100 * 86400)
    start, end = job_logs.web_log_time_range({}, minutes)
    assert end == 100 * 86400 * 1000
    assert end - start == min(minutes * 60 * 1000, JOB_LOG_MAX_WINDOW_MS)


def test_short_finished_job_keeps_padding():
    start, end = job_logs.web_log_time_range({"created_at": "1000000", "finished_at": "2000000"}, None)
    assert (start, end) == (400000, 2600000)
