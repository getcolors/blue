# The workflow engine: a graph of steps threaded by an opts dict.
#
# - wire_fn: (step, run_opts) -> [fn, *next_steps] — the static happy-path
#   graph for this run. It may depend on stable run-level inputs such as
#   "blue/event". Multiple successors run in parallel.
# - next_fn (optional): (step, default_next, opts) -> [(next_step, opts), ...]
#   — dynamic routing, error branching, fan-out.
# - Steps report outcome via "blue/exit" (0 ok, >0 error), "blue/err",
#   "blue/trace". Raised exceptions are caught and converted.
# - Branches converging on the same step join: the join step runs once with
#   the fork-point opts plus "blue/branches" (list of branch results).
# - A branch failing inside a fork lets the in-flight siblings finish their
#   current step, then the fork collapses: the join is skipped and the worst
#   exit propagates, with all branch results under "blue/branches".
# - advice_add attaches advice to one step; advice_add_all to every step.
#   Both stack in strict add order (most recently added, from either, is
#   outermost), unless a depth prop overrides placement (lower = more
#   outward), as in Emacs.
# - Advice is inherited across `step` embeds: a run stamps its effective
#   advice into opts and a nested run merges it over the child's own — step
#   names match flat at any depth, ancestor advice is outermost, and an
#   ancestor entry replaces a same-id child entry. advice_plan shows the
#   composed stack for a step.

from __future__ import annotations

import asyncio
import itertools
import traceback
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from . import advice as _advice
from .advice import AnyFn, Entry, Registry

Opts = dict
StepFn = Callable[[Opts], Any]
WireFn = Callable[[str, Opts], Any]
NextFn = Callable[[str, list | None, Opts], Any]

INHERITED = "blue.workflow/inherited"
ROOT = "blue.workflow/root"


class StepError(Exception):
    """Raise from a step to choose the failure's exit code (the ex-info
    equivalent); any other raise converts with exit 1."""

    def __init__(self, message: str, *, exit: int = 1):
        super().__init__(message)
        self.exit = exit


@dataclass(frozen=True)
class Workflow:
    start: str
    end: str | None
    wire_fn: WireFn
    next_fn: NextFn | None
    advice: Registry
    advice_all: list[Entry]
    advice_seq: int


def workflow(
    *,
    start: str,
    wire_fn: WireFn,
    end: str | None = None,
    next_fn: NextFn | None = None,
) -> Workflow:
    """Construct a workflow. `start` is required; `end` is an optional slice
    boundary (it runs, then the workflow stops); `wire_fn` is required and is
    called as wire_fn(step, run_opts); `next_fn` is optional."""
    if not isinstance(start, str) or start == "":
        raise ValueError("workflow start must be a step name string")
    if not callable(wire_fn):
        raise ValueError("workflow wire_fn must be a function")
    return Workflow(
        start=start,
        end=end,
        wire_fn=wire_fn,
        next_fn=next_fn,
        advice={},
        advice_all=[],
        advice_seq=0,
    )


def advice_add(
    wf: Workflow,
    step: str,
    how: str,
    id: str,
    fn: AnyFn,
    props: dict | None = None,
) -> Workflow:
    """Return a workflow with advice `fn` added on `step` (combinator `how`,
    explicit `id`). Pure — the original workflow is untouched. Composes with
    any all-steps advice (see `advice_add_all`) in strict add order: whichever
    was added more recently is outermost. `props` may carry depth (-100..100,
    default 0), which overrides add order: lower depth pushes the advice
    outward, higher pushes it inward, as with Emacs hook depths."""
    s = wf.advice_seq
    return replace(wf, advice=_advice.add(wf.advice, step, how, id, fn, s, props), advice_seq=s + 1)


def advice_remove(wf: Workflow, step: str, id: str) -> Workflow:
    """Return a workflow with the advice registered under `id` on `step` removed."""
    return replace(wf, advice=_advice.remove_id(wf.advice, step, id))


