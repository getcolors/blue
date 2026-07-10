# Progress reporting, built on the advice facility. Attach with `advise` to
# print step start/end with elapsed time; reads "blue/step" from opts.

from __future__ import annotations

import time

from .runtime import runtime
from .workflow import Opts, Workflow, advice_add_all

PROGRESS_ID = "blue.progress/progress"


async def progress(f, opts: Opts) -> Opts:
    """An around advice that prints step name on entry and elapsed time on
    exit. Reads "blue/step" from opts (stamped by the engine)."""
    step = opts.get("blue/step")
    event = opts.get("blue/event") or "create"
    runtime.log(f">>> {step} ({event})")
    t0 = time.monotonic()
    result = await f(opts)
    runtime.log(f"<<< {step} ({round((time.monotonic() - t0) * 1000)}ms)")
    return result


def advise(wf: Workflow) -> Workflow:
    """Attach progress advice to every step. Returns the advised workflow."""
    return advice_add_all(wf, "around", PROGRESS_ID, progress)
