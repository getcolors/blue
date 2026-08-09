from __future__ import annotations
import inspect
import os
from typing import Callable
from .workflow import Opts


async def preflight(original: Opts, *, defaults: Opts | None = None,
                    overlay: Callable | None = None, validators: list[Callable] | None = None,
                    after_validate: Callable | None = None, env: dict[str, str] | None = None) -> Opts:
    environment = dict(os.environ if env is None else env)
    opts = {**(defaults or {}), **original}
    if overlay:
        opts = overlay(opts, environment)
    context = {"event": opts.get("blue/event"), "real": not opts.get("blue/dry-run")}
    errors = [error for validator in (validators or []) for error in (validator(opts, environment, context) or [])]
    if errors:
        return {**opts, "blue/exit": 2, "blue/err": "\n".join(errors)}
    if after_validate:
        result = after_validate(opts, environment, context)
        return await result if inspect.isawaitable(result) else result
    return {**opts, "blue/exit": 0}