def advice_add_all(
    wf: Workflow, how: str, id: str, fn: AnyFn, props: dict | None = None
) -> Workflow:
    """Return a workflow with advice `fn` added on every step. Pure. Composes
    with any per-step advice in strict add order."""
    s = wf.advice_seq
    return replace(
        wf, advice_all=_advice.add_global(wf.advice_all, how, id, fn, s, props), advice_seq=s + 1
    )


def advice_remove_all(wf: Workflow, id: str) -> Workflow:
    """Return a workflow with the all-steps advice registered under `id` removed."""
    return replace(wf, advice_all=_advice.remove_global_id(wf.advice_all, id))


# --- advice inheritance across embeds ----------------------------------------
# A run stamps its effective registries into every step's opts under
# "blue.workflow/inherited"; `step` forwards the stamp into the nested run,
# whose `_inherit` merges it over the child's own advice. Inheritance is
# transitive because each run stamps its already-merged registries.


def _inherit(wf: Workflow, inherited: dict | None) -> Workflow:
    """Merge an inherited registry payload from an enclosing run over `wf`'s
    own advice: inherited entries stack outside the child's own, and an
    inherited entry replaces a same-id child entry (per step, or in the
    all-steps list). Step names are flat — whatever an ancestor advised under
    a name applies to this workflow's step of that name."""
    if not inherited:
        return wf
    n = wf.advice_seq
    return replace(
        wf,
        advice=_advice.merge_registry(wf.advice, inherited["advice"], n),
        advice_all=_advice.merge_entries(wf.advice_all, list(inherited["advice_all"]), n),
    )


def advice_plan(wfs: Workflow | list[Workflow], step: str) -> list[dict]:
    """Debugging: the advice stack that would wrap `step` at run time,
    outermost first. `wfs` is a single workflow or a chain
    [outermost ... innermost] — the workflows a run traverses to reach `step`
    through `step` embeds. Returns [{id, how, depth, seq, scope, level}, ...]
    where scope is "step" or "all" and level indexes the chain element that
    registered the advice (0 = outermost). A child entry replaced by a same-id
    ancestor entry does not appear."""
    chain = wfs if isinstance(wfs, list) else [wfs]

    def tag(wf: Workflow, level: int) -> Workflow:
        return replace(
            wf,
            advice={
                k: [replace(e, scope="step", level=level) for e in es]
                for k, es in wf.advice.items()
            },
            advice_all=[replace(e, scope="all", level=level) for e in wf.advice_all],
        )

    eff: dict | None = None
    for level, wf in enumerate(chain):
        merged = _inherit(tag(wf, level), eff)
        eff = _inherited_payload(merged)
    entries = [*(eff["advice_all"] if eff else []), *((eff["advice"] if eff else {}).get(step, []))]
    return [
        {
            "id": e.id,
            "how": e.how,
            "depth": e.depth,
            "seq": e.seq,
            "scope": e.scope,
            "level": e.level,
        }
        for e in reversed(_advice.ordered(entries))
    ]


# --- static graph (for join scheduling) ---------------------------------------


def _static_successors(wire_fn: WireFn, step: str, run_opts: Opts) -> list[str]:
    # Deliberately lenient: this graph exists only for join detection, so a
    # name the wire_fn can't resolve (None or a raise) just contributes no
    # static edges — next_fn may own routing for it. Real wiring bugs still
    # surface when the step runs: _run_step calls the same wire_fn and
    # converts the failure to "blue/exit". Raising here would escape `run`
    # uncaught, bypassing the Unix-style outcome contract.
    try:
        decl = wire_fn(step, run_opts)
        return [str(s) for s in decl[1:]] if decl else []
    except Exception:
        return []


def _static_graph(wire_fn: WireFn, start: str, run_opts: Opts) -> dict[str, list[str]]:
    g: dict[str, list[str]] = {}
    frontier = [start]
    while frontier:
        s = frontier.pop(0)
        if s in g:
            continue
        succ = _static_successors(wire_fn, s, run_opts)
        g[s] = succ
        frontier.extend(succ)
    return g


