"""Synchronous Python entry point; construction never authenticates or connects."""

from __future__ import annotations

from inspire.platform.web.flow import blocking_call, perform_sync

import math
from copy import deepcopy
from typing import Any

from .accounts import Accounts, InitResult, ensure_credentials
from .models_resources import AccountInfo

from .exceptions import ConfigurationError, ValidationError
from inspire.platform.web.transport import Transport
from .cache import CatalogCache
from .resources import Workspaces, Projects, ComputeGroups, Images


class InspireClient:
    accounts = Accounts

    def __init__(
        self,
        account: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        base_url: str | None = None,
        proxy: str | None = None,
        allow_browser: bool = False,
        timeout: float = 30,
        operation_timeout: float = 120,
        catalog_ttl: float = 60,
        catalog_disk_cache: bool = False,
    ):
        from inspire.accounts import current_account, account_exists, validate_name
        from inspire.config import Config

        if type(catalog_disk_cache) is not bool:
            raise ValidationError("catalog_disk_cache must be a boolean.")
        self.cache = CatalogCache(catalog_ttl)
        self._catalog_context: dict[str, bool] | None = None
        for value in (timeout, operation_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (math.isfinite(value) and value > 0)
            ):
                raise ValidationError("Timeouts must be finite positive seconds.")
        if username is not None or password is not None:
            if username is None or password is None:
                raise ValidationError("Username and password must be supplied together.")
            account = ensure_credentials(
                account, username=username, password=password, base_url=base_url, proxy=proxy
            )
        try:
            selected = validate_name(account if account is not None else current_account() or "")
            if not account_exists(selected):
                raise ValueError()
            config, _ = Config.from_files_and_env(require_credentials=False, account=selected)
        except Exception:
            raise ConfigurationError("Select an initialized Inspire account.") from None
        self._account = selected
        self._base_url = config.base_url.rstrip("/")
        from .catalog_store import CatalogStore

        catalog_store = CatalogStore(self.account, self.base_url)
        if catalog_disk_cache:
            self.cache = CatalogCache(catalog_ttl, store=catalog_store)
            from .identity_cache import IdentityCache

            self.cache._identity = IdentityCache(self.account, catalog_ttl, self.base_url)
        # Even readers that opt out must invalidate an existing shared catalog
        # when they mutate images; do not create a disk cache for these clients.
        self.cache._invalidation_store = catalog_store
        from .identity_cache import IdentityCache

        self.cache._identity_invalidation = IdentityCache(self.account, catalog_ttl, self.base_url)
        self.operation_timeout = operation_timeout
        self._config = config
        self._transport = Transport(
            selected,
            self.base_url,
            username=config.username,
            allow_browser=allow_browser,
            timeout=timeout,
        )
        self.workspaces, self.projects = Workspaces(self), Projects(self)
        self.compute_groups, self.images = ComputeGroups(self), Images(self)
        from .jobs import Jobs

        self.jobs = Jobs(self)
        from .hpc import HPC

        self.hpc = HPC(self)
        from .ray import Ray

        self.ray = Ray(self)
        from .servings import Servings

        self.servings = Servings(self)
        from .tensorboards import Tensorboards

        self.tensorboards = Tensorboards(self)
        from .notebooks import Notebooks

        self.notebooks = Notebooks(self)
        from .account import AccountInformation

        self.account_info = AccountInformation(self)
        from .account import APIKeys

        self.api_keys = APIKeys(self)
        from .datasets import Datasets

        self.datasets = Datasets(self)
        from .model_registry import Models

        self.models = Models(self)
        from .resource_monitor import Resources

        self.resources = Resources(self)

    @classmethod
    def from_credentials(
        cls,
        username: str,
        password: str,
        *,
        base_url: str | None = None,
        proxy: str | None = None,
        account: str | None = None,
        **client_kwargs: Any,
    ) -> InspireClient:
        """Create a fixed-account client without changing the saved default."""
        return cls(
            account,
            username=username,
            password=password,
            base_url=base_url,
            proxy=proxy,
            **client_kwargs,
        )

    def login(self, *, force: bool = False) -> AccountInfo:
        """Validate a session now; Chromium requires explicit allow_browser=True."""
        from inspire.platform.web import browser_api
        from inspire.platform.web.session.models import WebSession
        from .exceptions import AuthenticationError

        transport = self._transport
        with transport.scope(timeout=self.operation_timeout):
            if transport._session is None:
                cached = WebSession.load(allow_expired=True, account=self.account)
                try:
                    transport._adopt_session(cached)
                except AuthenticationError:
                    pass
            if force or transport._session is None or not transport._session.is_valid():
                transport._refresh()
            user = browser_api.get_current_user(session=transport.session, refresh=True)
            return AccountInfo(
                self.account,
                self._config.username,
                self.base_url,
                str(user.get("id") or user.get("user_id") or ""),
                str(user.get("name") or user.get("user_name") or ""),
            )

    def init(self, *, force: bool = False) -> InitResult:
        """Login and persist account config. Force rebuilds from the template.

        Interactive prompts, Playwright installation and ssh-keygen are CLI-only.
        Ordinary init preserves all other keys, including legacy and unknown sections.
        Force drops those sections, like inspire init --force.
        """
        from inspire.platform.web.session import DEFAULT_WORKSPACE_ID
        from inspire.local_files import repair_inspire_path
        from inspire.services.account.account_config import (
            ACCOUNT_CONFIG_TEMPLATE,
            sanitize_account_config,
            toml_dumps,
            atomic_write_text,
        )

        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            import tomli as tomllib

        with self._transport.scope(timeout=self.operation_timeout):
            self.login(force=force)
            session = self._transport.session
            workspace_id = str(session.workspace_id or "").strip()
            if not workspace_id or workspace_id == DEFAULT_WORKSPACE_ID:
                raise ValidationError(
                    "Could not detect an accessible workspace from the authenticated session. "
                    "Re-run `inspire init` with an account that can see at least one workspace."
                )
            path = Accounts.config_path(self.account)
            def read_existing() -> str | None:
                repair_inspire_path(path)
                return path.read_text(encoding="utf-8") if path.exists() else None

            before = perform_sync(blocking_call(read_existing))
            existing = tomllib.loads(before) if before is not None else {}
            data = (
                sanitize_account_config(tomllib.loads(ACCOUNT_CONFIG_TEMPLATE))
                if force
                else deepcopy(existing)
            )
            auth = data.setdefault("auth", {})
            if not auth.get("username") or auth.get("username") == "your_username":
                auth["username"] = session.login_username or self._config.username
            if not auth.get("password") and self._config.password:
                auth["password"] = self._config.password
            api = data.setdefault("api", {})
            if force or not api.get("base_url") or api.get("base_url") == "https://api.example.com":
                api["base_url"] = self.base_url
            changed = data != existing
            if changed:
                perform_sync(blocking_call(atomic_write_text, path, toml_dumps(data), private=True))
            return InitResult(path, changed)

    @property
    def account(self) -> str:
        return self._account

    @property
    def base_url(self) -> str:
        return self._base_url

    def _validate_ref(self, ref, cls, workspace_id=None):
        if type(ref) is not cls or ref.account != self.account or ref.base_url != self.base_url:
            raise ValidationError("Reference belongs to another resource type, account or origin.")
        if (
            not isinstance(ref.key, str)
            or not ref.key
            or (workspace_id is not None and ref.workspace_id and ref.workspace_id != workspace_id)
        ):
            raise ValidationError("Reference does not match the requested workspace.")

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> InspireClient:
        self._transport.check()
        return self

    def __exit__(self, *args):
        self.close()
