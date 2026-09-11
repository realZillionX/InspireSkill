"""Execute every generated entry point through the real runtime and fake sync facades."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime, timezone
import inspect
from pathlib import Path
import sys
import types
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import pytest
import inspire
from inspire.sdk import InspireAsyncClient
from inspire.sdk.models import ResourceRef
from test_sdk import client as client
from test_sdk_async import HANDLE_CASES, tracked as tracked
from test_sdk_signatures import facade_methods


# No methods are skipped. These entries have deliberately different async routing.
SPECIAL_ROUTING = {
    "ray.status": "The native rolling window calls get once per input ref, not sync status.",
    "servings.status": "The native rolling window calls get once per input ref, not sync status.",
    **{f"{facade}.exec_stream": "Async-only output iterator wraps synchronous exec/on_output."
       for facade in ("jobs", "notebooks", "hpc", "ray", "servings")},
    **{f"{facade}.{binding}": "Async-only local binding validates refs; it has no sync method."
       for facade, _, _, _, _, binding in HANDLE_CASES},
}


def sample(annotation, name, client):
    """Populate all parameters, including optional ones, without magic object sentinels."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return sample(next(arg for arg in args if arg is not type(None)), name, client)
    if origin is Literal:
        return args[-1]
    if annotation is str:
        return {"account": "alpha", "base_url": client.base_url,
                "workspace_id": "ws-test", "window": "1h", "transport": "jupyter",
                "remote": "/tmp/remote.txt", "source_path": "/tmp/source",
                "local": "/tmp/local.txt", "command": "printf fake"}.get(name, f"fake-{name}")
    if annotation is bool:
        return True
    if annotation is int:
        return 3
    if annotation is float:
        return 0.25
    if annotation is datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)
    if annotation is Path:
        return Path("/tmp/fake")
    if origin in (list, Sequence, tuple):
        values = [sample(args[0], name + "-first", client), sample(args[0], name + "-second", client)]
        return tuple(values) if origin is tuple else values
    if origin is dict:
        return {sample(args[0], "key", client): sample(args[1], "value", client)}
    if origin is Callable:
        return lambda chunk: None
    if inspect.isclass(annotation) and issubclass(annotation, ResourceRef):
        return annotation("fake", "alpha", client.base_url, "fake-key", "ws-test")
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return annotation(**{
            field.name: sample(hints[field.name], field.name, client)
            for field in fields(annotation)
            if field.init and field.default is MISSING and field.default_factory is MISSING
        })
    if annotation is Any:
        return "fake-filter"
    pytest.fail(f"No plausible sample for {name}: {annotation!r}; add explicit handling")


def public_async_methods(client):
    methods = {f"{facade}.{name}": bound for facade, name, bound in facade_methods(client)}
    methods.update({name: getattr(client, name)
                    for name, bound in inspect.getmembers(type(client), inspect.iscoroutinefunction)
                    if not name.startswith("_")})
    return methods


@pytest.fixture
def execution_guard():
    """Observe actual function entry, independently of the loop that chooses calls.

    This runs under ordinary pytest, without coverage.py or a percentage threshold.
    Removing a driver case still fails teardown and names the unexecuted wrapper.
    """
    c = InspireAsyncClient("alpha")
    expected = {bound.__func__.__code__: name for name, bound in public_async_methods(c).items()}
    seen = set()
    previous = sys.getprofile()

    def entered(frame, event, arg):
        if event == "call" and frame.f_code in expected:
            seen.add(frame.f_code)
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(entered)
    try:
        yield
    finally:
        sys.setprofile(previous)
        missing = sorted(name for code, name in expected.items() if code not in seen)
        assert not missing, "Public async methods never executed: " + ", ".join(missing)