def _reaches(g: dict[str, list[str]], from_step: str, to_step: str) -> bool:
    """Can a branch currently at `from_step` (about to run it) later arrive at
    `to_step`, following static wire_fn edges?"""
    seen: set[str] = set()
    frontier = list(g.get(from_step, []))
    while frontier:
        s = frontier.pop()
        if s == to_step:
            return True
        if s in seen:
            continue
        seen.add(s)
        frontier.extend(g.get(s, []))
    return False


# --- freezing ------------------------------------------------------------------
# Opts are deep-frozen at the step boundary: the error model guarantees a
# throwing step's partial work is discarded (failure opts are its *input*),
# which is only real if steps cannot mutate their input.


def _frozen(*_args: Any, **_kwargs: Any) -> Any:
    raise TypeError("step input opts are deep-frozen; steps must return new dicts")


class FrozenDict(dict):
    __setitem__ = _frozen
    __delitem__ = _frozen
    __ior__ = _frozen
    clear = _frozen
    pop = _frozen
    popitem = _frozen
    setdefault = _frozen
    update = _frozen


class FrozenList(list):
    __setitem__ = _frozen
    __delitem__ = _frozen
    __iadd__ = _frozen
    __imul__ = _frozen
    append = _frozen
    extend = _frozen
    insert = _frozen
    remove = _frozen
    pop = _frozen
    clear = _frozen
    sort = _frozen
    reverse = _frozen


def _deep_freeze(x: Any, _memo: dict | None = None) -> Any:
    memo = _memo if _memo is not None else {}
    if isinstance(x, dict):
        if id(x) in memo:
            return memo[id(x)]
        fd = FrozenDict()
        memo[id(x)] = fd
        for k, v in x.items():
            dict.__setitem__(fd, _deep_freeze(k, memo), _deep_freeze(v, memo))
        return fd
    if isinstance(x, list):
        if id(x) in memo:
            return memo[id(x)]
        fl = FrozenList()
        memo[id(x)] = fl
        for v in x:
            list.append(fl, _deep_freeze(v, memo))
        return fl
    if isinstance(x, (tuple, frozenset)):
        if id(x) in memo:
            return memo[id(x)]
        values = [_deep_freeze(v, memo) for v in x]
        # A tuple can participate in a cycle through a list or dict. That
        # recursive visit may already have built its frozen counterpart.
        if id(x) not in memo:
            memo[id(x)] = tuple(values) if isinstance(x, tuple) else frozenset(values)
        return memo[id(x)]
    if x is None or type(x) in (str, bytes, bool, int, float, complex, range, Entry) or callable(x):
        return x
    raise TypeError("unsupported mutable step input; use dicts, lists, tuples and immutable scalars")


# --- running one step ----------------------------------------------------------


def _stack_trace(t: BaseException) -> str:
    return "".join(traceback.format_exception(type(t), t, t.__traceback__))


def _err_message(t: BaseException) -> str:
    return str(t) or type(t).__name__


def _exit_of(opts: Opts) -> int:
    exit = opts.get("blue/exit")
    return exit if exit is not None else 0


def failed(opts: Opts) -> bool:
    return _exit_of(opts) > 0


def _step_failure(opts: Opts, t: BaseException) -> Opts:
    return {
        **opts,
        "blue/exit": t.exit if isinstance(t, StepError) else 1,
        "blue/err": _err_message(t),
        "blue/trace": _stack_trace(t),
    }


def _scheduler_failure(opts: Opts, t: BaseException) -> Opts:
    return {
        **opts,
        "blue/exit": 1,
        "blue/err": _err_message(t),
        "blue/trace": _stack_trace(t),
    }


def _with_default_exit(opts: Opts) -> Opts:
    if opts.get("blue/exit") is None:
        return {**opts, "blue/exit": 0}
    return opts


def _is_plain_map(x: Any) -> bool:
    return isinstance(x, dict)


def _inherited_payload(wf: Workflow) -> dict:
    return {"advice": wf.advice, "advice_all": wf.advice_all}


def _step_advice(wf: Workflow, step: str) -> list[Entry]:
    return [*wf.advice_all, *wf.advice.get(step, [])]


