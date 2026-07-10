# Pydantic-backed validation gates for the before-while combinator (the port
# of red's Zod gates).
#
# A gate VALIDATES, it never transforms: replacing opts with a parse result
# would silently drop every other namespace's keys from the threaded dict.
# On success the gate returns the ORIGINAL opts (truthy, so the chain
# continues); on failure it raises a StepError, which the step boundary
# converts to "blue/exit"/"blue/err" through the ordinary error contract —
# the guarded step never runs and the failure message names each offending
# key.

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from .workflow import Opts, StepError


def schema_gate(schema: Any, *, exit: int = 2):
    """Build a before-while advice from a schema (a pydantic `TypeAdapter`,
    or any type pydantic can adapt — a TypedDict, a BaseModel, …): opts pass
    through untouched when they validate; otherwise the gate raises (default
    exit 2) and the guarded step never runs."""
    adapter = schema if isinstance(schema, TypeAdapter) else TypeAdapter(schema)

    def gate(o: Opts) -> Opts:
        try:
            adapter.validate_python(o)
        except ValidationError as e:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
                for err in e.errors()
            )
            raise StepError(f"schema gate failed: {detail}", exit=exit) from None
        return o

    return gate
