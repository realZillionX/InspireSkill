"""Account storage — one account = one isolated directory on disk.

See :mod:`inspire.accounts.storage` for the rationale and layout. This
package re-exports the storage helpers so callers can do
``from inspire.accounts import current_account, account_config_path``.
"""

from inspire.accounts.normalize import (
    NORMALIZATION_SENTINEL,
    NormalizationReport,
    normalize_environment,
)
from inspire.accounts.storage import (
    CONFIG_FILENAME,
    AccountError,
    account_config_path,
    account_dir,
    account_exists,
    accounts_dir,
    clear_current_account,
    create_account,
    current_account,
    current_file,
    ensure_inspire_home,
    inspire_home,
    list_accounts,
    remove_account,
    rename_account,
    set_current_account,
    validate_name,
)

__all__ = [
    "CONFIG_FILENAME",
    "AccountError",
    "NORMALIZATION_SENTINEL",
    "NormalizationReport",
    "account_config_path",
    "account_dir",
    "account_exists",
    "accounts_dir",
    "clear_current_account",
    "create_account",
    "current_account",
    "current_file",
    "ensure_inspire_home",
    "inspire_home",
    "list_accounts",
    "normalize_environment",
    "remove_account",
    "rename_account",
    "set_current_account",
    "validate_name",
]
