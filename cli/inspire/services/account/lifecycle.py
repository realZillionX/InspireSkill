"""Shared local account deletion for CLI and SDK."""

from inspire.accounts import AccountError, account_dir, remove_account, validate_name
from inspire.platform.web.session import clear_session_cache
from inspire.platform.web.session.refresh_lock import exclusive_session_refresh


def delete_local_account(name: str) -> None:
    """Clear tokens under the refresh lock, then delete the complete profile."""
    name = validate_name(name)
    if not account_dir(name).exists():
        raise AccountError(f"Account not found: {name}")
    with exclusive_session_refresh(name):
        clear_session_cache(name)
    # Close the lock handle before removing its directory (required on Windows).
    remove_account(name)
