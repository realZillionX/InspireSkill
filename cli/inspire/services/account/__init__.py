"""Resolve account configuration and session/workspace context for callers.

Configuration rendering, account checks and context views belong here so CLI and
SDK agree without invoking prompts. Storage layout belongs to inspire.accounts,
configuration loading to inspire.config, and credentials/session renewal to
inspire.platform.web.session. This package does not own another login flow.
"""
