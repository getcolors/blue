import asyncio

import pytest

from blue.workflow import StepError, run, step, workflow


def mark(k):
    def f(o):
        return {**o, "seen": [*o.get("seen", []), k]}

    return f


async def test_linear_happy_path():
    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (mark("a"), "t/b"),
            "t/b": (mark("b"), "t/c"),
            "t/c": (mark("c"),),
        }.get(s),
    )
    res = await run(w, {})
    assert res["seen"] == ["a", "b", "c"]
    assert res["blue/exit"] == 0


async def test_error_halts_without_next_fn():
    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (mark("a"), "t/b"),
            "t/b": (lambda o: {**o, "blue/exit": 3}, "t/c"),
            "t/c": (mark("c"),),
        }.get(s),
    )
    res = await run(w, {})
    assert res["blue/exit"] == 3
    # t/c must not run
    assert res["seen"] == ["a"]


async def test_exception_becomes_exit_err_trace():
    def boom(_o):
        raise RuntimeError("boom")

    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (boom, "t/b"),
            "t/b": (mark("b"),),
        }.get(s),
    )
    res = await run(w, {})
    assert res["blue/exit"] == 1
    assert res["blue/err"] == "boom"
    assert isinstance(res["blue/trace"], str)
    # t/b must not run
    assert "seen" not in res


async def test_step_error_chooses_the_exit_code():
    def boom(_o):
        raise StepError("custom", exit=7)

    w = workflow(start="t/a", wire_fn=lambda s, _: (boom,))
    res = await run(w, {})
    assert res["blue/exit"] == 7
    assert res["blue/err"] == "custom"


async def test_a_step_returning_a_non_map_fails_loudly():
    # a forgotten return: the step yields None
    w = workflow(start="t/a", wire_fn=lambda s, _: (lambda o: None,))
    res = await run(w, {})
    assert res["blue/exit"] == 1
    assert "returned a non-map" in res["blue/err"]


async def test_a_step_mutating_its_frozen_input_fails_through_the_contract():
    def mutate(o):
        o["x"] = 1  # assignment to a frozen dict raises
        return o

    w = workflow(start="t/a", wire_fn=lambda s, _: (mutate,))
    res = await run(w, {})
    assert res["blue/exit"] == 1
    assert "frozen" in res["blue/err"].lower()


async def test_end_step_is_an_inclusive_slice_boundary():
    def wire(s, _):
        return {
            "t/a": (mark("a"), "t/b"),
            "t/b": (mark("b"), "t/c"),
            "t/c": (mark("c"),),
        }.get(s)

    # end stops after running it
    assert (await run(workflow(start="t/a", end="t/b", wire_fn=wire), {}))["seen"] == ["a", "b"]
    # start skips earlier steps
    assert (await run(workflow(start="t/b", wire_fn=wire), {}))["seen"] == ["b", "c"]


async def test_wire_fn_can_select_an_event_specific_static_graph():
    def wire(s, run_opts):
        key = f"{run_opts['blue/event']} {s}"
        return {
            "create t/start": (mark("start"), "t/node"),
            "create t/node": (mark("node"), "t/ansible"),
            "create t/ansible": (mark("ansible"),),
            "delete t/start": (mark("start"), "t/ansible"),
            "delete t/ansible": (mark("ansible"), "t/node"),
            "delete t/node": (mark("node"),),
        }.get(key)

    w = workflow(start="t/start", wire_fn=wire)
    assert (await run(w, {"blue/event": "create"}))["seen"] == ["start", "node", "ansible"]
    assert (await run(w, {"blue/event": "delete"}))["seen"] == ["start", "ansible", "node"]