async def _run_step(wf: Workflow, step: str, run_opts: Opts, opts: Opts) -> Opts:
    try:
        decl = wf.wire_fn(step, run_opts)
        f = decl[0] if decl else None
        if not callable(f):
            raise StepError(f"no function wired for step {step}")
        input_opts = _deep_freeze({**opts, INHERITED: _inherited_payload(wf), "blue/step": step})
        ret = await _advice.compose(f, _step_advice(wf, step))(input_opts)
        if not _is_plain_map(ret):
            raise StepError(f"step {step} returned a non-map: {ret!r}")
        rest = {k: v for k, v in ret.items() if k != INHERITED}
        return _with_default_exit(rest)
    except Exception as t:
        return _step_failure(opts, t)


def _next_pairs(wf: Workflow, step: str, run_opts: Opts, opts: Opts) -> list[tuple[str, Opts]]:
    """Successor (step, opts) pairs for `step` after it produced `opts`. The
    end step is a hard boundary; without next_fn an error halts."""
    if step == wf.end:
        return []
    decl = wf.wire_fn(step, run_opts)
    rest = [str(s) for s in decl[1:]] if decl else []
    default_next = rest if rest else None
    if wf.next_fn:
        return [tuple(p) for p in (wf.next_fn(step, default_next, opts) or [])]
    if failed(opts):
        return []
    return [(s, opts) for s in rest]


# --- the scheduler ---------------------------------------------------------------

# Live entries: LiveEntry(step, opts, parent, forks=[frame…]) where a fork
# frame is ForkFrame(id=fork_id, opts=fork_point_opts). `parent` identifies
# the run-unit that produced the entry, so same-step entries from one fan-out
# run individually while entries converging from different origins join.
# Finished branches are Terminal(opts, forks=[frame…]); the frames let a
# failure collapse its enclosing fork.


@dataclass
class ForkFrame:
    id: str
    opts: Opts


@dataclass
class LiveEntry:
    step: str
    opts: Opts
    parent: str
    forks: list[ForkFrame]


@dataclass
class Terminal:
    opts: Opts
    forks: list[ForkFrame]


@dataclass
class UnitResult:
    new_entries: list[LiveEntry] = field(default_factory=list)
    terminals: list[Terminal] = field(default_factory=list)


@dataclass
class Unit:
    kind: str  # "single" | "join"
    step: str
    entry: LiveEntry | None = None
    entries: list[LiveEntry] | None = None


_unit_counter = itertools.count()


def _children(
    uid: str, opts: Opts, pairs: list[tuple[str, Opts]], forks: list[ForkFrame]
) -> UnitResult:
    if len(pairs) == 0:
        return UnitResult(terminals=[Terminal(opts, forks)])
    if len(pairs) == 1:
        s, o = pairs[0]
        return UnitResult(new_entries=[LiveEntry(s, o, uid, forks)])
    frame = ForkFrame(uid, opts)
    return UnitResult(new_entries=[LiveEntry(s, o, uid, [*forks, frame]) for s, o in pairs])


def _terminal_result(opts: Opts, forks: list[ForkFrame]) -> UnitResult:
    return UnitResult(terminals=[Terminal(opts, forks)])


def _branch_worst_exit(branch_opts: list[Opts]) -> int:
    return max(_exit_of(o) for o in branch_opts)


def _join_forks(entries: list[LiveEntry]) -> list[ForkFrame]:
    first = entries[0].forks if entries else []
    length = 0
    while length < len(first) and all(
        len(e.forks) > length and e.forks[length].id == first[length].id for e in entries
    ):
        length += 1
    return first[:length]


def _failed_join_result(
    fork_opts: Opts, forks: list[ForkFrame], branch_opts: list[Opts], worst: int
) -> UnitResult:
    bad = next((o for o in branch_opts if _exit_of(o) == worst), None)
    return _terminal_result(
        {
            **fork_opts,
            "blue/exit": worst,
            "blue/err": bad.get("blue/err") if bad else None,
            "blue/trace": bad.get("blue/trace") if bad else None,
            "blue/branches": branch_opts,
        },
        forks,
    )


async def _run_single_unit(
    wf: Workflow, uid: str, step: str, run_opts: Opts, entry: LiveEntry
) -> UnitResult:
    opts = await _run_step(wf, step, run_opts, entry.opts)
    return _children(uid, opts, _next_pairs(wf, step, run_opts, opts), entry.forks)