def test_every_public_async_method_executes_its_mirror(client, tracked, monkeypatch, execution_guard):
    calls = []
    marker = object()
    output = "fake-output"
    handles = {(facade, producer): (handle, ref, binding)
               for facade, producer, handle, ref, _, binding in HANDLE_CASES}
    originals = {(facade, name): method for facade, name, method in facade_methods(client)}
    originals.update({("", name): getattr(client, name) for name in ("login", "init")})

    def fake_method(facade, name, original):
        signature = inspect.signature(original)
        iterator = get_origin(get_type_hints(original)["return"]) is Iterator

        def fake(self, *args, **kwargs):
            arguments = dict(signature.bind(*args, **kwargs).arguments)
            calls.append((facade, name, arguments))
            if name == "exec":
                arguments["on_output"](output)
            if (facade, name) in handles:
                handle, ref_name, _ = handles[facade, name]
                ref = sample(getattr(inspire, ref_name), "ref", client)
                extra = ({"notebook": sample(inspire.NotebookRef, "notebook", client)}
                         if name == "save_image" else {"operation_id": "fake-operation_id"})
                return getattr(inspire, handle)(name="fake", ref=ref, **extra)
            return iter([marker, marker]) if iterator else marker
        return fake

    for (facade, name), original in originals.items():
        cls = type(getattr(client, facade)) if facade else type(client)
        monkeypatch.setattr(cls, name, fake_method(facade, name, original))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            methods = public_async_methods(c)
            assert set(SPECIAL_ROUTING) <= set(methods), "Stale explicit routing exception"
            for label, bound in methods.items():
                if label == "close":
                    continue  # Exercised after every other method below; never skip teardown.
                facade, _, name = label.rpartition(".")
                signature = inspect.signature(bound)
                hints = get_type_hints(bound)
                kwargs = {}
                chunks = []
                for key, parameter in signature.parameters.items():
                    if parameter.kind == parameter.VAR_KEYWORD:
                        kwargs["reason"] = "fake-filter-reason"
                    elif key == "on_output":
                        kwargs[key] = chunks.append
                    elif hints[key] is bool:
                        kwargs[key] = not parameter.default
                    else:
                        kwargs[key] = sample(hints[key], key, client)
                # Exercise the supported positional selector as well as keyword-only options.
                positional = next((p.name for p in signature.parameters.values()
                                   if p.kind == p.POSITIONAL_OR_KEYWORD), None)
                args = (kwargs.pop(positional),) if positional else ()
                expected = dict(signature.bind(*args, **kwargs).arguments)
                calls.clear()
                if inspect.isasyncgenfunction(bound):
                    result = [item async for item in bound(*args, **kwargs)]
                else:
                    result = await bound(*args, **kwargs)
                if name.startswith("bind_"):
                    assert label in SPECIAL_ROUTING
                    assert not calls, label
                    assert result.ref is expected["ref"], label
                    assert result._facade is getattr(c, facade), label
                    if name == "bind_image_ref":
                        assert result.notebook is expected["notebook"], label
                elif label in {"ray.status", "servings.status"}:
                    assert calls == [(facade, "get", {"ref": ref, "workspace": expected["workspace"]})
                                     for ref in expected["refs"]], label
                    assert result == (marker,) * len(expected["refs"]), label
                else:
                    target = "exec" if name == "exec_stream" else name
                    assert len(calls) == 1, (label, calls)
                    actual_facade, actual_name, actual = calls[0]
                    assert (actual_facade, actual_name) == (facade, target), label
                    if "on_output" in expected:
                        assert callable(actual.pop("on_output")), label
                        expected.pop("on_output")
                        assert chunks == [output], label
                    assert actual == expected, label
                    if (facade, name) in handles:
                        assert type(result) is getattr(inspire, "Async" + handles[facade, name][0]), label
                    elif name == "exec_stream":
                        assert result == [output], label
                    elif inspect.isasyncgenfunction(bound):
                        assert result == [marker, marker], label
                    else:
                        assert result is marker, label
        await c.close()

    asyncio.run(run())
