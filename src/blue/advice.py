# Emacs-style advice for step functions: the full set of nadvice
# combinators, LIFO stacking (most recently added outermost) with an
# optional depth override, removal by explicit id.
# Registries are plain dicts of step-name -> list of Entry
# {id, how, fn, seq, depth}, so all operations are pure — advice is
# workflow-scoped, not global. A workflow embedded via blue.workflow's
# `step` inherits its ancestors' advice at run time by merging registries
# (see `merge_entries`); the values stay pure.
# The `seq` field is a caller-assigned monotonic add-order number; it lets
# a step-scoped registry and a separate all-steps registry be merged and
# sorted into one strict add-order stack (see blue.workflow's _run_step).
# `depth` (-100..100, default 0) overrides add order the way Emacs hook
# depths do: lower pushes the advice outward, higher pushes it inward;
# at equal depth the most recently added is outermost.
#
# The supported combinators, matching Emacs nadvice. With FUNCTION the
# advice, OLDFUN the prior chain, and r the opts map:
#
#   around        FUNCTION(OLDFUN, r)
#   before        FUNCTION(r) then OLDFUN(r)
#   after         OLDFUN(r) then FUNCTION(r), returns OLDFUN's value
#   override      FUNCTION(r), OLDFUN never runs
#   before-while  FUNCTION(r) && OLDFUN(r)          (Clojure truthiness)
#   before-until  FUNCTION(r) || OLDFUN(r)          (Clojure truthiness)
#   after-while   ret = OLDFUN(r); blue_true(ret) ? FUNCTION(r) : ret
#   after-until   ret = OLDFUN(r); blue_true(ret) ? ret : FUNCTION(r)
#   filter-args   OLDFUN(FUNCTION(r))
#   filter-return FUNCTION(OLDFUN(r))
#
# Typical use cases: around = dry-run/retry/timing/locks; override = stubs;
# before = setup/prerequisites; after = audit/metrics/cleanup;
# before-while = precondition gates; before-until = fast paths/no-ops;
# after-while = success-only follow-ups; after-until = recovery/fallback;
# filter-args = normalize/scope inputs; filter-return = normalize/enrich
# outputs.
#
# For after-while/after-until, a blue step result is true only when its
# "blue/exit" is 0 (missing means 0); a positive "blue/exit" is false.
# Non-dict returns keep Clojure truthiness (only None/False are falsy) for
# compose-level use.

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any, Callable

AnyFn = Callable[..., Any]

HOWS = frozenset(
    {
        "around",
        "before",
        "after",
        "override",
        "before-while",
        "before-until",
        "after-while",
        "after-until",
        "filter-args",
        "filter-return",
    }
)


class AdviceError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    id: str
    how: str
    fn: AnyFn
    seq: int
    depth: int
    # provenance tags, set only by blue.workflow.advice_plan
    scope: str | None = None
    level: int | None = None


# Registry: dict of step name -> list[Entry]. Treated as an immutable value.
Registry = dict


def _entry(how: str, id: str, fn: AnyFn, order: int, props: dict | None = None) -> Entry:
    if how not in HOWS:
        raise AdviceError(f"unsupported advice combinator: {how}")
    depth = (props or {}).get("depth", 0)
    if not (-100 <= depth <= 100):
        raise AdviceError("advice depth must be in -100..100")
    return Entry(id=id, how=how, fn=fn, seq=order, depth=depth)


def _remove_entry_ids(entries: list[Entry] | None, ids: set[str]) -> list[Entry]:
    return [e for e in (entries or []) if e.id not in ids]


def _remove_entry_id(entries: list[Entry] | None, id: str) -> list[Entry]:
    return _remove_entry_ids(entries, {id})


def add(
    registry: Registry,
    step: str,
    how: str,
    id: str,
    fn: AnyFn,
    order: int,
    props: dict | None = None,
) -> Registry:
    """Register advice `fn` on `step` with combinator `how` under `id`, tagged
    with add-order `order`. Re-adding an existing id replaces it and moves it
    to the top of the stack for its depth (given a fresh, larger order)."""
    return {
        **registry,
        step: [*_remove_entry_id(registry.get(step), id), _entry(how, id, fn, order, props)],
    }


def remove_id(registry: Registry, step: str, id: str) -> Registry:
    return {**registry, step: _remove_entry_id(registry.get(step), id)}