async def _run_join_unit(
    wf: Workflow, uid: str, step: str, run_opts: Opts, entries: list[LiveEntry]
) -> UnitResult:
    branch_opts = [e.opts for e in entries]
    forks = _join_forks(entries)
    fork_opts = forks[-1].opts if forks else branch_opts[0]
    rest_forks = forks[:-1] if forks else forks
    worst = _branch_worst_exit(branch_opts)
    if worst > 0:
        return _failed_join_result(fork_opts, rest_forks, branch_opts, worst)
    opts = await _run_step(wf, step, run_opts, {**fork_opts, "blue/branches": branch_opts})
    return _children(uid, opts, _next_pairs(wf, step, run_opts, opts), rest_forks)


def _unit_base_opts(unit: Unit) -> Opts:
    if unit.entry is not None:
        return unit.entry.opts
    entries = unit.entries or []
    forks = _join_forks(entries)
    base = forks[-1].opts if forks else entries[0].opts if entries else {}
    return {**base, "blue/branches": [e.opts for e in entries]}


def _unit_forks(unit: Unit) -> list[ForkFrame]:
    if unit.entry is not None:
        return unit.entry.forks
    return _join_forks(unit.entries or [])[:-1]


async def _run_unit(wf: Workflow, run_opts: Opts, unit: Unit) -> UnitResult:
    uid = f"blue-unit-{next(_unit_counter)}"
    try:
        if unit.kind == "single":
            return await _run_single_unit(wf, uid, unit.step, run_opts, unit.entry)
        return await _run_join_unit(wf, uid, unit.step, run_opts, unit.entries)
    except Exception as t:
        return _terminal_result(_scheduler_failure(_unit_base_opts(unit), t), _unit_forks(unit))


def _failed_fork_branch(branch: Terminal) -> bool:
    return failed(branch.opts) and len(branch.forks) > 0


def _in_fork(fork_id: str, entry: LiveEntry | Terminal) -> bool:
    return any(f.id == fork_id for f in entry.forks)


def _collapse_fork(
    live: list[LiveEntry], finished: list[Terminal], bad: Terminal
) -> tuple[list[LiveEntry], list[Terminal]]:
    frame = bad.forks[-1]
    members = [
        *(e for e in finished if _in_fork(frame.id, e)),
        *(e for e in live if _in_fork(frame.id, e)),
    ]
    branch_opts = [m.opts for m in members]
    worst = _branch_worst_exit(branch_opts)
    worst_opts = next((o for o in branch_opts if _exit_of(o) == worst), None)
    collapsed = Terminal(
        opts={
            **frame.opts,
            "blue/exit": worst,
            "blue/err": worst_opts.get("blue/err") if worst_opts else None,
            "blue/trace": worst_opts.get("blue/trace") if worst_opts else None,
            "blue/branches": branch_opts,
        },
        forks=bad.forks[:-1],
    )
    return (
        [e for e in live if not _in_fork(frame.id, e)],
        [*(e for e in finished if not _in_fork(frame.id, e)), collapsed],
    )


def _collapse_doomed(
    live: list[LiveEntry], finished: list[Terminal]
) -> tuple[list[LiveEntry], list[Terminal]]:
    """While a finished branch failed inside a fork, collapse that fork:
    absorb its live entries (their current opts are their branch results —
    siblings finished their step, nothing new starts) and its finished
    branches, skip the join, and emit a terminal carrying the worst exit and
    "blue/branches". Cascades outward through nested forks."""
    while True:
        bad = next((t for t in finished if _failed_fork_branch(t)), None)
        if bad is None:
            return live, finished
        live, finished = _collapse_fork(live, finished, bad)


def _dissoc_inherited(opts: Opts) -> Opts:
    if INHERITED not in opts:
        return opts
    return {k: v for k, v in opts.items() if k != INHERITED}


