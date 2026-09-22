"""Caller-owned runtime cleanup; capture register and handles outside opts."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Literal

CleanupPhase = Literal["process", "resource"]
RegisterFinalizer = Callable[[CleanupPhase, Callable[[], Any]], None]


async def with_scope(body: Callable[[RegisterFinalizer], Any]) -> Any:
    """Run process finalizers before resource finalizers, each in LIFO order.

    Cleanup must stop and await owned work. All callbacks are attempted, even
    under cancellation. Body errors take precedence over cleanup errors.
    Uncatchable termination cannot execute finalizers.
    """
    callbacks: dict[str, list[Callable[[], Any]]] = {"process": [], "resource": []}
    open_scope = True

    def register(phase: CleanupPhase, cleanup: Callable[[], Any]) -> None:
        if not open_scope:
            raise RuntimeError("Scope is closed")
        if phase not in callbacks or not callable(cleanup):
            raise ValueError("Invalid scope finalizer")
        callbacks[phase].append(cleanup)

    failure: BaseException | None = None
    result = None
    try:
        result = body(register)
        if inspect.isawaitable(result):
            result = await result
    except BaseException as error:
        failure = error
    open_scope = False

    async def finish() -> list[BaseException]:
        errors = []
        for phase in ("process", "resource"):
            for cleanup in reversed(callbacks[phase]):
                try:
                    value = cleanup()
                    if inspect.isawaitable(value):
                        await value
                except BaseException as error:
                    errors.append(error)
        return errors

    task = asyncio.create_task(finish())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if failure is None:
                failure = error
    errors = task.result()
    if failure is not None:
        raise failure
    if errors:
        raise BaseExceptionGroup("Scope cleanup failed", errors)
    return result
