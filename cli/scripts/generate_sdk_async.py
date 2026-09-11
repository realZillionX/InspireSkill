"""Generate explicit signatures: uv run python scripts/generate_sdk_async.py."""
from __future__ import annotations

import ast
import builtins
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_origin
from unittest.mock import patch

from inspire.sdk.client import InspireClient


# Deliberate async-only return types and local reference binding methods.
AWAITABLE_HANDLES = {
    ("jobs", "create"): ("JobHandle", "JobRef", "bind_ref"),
    ("notebooks", "create"): ("NotebookHandle", "NotebookRef", "bind_ref"),
    ("hpc", "create"): ("HPCJobHandle", "HPCJobRef", "bind_ref"),
    ("ray", "create"): ("RayJobHandle", "RayJobRef", "bind_ref"),
    ("servings", "create"): ("ServingHandle", "ServingRef", "bind_ref"),
    ("tensorboards", "create"): ("TensorboardHandle", "TensorboardRef", "bind_ref"),
    ("notebooks", "save_image"): ("ImageSaveHandle", "ImageRef", "bind_image_ref"),
    ("images", "register"): ("ImageRegisterHandle", "ImageRef", "bind_ref"),
}


def generate() -> str:
    # Local bindings only: no account files or platform access.
    with patch("inspire.accounts.account_exists", return_value=True), patch(
        "inspire.config.Config.from_files_and_env",
        return_value=(SimpleNamespace(base_url="https://example.invalid", username="fake"), None),
    ):
        client = InspireClient("generator")
    facades = {name: type(value) for name, value in vars(client).items()
               if not name.startswith("_") and not isinstance(value, (str, int, float, bool))}
    client.close()
    modules: dict[str, str] = {}
    binding_modules: dict[str, str] = {}

    def module(name: str) -> str:
        if name not in modules:
            modules[name] = f"_m{len(modules)}"
        return modules[name]

    def wrapper(cls, facade: str, name: str, *, output: bool = False) -> str:
        fn = inspect.unwrap(getattr(cls, name))
        signature = inspect.signature(fn)
        substitutions = {}
        for base in getattr(cls, "__orig_bases__", ()):
            origin = get_origin(base)
            for variable, concrete in zip(getattr(origin, "__parameters__", ()), get_args(base)):
                substitutions[variable.__name__] = (
                    f"{module(concrete.__module__)}.{concrete.__name__}"
                )

        class Qualify(ast.NodeTransformer):
            def visit_Name(self, node):
                if node.id == "Literal":
                    return ast.Name(id="Literal", ctx=ast.Load())
                if node.id == "Iterator":
                    return ast.Name(id="AsyncIterator", ctx=ast.Load())
                if node.id in substitutions:
                    return ast.parse(substitutions[node.id], mode="eval").body
                if node.id in vars(builtins):
                    return node
                return ast.Attribute(value=ast.Name(id=module(fn.__module__), ctx=ast.Load()),
                                     attr=node.id, ctx=ast.Load())

        def annotation(value) -> str:
            assert isinstance(value, str), (name, value)
            return ast.unparse(Qualify().visit(ast.parse(value, mode="eval").body))

        params = []
        calls = []
        keyword = False
        for param in signature.parameters.values():
            if param.name == "self":
                params.append("self")
                continue
            if param.kind == param.KEYWORD_ONLY and not keyword:
                params.append("*")
                keyword = True
            prefix = "**" if param.kind == param.VAR_KEYWORD else ""
            item = prefix + param.name + ": " + (
                "Callable[[str], None | Awaitable[None]] | None" if param.name == "on_output"
                else annotation(param.annotation)
            )
            if param.default is not param.empty:
                item += " = " + repr(param.default)
            params.append(item)
            calls.append(prefix + param.name if prefix else f"{param.name}={param.name}")
        returns = "AsyncIterator[str]" if output else annotation(signature.return_annotation)
        handle = AWAITABLE_HANDLES.get((facade, name))
        if handle:
            returns = f"_handles.Async{handle[0]}"
        is_iterator = returns.startswith("AsyncIterator[")
        public_name = "exec_stream" if output else name
        lines = [f"    async def {public_name}(\n" +
                 "".join(f"        {param},\n" for param in params) +
                 f"    ) -> {returns}:\n"]
        doc = inspect.getdoc(fn)
        if doc and not handle and not output:
            lines.append(f"        {doc!r}\n")
        invocation = f"self._client.{'_stream' if is_iterator else '_call'}({facade!r}, {name!r}"
        if output:
            invocation += ", output=True"
        invocation += "".join(f",\n            {arg}" for arg in calls) + ")"
        if is_iterator:
            lines.append(f"        async with aclosing({invocation}) as stream:\n")
            lines.append("            async for item in stream:\n                yield item\n")
        elif handle:
            lines.append(f'        """Submit and return {returns}; awaiting polls, cancellation never stops remote work."""\n')
            lines.append(f"        result = await {invocation}\n")
            lines.append(f"        return {returns}(**vars(result), _facade=self)\n")
        else:
            lines.append(f"        return await {invocation}\n")
        return "".join(lines)

    body = []
    for facade, cls in facades.items():
        body.append(f"class Async{cls.__name__}(AsyncFacade):\n")
        for name, _ in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            body.append(wrapper(cls, facade, name))
            if name == "exec":
                body.append(wrapper(cls, facade, name, output=True))
        for (owner, producer), (handle, ref, binding) in AWAITABLE_HANDLES.items():
            if owner != facade:
                continue
            model_module = inspect.unwrap(getattr(cls, producer)).__globals__[handle].__module__
            alias = binding_modules.setdefault(model_module, f"_refs{len(binding_modules)}")
            ref_type = f"{alias}.{ref}"
            # ImageSaveHandle imports ImageRef from models; it is public there too.
            notebook = f"{alias}.NotebookRef"
            extra = f", *, notebook: {notebook}" if binding == "bind_image_ref" else ""
            body.append(f"    async def {binding}(self, ref: {ref_type}{extra}) -> _handles.Async{handle}:\n")
            body.append('        """Bind a stored ref locally; no platform request or submission.\n\n'
                        '        Awaiting polls; cancelling the wait never stops remote work.\n'
                        '        Submission metadata is unknown on restored handles.\n'
                        '        """\n')
            body.append(f"        await self._client._validate_binding(ref, {ref_type})\n")
            if binding == "bind_image_ref":
                body.append(f"        await self._client._validate_binding(notebook, {notebook})\n")
                body.append("        if ref.workspace_id != notebook.workspace_id:\n"
                            "            raise ValidationError('Image and notebook workspaces must match.')\n")
                extras = "notebook=notebook"
            else:
                extras = "operation_id=''"
            body.append(f"        return _handles.Async{handle}(name=ref.name, ref=ref, {extras}, _facade=self)\n")
        body.append("\n")
    body.append("class InspireAsyncClient(AsyncRuntime):\n")
    body.append('    """Share one client across tasks on the same process and event loop.\n\n'
                '    Account/configuration are pinned on first use, not construction.\n'
                '    concurrency is a deprecated positive-integer no-op; use an application\n'
                '    semaphore to limit requests. Local I/O uses a separate four-worker pool.\n'
                '    Close with async with or await close(); cancellation does not stop\n'
                '    remote workloads or forcibly interrupt running local offloads.\n'
                '    """\n')
    body.append("    accounts = Accounts\n\n")
    body.append("    def __init__(\n        self,\n        account: str | None = None,\n        *,\n")
    sig = inspect.signature(InspireClient)
    options = ["account"]
    for param in list(sig.parameters.values())[1:]:
        body.append(f"        {param.name}: {param.annotation} = {param.default!r},\n")
        options.append(param.name)
    body.append("        concurrency: int | None = None,\n    ) -> None:\n")
    body.append("        super().__init__({\n" + "".join(
        f"            {name!r}: {name},\n" for name in options) + "        }, concurrency)\n")
    for facade, cls in facades.items():
        body.append(f"        self.{facade} = Async{cls.__name__}(self)\n")
    body.append("""
    @classmethod
    def from_credentials(
        cls, username: str, password: str, *, base_url: str | None = None,
        proxy: str | None = None, account: str | None = None, **client_kwargs: Any,
    ) -> InspireAsyncClient:
        return cls(account, username=username, password=password, base_url=base_url,
                   proxy=proxy, **client_kwargs)

    @property
    def account(self) -> str:
        if self._account is None:
            raise ConfigurationError("account is available after the first operation or entering async with client.")
        return self._account

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise ConfigurationError("base_url is available after the first operation or entering async with client.")
        return self._base_url

    async def login(self, *, force: bool = False) -> AccountInfo:
        return await self._call("", "login", force=force)

    async def init(self, *, force: bool = False) -> InitResult:
        return await self._call("", "init", force=force)

    async def __aenter__(self) -> InspireAsyncClient:
        await self._start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
""")
    return (
        '"""Generated by scripts/generate_sdk_async.py; do not edit wrappers by hand."""\n'
        'from __future__ import annotations\n\n'
        'from contextlib import aclosing\n'
        'from typing import Any, AsyncIterator\n'
        'from .accounts import Accounts, InitResult\n'
        'from .models_resources import AccountInfo\n'
        'from collections.abc import Awaitable, Callable\n'
        'from ._async_runtime import AsyncFacade, AsyncRuntime\n'
        'from . import async_handles as _handles\n'
        'from .exceptions import ValidationError, ConfigurationError\n'
        + "".join(f"import {name} as {alias}\n" for name, alias in modules.items()
                  if alias + "." in "".join(body))
        + "".join(f"import {name} as {alias}\n" for name, alias in binding_modules.items())
        + "\n\n" + "\n".join(body)
    )


if __name__ == "__main__":
    Path("inspire/sdk/async_client.py").write_text(generate())