def _finalize(finished: list[Terminal]) -> Opts:
    terminals = [_dissoc_inherited(t.opts) for t in finished]
    if len(terminals) == 0:
        return {"blue/exit": 0}
    if len(terminals) == 1:
        return terminals[0]
    return next((t for t in terminals if failed(t)), terminals[-1])


def _blocked_step(g: dict[str, list[str]], live: list[LiveEntry], step: str) -> bool:
    return any(e.step != step and _reaches(g, e.step, step) for e in live)


def _ready_steps(
    g: dict[str, list[str]], live: list[LiveEntry], by_step: dict[str, list[LiveEntry]]
) -> list[str]:
    steps = list(by_step.keys())
    unblocked = [s for s in steps if not _blocked_step(g, live, s)]
    return unblocked if unblocked else steps


def _same_origin(entries: list[LiveEntry]) -> bool:
    return len(entries) == 1 or all(e.parent == entries[0].parent for e in entries)


def _step_units(step: str, entries: list[LiveEntry]) -> list[Unit]:
    if _same_origin(entries):
        return [Unit(kind="single", step=step, entry=entry) for entry in entries]
    return [Unit(kind="join", step=step, entries=entries)]


async def _scheduler_step(
    wf: Workflow,
    run_opts: Opts,
    g: dict[str, list[str]],
    live: list[LiveEntry],
    finished: list[Terminal],
) -> tuple[list[LiveEntry], list[Terminal]]:
    by_step: dict[str, list[LiveEntry]] = {}
    for e in live:
        by_step.setdefault(e.step, []).append(e)
    ready = _ready_steps(g, live, by_step)
    ready_set = set(ready)
    waiting = [s for s in by_step if s not in ready_set]
    units = [u for s in ready for u in _step_units(s, by_step[s])]
    results = await asyncio.gather(*(_run_unit(wf, run_opts, u) for u in units))
    return _collapse_doomed(
        [*(e for s in waiting for e in by_step[s]), *(e for r in results for e in r.new_entries)],
        [*finished, *(t for r in results for t in r.terminals)],
    )


def step(
    wf: Workflow,
    *,
    in_fn: Callable[[Opts], Opts] | None = None,
    out_fn: Callable[[Opts, Opts], Opts] | None = None,
) -> StepFn:
    """Turn a workflow into a step function (opts -> awaitable opts), so
    workflows compose into higher-level workflows: wire the result like any
    other step, advise it, fan it out. The sub-workflow's "blue/exit"
    propagates naturally, and ambient keys like "blue/event" and
    "blue/dry-run" flow in with opts.

    The enclosing run's advice is inherited: the nested run merges it over
    the sub-workflow's own. The engine re-stamps the inherited registry after
    `in_fn` runs, so `in_fn` may build sub-opts from scratch without severing
    inheritance.

    Options:
      in_fn: (opts) -> sub_opts         — shape the opts entering the
                                          sub-workflow
      out_fn: (opts, sub_result) -> opts — merge the sub-result back into the
                                           parent's opts (default: the
                                           sub-result itself is the step's
                                           result)
    """

    async def f(o: Opts) -> Opts:
        inherited = o.get(INHERITED)
        sub_opts = in_fn(o) if in_fn else o
        if inherited is not None:
            sub_opts = {**sub_opts, INHERITED: inherited}
        result = await run(wf, sub_opts)
        return out_fn(o, result) if out_fn else result

    return f


async def run(wf: Workflow, opts: Opts) -> Opts:
    """Run the workflow from its start step with `opts` as the initial state.
    The same initial opts are passed to `wire_fn` as `run_opts` for the whole
    run, so the static graph stays stable. Returns the final opts dict; its
    "blue/exit" is the workflow's exit code. When `opts` carries an inherited
    advice registry (stamped by an enclosing run through `step`), it is merged
    over the workflow's own advice before anything runs."""
    run_opts = opts
    w = _inherit(wf, opts.get(INHERITED))
    g = _static_graph(w.wire_fn, w.start, run_opts)
    live: list[LiveEntry] = [LiveEntry(w.start, opts, ROOT, [])]
    finished: list[Terminal] = []
    while live:
        live, finished = await _scheduler_step(w, run_opts, g, live, finished)
    return _finalize(finished)