def add_global(
    entries: list[Entry],
    how: str,
    id: str,
    fn: AnyFn,
    order: int,
    props: dict | None = None,
) -> list[Entry]:
    """Like `add`, but for a flat (not step-keyed) list of entries — used for
    advice that applies to every step."""
    return [*_remove_entry_id(entries, id), _entry(how, id, fn, order, props)]


def remove_global_id(entries: list[Entry], id: str) -> list[Entry]:
    return _remove_entry_id(entries, id)


def merge_entries(inner: list[Entry], outer: list[Entry], offset: int) -> list[Entry]:
    """Stack `outer` entries (inherited from an enclosing workflow) outside
    `inner` ones: outer seqs are rebased by `offset` so they sort above every
    inner seq, and an outer entry replaces an inner one with the same id.
    Depth still overrides placement at compose time."""
    rebased = [replace(e, seq=e.seq + offset) for e in outer]
    replaced_ids = {e.id for e in rebased}
    return [*_remove_entry_ids(inner, replaced_ids), *rebased]


def merge_registry(inner: Registry, outer: Registry, offset: int) -> Registry:
    """Merge an outer step-keyed registry over an inner one, step by step."""
    result = dict(inner)
    for step, entries in outer.items():
        result[step] = merge_entries(result.get(step, []), list(entries), offset)
    return result


def truthy(x: Any) -> bool:
    """Clojure truthiness: only None/False are falsy (0 and "" are true)."""
    return x is not None and x is not False


def _is_plain_map(x: Any) -> bool:
    return isinstance(x, dict)


def _blue_true(ret: Any) -> bool:
    """Truth predicate for blue step returns: success is true, failure is
    false. Compose can also be used directly with non-dict values, where
    Clojure truthiness is preserved."""
    if _is_plain_map(ret):
        exit = ret.get("blue/exit")
        return (exit if exit is not None else 0) == 0
    return truthy(ret)


async def call(fn: AnyFn, *args: Any) -> Any:
    """Call `fn`, awaiting its result when it is a coroutine — advice and
    steps may be sync or async and composition awaits either."""
    ret = fn(*args)
    if inspect.isawaitable(ret):
        ret = await ret
    return ret


def _as_async(fn: AnyFn) -> AnyFn:
    async def f(*args: Any) -> Any:
        return await call(fn, *args)

    return f


def _wrap(how: str, advice_fn: AnyFn, base: AnyFn) -> AnyFn:
    if how == "around":
        # around advice receives the prior chain as an async callable
        inner = _as_async(base)

        async def f(opts):
            return await call(advice_fn, inner, opts)

    elif how == "override":

        async def f(opts):
            return await call(advice_fn, opts)

    elif how == "before":

        async def f(opts):
            await call(advice_fn, opts)
            return await call(base, opts)

    elif how == "after":

        async def f(opts):
            ret = await call(base, opts)
            await call(advice_fn, opts)
            return ret

    elif how == "before-while":

        async def f(opts):
            a = await call(advice_fn, opts)
            return await call(base, opts) if truthy(a) else a

    elif how == "before-until":

        async def f(opts):
            a = await call(advice_fn, opts)
            return a if truthy(a) else await call(base, opts)

    elif how == "after-while":

        async def f(opts):
            ret = await call(base, opts)
            return await call(advice_fn, opts) if _blue_true(ret) else ret

    elif how == "after-until":

        async def f(opts):
            ret = await call(base, opts)
            return ret if _blue_true(ret) else await call(advice_fn, opts)

    elif how == "filter-args":

        async def f(opts):
            return await call(base, await call(advice_fn, opts))

    elif how == "filter-return":

        async def f(opts):
            return await call(advice_fn, await call(base, opts))

    else:  # pragma: no cover — _entry rejects unknown hows at add time
        raise AdviceError(f"unsupported advice combinator: {how}")

    return f


def ordered(entries: list[Entry]) -> list[Entry]:
    """Entries sorted innermost-first — the order `compose` wraps them: higher
    depth is more inward; at equal depth the most recently added (largest seq)
    is outermost."""
    return sorted(entries, key=lambda e: (-e.depth, e.seq))


def compose(base: AnyFn, entries: list[Entry]) -> AnyFn:
    """Wrap `base` with `entries`, ordered like Emacs nadvice: lower depth is
    more outward; at equal depth the most recently added (largest seq) is
    outermost. Builds inside-out over `ordered` (innermost-first) entries.
    The composed function is always async."""
    f = base
    for e in ordered(entries):
        f = _wrap(e.how, e.fn, f)
    return _as_async(f)