async def test_wire_fn_uses_stable_run_opts_not_branch_opts():
    def wire(s, run_opts):
        return {
            "t/start": (lambda o: o, "t/work"),
            "t/work": (
                mark("work"),
                "t/branch-done" if run_opts.get("route") == "branch" else "t/run-done",
            ),
            "t/run-done": (mark("run-done"),),
            "t/branch-done": (mark("branch-done"),),
        }.get(s)

    def next_fn(s, default_next, opts):
        if s == "t/start":
            return [("t/work", {**opts, "route": "branch"})]
        return [(n, opts) for n in (default_next or [])]

    res = await run(workflow(start="t/start", wire_fn=wire, next_fn=next_fn), {"route": "run"})
    assert res["seen"] == ["work", "run-done"]
    # the step still receives branch-local opts
    assert res["route"] == "branch"


async def test_next_fn_reroutes_errors():
    def wire(s, _):
        return {
            "t/a": (lambda o: {**o, "blue/exit": 7}, "t/b"),
            "t/b": (mark("b"),),
            "t/cleanup": (lambda o: mark("cleanup")({**o, "blue/exit": 0}),),
        }.get(s)

    def next_fn(s, default_next, opts):
        if (opts.get("blue/exit") or 0) > 0:
            return None if s == "t/cleanup" else [("t/cleanup", opts)]
        return [(n, opts) for n in (default_next or [])]

    res = await run(workflow(start="t/a", wire_fn=wire, next_fn=next_fn), {})
    assert res["seen"] == ["cleanup"]
    assert res["blue/exit"] == 0


async def test_next_fn_none_terminates():
    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (mark("a"), "t/b"),
            "t/b": (mark("b"),),
        }.get(s),
        next_fn=lambda s, d, o: None,
    )
    assert (await run(w, {}))["seen"] == ["a"]


async def test_static_fork_and_join():
    # t/a forks to t/b and t/c; t/c has a longer chain (t/c -> t/c2); both
    # arrive at t/d, which must run once with both branches collected.
    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (mark("a"), "t/b", "t/c"),
            "t/b": (mark("b"), "t/d"),
            "t/c": (mark("c"), "t/c2"),
            "t/c2": (mark("c2"), "t/d"),
            "t/d": (lambda o: {**o, "joined": [b["seen"] for b in o["blue/branches"]]},),
        }.get(s),
    )
    res = await run(w, {})
    assert res["blue/exit"] == 0
    assert len(res["blue/branches"]) == 2
    # join waited for the longer branch
    assert {",".join(s) for s in res["joined"]} == {"a,b", "a,c,c2"}
    # join base opts come from the fork point, not a branch
    assert res["seen"] == ["a"]


async def test_dynamic_fan_out_and_join():
    def next_fn(s, default_next, opts):
        if (opts.get("blue/exit") or 0) > 0:
            return []
        if s == "t/fan":
            return [("t/work", {**opts, "n": n}) for n in (1, 2, 3)]
        return [(n, opts) for n in (default_next or [])]

    w = workflow(
        start="t/fan",
        wire_fn=lambda s, _: {
            "t/fan": (mark("fan"), "t/work"),
            "t/work": (lambda o: {**o, "done": o["n"]}, "t/join"),
            "t/join": (lambda o: {**o, "collected": {b["done"] for b in o["blue/branches"]}},),
        }.get(s),
        next_fn=next_fn,
    )
    res = await run(w, {})
    assert res["blue/exit"] == 0
    assert len(res["blue/branches"]) == 3
    assert res["collected"] == {1, 2, 3}


async def test_branch_failure_skips_the_join_and_propagates_the_worst_exit():
    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (mark("a"), "t/ok", "t/bad"),
            "t/ok": (mark("ok"), "t/d"),
            "t/bad": (lambda o: {**o, "blue/exit": 5, "blue/err": "bad branch"}, "t/d"),
            "t/d": (mark("join-ran"),),
        }.get(s),
    )
    res = await run(w, {})
    assert res["blue/exit"] == 5
    assert res["blue/err"] == "bad branch"
    # join must be skipped
    assert "join-ran" not in (res.get("seen") or [])
    # both branches finished
    assert len(res["blue/branches"]) == 2


