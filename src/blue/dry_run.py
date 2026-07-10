# Dry-run support, built on the advice facility rather than hardwired into
# steps. Attach `advice` (an around) to side-effecting steps — or `advise` a
# whole list of them; when "blue/dry-run" is set in opts (the CLI's --dry-run
# flag stamps it) the step is skipped with a note instead of run.

from __future__ import annotations

from .runtime import runtime
from .workflow import Opts, Workflow, advice_add

SKIP_ID = "blue.dry-run/skip"


def advice(step: str):
    """Build an around advice for `step`: when "blue/dry-run" is set, print
    what would have run and skip the base function; otherwise call through."""

    def f(base, opts: Opts):
        if opts.get("blue/dry-run"):
            runtime.log(f"dry-run: would run {step} ({opts.get('blue/event') or 'create'})")
            return {**opts, "blue/exit": 0}
        return base(opts)

    return f


def advise(wf: Workflow, steps: list[str]) -> Workflow:
    """Attach dry-run advice to every step in `steps` under id
    "blue.dry-run/skip". Returns the advised workflow; remove per step with
    advice_remove(wf, step, SKIP_ID)."""
    for s in steps:
        wf = advice_add(wf, s, "around", SKIP_ID, advice(s))
    return wf