async def test_workflows_compose_as_steps():
    sub = workflow(
        start="s/a",
        wire_fn=lambda s, _: {
            "s/a": (mark("sub-a"), "s/b"),
            "s/b": (mark("sub-b"),),
        }.get(s),
    )
    parent = workflow(
        start="p/a",
        wire_fn=lambda s, _: {
            "p/a": (mark("p-a"), "p/sub"),
            "p/sub": (step(sub), "p/z"),
            "p/z": (mark("p-z"),),
        }.get(s),
    )
    res = await run(parent, {})
    assert res["blue/exit"] == 0
    assert res["seen"] == ["p-a", "sub-a", "sub-b", "p-z"]


async def test_sub_workflow_failure_halts_the_parent():
    sub = workflow(
        start="s/a",
        wire_fn=lambda s, _: (lambda o: {**o, "blue/exit": 4, "blue/err": "sub failed"},),
    )
    parent = workflow(
        start="p/sub",
        wire_fn=lambda s, _: {
            "p/sub": (step(sub), "p/z"),
            "p/z": (mark("p-z"),),
        }.get(s),
    )
    res = await run(parent, {})
    assert res["blue/exit"] == 4
    assert res["blue/err"] == "sub failed"
    # p/z must not run
    assert "seen" not in res


async def test_step_in_fn_out_fn_scope_the_sub_workflow():
    sub = workflow(start="s/a", wire_fn=lambda s, _: (lambda o: {**o, "result": 2 * o["n"]},))
    parent = workflow(
        start="p/sub",
        wire_fn=lambda s, _: (
            step(
                sub,
                in_fn=lambda o: {"blue/event": o["blue/event"], "n": o["parentN"]},
                out_fn=lambda o, r: {**o, "doubled": r["result"]},
            ),
        ),
    )
    res = await run(parent, {"blue/event": "create", "parentN": 21, "keepMe": "yes"})
    assert res["doubled"] == 42
    # out_fn preserved the parent opts
    assert res["keepMe"] == "yes"


async def test_parallel_branches_actually_run_concurrently():
    in_flight = 0
    peak = 0

    async def slow(o):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return o

    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (lambda o: o, "t/x", "t/y", "t/z"),
            "t/x": (slow, "t/d"),
            "t/y": (slow, "t/d"),
            "t/z": (slow, "t/d"),
            "t/d": (lambda o: o,),
        }.get(s),
    )
    res = await run(w, {"blue/exit": 0})
    assert res["blue/exit"] == 0
    # branches overlapped in time
    assert peak > 1


@pytest.mark.parametrize("right_length", [1, 4])
@pytest.mark.parametrize("enclosing_fork", [False, True])
async def test_nested_join_uses_common_ancestry(right_length, enclosing_fork):
    graph = {
        "t/root": ["t/fork", "t/outside"] if enclosing_fork else ["t/fork"],
        "t/fork": ["t/left", "t/right1"], "t/left": ["t/leaf1", "t/leaf2"],
        "t/leaf1": ["t/join"], "t/leaf2": ["t/join"],
        "t/join": ["t/final"] if enclosing_fork else [],
        "t/outside": ["t/final"], "t/final": [],
    }
    for i in range(1, right_length + 1):
        graph[f"t/right{i}"] = ["t/join" if i == right_length else f"t/right{i + 1}"]
    joins = []

    def wire(s, _):
        def execute(o):
            if s in ("t/join", "t/final"):
                joins.append({"step": s, "base": o["path"], "branches": o["blue/branches"]})
            return {**o, "path": [*o.get("path", []), s]}
        return [execute, *graph[s]]

    result = await run(workflow(start="t/root", wire_fn=wire), {})
    assert result["blue/exit"] == 0
    assert joins[0]["base"] == ["t/root", "t/fork"]
    assert len(joins[0]["branches"]) == 3
    assert len(joins) == (2 if enclosing_fork else 1)
    if enclosing_fork:
        assert joins[1]["base"] == ["t/root"]
        assert len(joins[1]["branches"]) == 2


async def test_failure_after_nested_join_does_not_collapse_joined_fork():
    graph = {
        "t/root": ["t/left", "t/right"], "t/left": ["t/a", "t/b"],
        "t/a": ["t/join"], "t/b": ["t/join"], "t/right": ["t/r2"],
        "t/r2": ["t/r3"], "t/r3": ["t/join"], "t/join": ["t/fail"], "t/fail": [],
    }

    def wire(s, _):
        def execute(o):
            return ({**o, "blue/exit": 8, "blue/err": "after join"} if s == "t/fail"
                    else {**o, "joined": s == "t/join" or o.get("joined", False)})
        return [execute, *graph[s]]

    result = await run(workflow(start="t/root", wire_fn=wire), {})
    assert result["blue/exit"] == 8
    assert result["joined"] is True
    assert len(result["blue/branches"]) == 3


@pytest.mark.parametrize("exits", [(3, 9), (9, 3), (9, 9)])
async def test_failed_join_preserves_worst_diagnostics(exits):
    def wire(s, _):
        if s == "t/root":
            return [lambda o: o, "t/a", "t/b"]
        if s == "t/join":
            def unexpected(_):
                raise AssertionError("join must not execute")
            return [unexpected]
        n = 0 if s == "t/a" else 1
        return [lambda o: {**o, "blue/exit": exits[n], "blue/err": f"error {n}",
                           "blue/trace": f"trace {n}"}, "t/join"]

    result = await run(workflow(start="t/root", wire_fn=wire,
                               next_fn=lambda _s, ns, o: [(n, o) for n in ns or []]), {})
    winner = 0 if exits[0] >= exits[1] else 1
    assert result["blue/exit"] == max(exits)
    assert result["blue/err"] == f"error {winner}"
    assert result["blue/trace"] == f"trace {winner}"
    assert len(result["blue/branches"]) == 2


async def test_tuple_members_are_frozen_before_step_mutation():
    original = {"project": ({"value": "before"},)}

    def mutate(o):
        o["project"][0]["value"] = "after"
        raise RuntimeError("later failure")

    result = await run(workflow(start="t/change", wire_fn=lambda *_: [mutate]), original)
    assert result["blue/exit"] == 1
    assert original["project"][0]["value"] == "before"
    assert result["project"][0]["value"] == "before"


@pytest.mark.parametrize("value", [{"before"}, bytearray(b"before"), object()])
async def test_unsupported_mutable_input_fails_before_step_runs(value):
    called = False

    def execute(o):
        nonlocal called
        called = True
        return o

    result = await run(workflow(start="t/change", wire_fn=lambda *_: [execute]), {"project": value})
    assert not called
    assert result["blue/exit"] == 1
    assert "unsupported mutable step input" in result["blue/err"]


async def test_freezing_preserves_tuple_cycles_and_callable_values():
    data = []
    cycle = (data,)
    data.append(cycle)
    result = await run(workflow(start="t/read", wire_fn=lambda *_: [
        lambda o: {**o, "cycle_preserved": o["data"][0][0] is o["data"], "value": o["compute"](1)}
    ]), {"data": cycle, "compute": lambda n: n + 1})
    assert result["blue/exit"] == 0
    assert result["cycle_preserved"]
    assert result["value"] == 2


async def test_join_routing_exceptions_preserve_common_fork_context():
    graph = {
        "t/root": ["t/left", "t/right"], "t/left": ["t/a", "t/b"],
        "t/a": ["t/join"], "t/b": ["t/join"], "t/right": ["t/r2"],
        "t/r2": ["t/r3"], "t/r3": ["t/join"], "t/join": [],
    }

    def wire(s, _):
        return [lambda o: {**o, "path": [*o.get("path", []), s]}, *graph[s]]

    def route(s, ns, o):
        if s == "t/join":
            raise RuntimeError("routing failed")
        return [(n, o) for n in ns or []]

    result = await run(workflow(start="t/root", wire_fn=wire, next_fn=route), {})
    assert result["blue/exit"] == 1
    assert result["blue/err"] == "routing failed"
    assert result["path"] == ["t/root"]
    assert len(result["blue/branches"]) == 3
