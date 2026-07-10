import itertools

import pytest

from blue import advice
from blue.advice import AdviceError, Entry
from blue.workflow import (
    advice_add,
    advice_add_all,
    advice_plan,
    advice_remove,
    advice_remove_all,
    run,
    step,
    workflow,
)


def log(o, x):
    return {**o, "log": [*o.get("log", []), x]}


def single_step_wf():
    return workflow(start="t/step", wire_fn=lambda s, _: (lambda o: log(o, "base"),))


async def test_filter_return_advice_stacks_lifo_most_recently_added_outermost():
    base = single_step_wf()
    advised = advice_add(
        advice_add(base, "t/step", "filter-return", "test/a", lambda o: log(o, "a")),
        "t/step",
        "filter-return",
        "test/b",
        lambda o: log(o, "b"),
    )
    assert (await run(advised, {}))["log"] == ["base", "a", "b"]
    # the original workflow is untouched (workflow-scoped advice)
    assert (await run(base, {}))["log"] == ["base"]


async def test_advice_remove_by_id():
    wf = single_step_wf()
    wf = advice_add(wf, "t/step", "filter-return", "test/a", lambda o: log(o, "a"))
    wf = advice_add(wf, "t/step", "filter-return", "test/b", lambda o: log(o, "b"))
    wf = advice_remove(wf, "t/step", "test/a")
    assert (await run(wf, {}))["log"] == ["base", "b"]


async def test_re_adding_the_same_id_replaces_and_moves_to_top():
    wf = single_step_wf()
    wf = advice_add(wf, "t/step", "filter-return", "test/a", lambda o: log(o, "a1"))
    wf = advice_add(wf, "t/step", "filter-return", "test/b", lambda o: log(o, "b"))
    wf = advice_add(wf, "t/step", "filter-return", "test/a", lambda o: log(o, "a2"))
    assert (await run(wf, {}))["log"] == ["base", "b", "a2"]


async def test_override_replaces_the_base():
    wf = advice_add(single_step_wf(), "t/step", "override", "test/o", lambda o: log(o, "override"))
    assert (await run(wf, {}))["log"] == ["override"]


async def test_around_controls_the_call():
    async def ar(f, o):
        return log(await f(log(o, "in")), "out")

    wf = advice_add(single_step_wf(), "t/step", "around", "test/ar", ar)
    assert (await run(wf, {}))["log"] == ["in", "base", "out"]


async def test_filter_args_transforms_input():
    wf = advice_add(single_step_wf(), "t/step", "filter-args", "test/fa", lambda o: {**o, "x": 1})
    assert (await run(wf, {}))["x"] == 1


async def test_before_and_after_run_for_side_effects_oldfuns_value_flows_out():
    seen = []
    wf = single_step_wf()

    def before(o):
        seen.append(("before", o.get("log")))
        return o

    def after(o):
        seen.append(("after", o.get("log")))
        return o

    wf = advice_add(wf, "t/step", "before", "test/b", before)
    wf = advice_add(wf, "t/step", "after", "test/a", after)
    res = await run(wf, {})
    # both see the step's input opts (Emacs: both get r); their returns are
    # ignored and OLDFUN's value flows out
    assert res["log"] == ["base"]
    assert seen == [("before", None), ("after", None)]


async def test_before_runs_newest_to_oldest_after_runs_oldest_to_newest():
    seen = []

    def note(k):
        def f(o):
            seen.append(k)
            return o

        return f

    wf = single_step_wf()
    wf = advice_add(wf, "t/step", "before", "test/b1", note("b1"))
    wf = advice_add(wf, "t/step", "before", "test/b2", note("b2"))
    wf = advice_add(wf, "t/step", "after", "test/a1", note("a1"))
    wf = advice_add(wf, "t/step", "after", "test/a2", note("a2"))
    await run(wf, {})
    assert seen == ["b2", "b1", "a1", "a2"]


async def test_throwing_advice_fails_the_step_through_the_contract():
    def boom(_o):
        raise RuntimeError("advice boom")

    wf = advice_add(single_step_wf(), "t/step", "before", "test/boom", boom)
    res = await run(wf, {})
    assert res["blue/exit"] == 1
    assert res["blue/err"] == "advice boom"
    assert isinstance(res["blue/trace"], str)


def two_step_wf():
    return workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (lambda o: log(o, "a"), "t/b"),
            "t/b": (lambda o: log(o, "b"),),
        }.get(s),
    )


async def test_advice_add_all_applies_to_every_step():
    wf = advice_add_all(two_step_wf(), "filter-return", "test/tag", lambda o: log(o, "all"))
    assert (await run(wf, {}))["log"] == ["a", "all", "b", "all"]


async def test_advice_add_all_interleaves_in_strict_add_order_with_per_step():
    wf = two_step_wf()
    wf = advice_add_all(wf, "filter-return", "test/g1", lambda o: log(o, "g1"))
    wf = advice_add(wf, "t/a", "filter-return", "test/pa", lambda o: log(o, "pa"))
    wf = advice_add_all(wf, "filter-return", "test/g2", lambda o: log(o, "g2"))
    res = await run(wf, {})
    # t/a ran with both global entries plus its own, in add order
    assert res["log"][:4] == ["a", "g1", "pa", "g2"]
    # t/b, with no per-step advice, only saw the globals in add order
    assert res["log"][4:] == ["b", "g1", "g2"]


async def test_advice_remove_all_removes_the_global_entry():
    wf = two_step_wf()
    wf = advice_add_all(wf, "filter-return", "test/g", lambda o: log(o, "g"))
    wf = advice_remove_all(wf, "test/g")
    assert (await run(wf, {}))["log"] == ["a", "b"]


# --- the while/until combinators ----------------------------------------------

_entry_seq = itertools.count()


def entry(how, fn, seq):
    return Entry(id=f"adv-{next(_entry_seq)}", how=how, fn=fn, seq=seq, depth=0)


class TestBeforeWhile:
    async def test_gates_the_inward_call(self):
        seen = []

        def base(o):
            seen.append("base")
            return log(o, "base")

        def w_true(_o):
            seen.append("w")
            return True

        # truthy advice lets the chain run; base sees the original opts
        f = advice.compose(base, [entry("before-while", w_true, 0)])
        assert (await f({}))["log"] == ["base"]
        assert seen == ["w", "base"]

        # None advice short-circuits: nothing inward runs, result is None
        seen.clear()

        def w_none(_o):
            seen.append("w")
            return None

        g = advice.compose(base, [entry("before-while", w_none, 0)])
        assert await g({}) is None
        assert seen == ["w"]

    async def test_stack_runs_newest_to_oldest_and_stops_on_none(self):
        seen = []

        def base(o):
            seen.append("base")
            return o

        def old(_o):
            seen.append("old")
            return True

        def new(_o):
            seen.append("new")
            return None

        f = advice.compose(base, [entry("before-while", old, 0), entry("before-while", new, 1)])
        assert await f({}) is None
        # the newest gate is outermost and stops the chain
        assert seen == ["new"]


class TestBeforeUntil:
    async def test_short_circuits_on_non_none(self):
        # a non-None advice return is the result; base never runs
        hit = advice_add(
            single_step_wf(), "t/step", "before-until", "test/bu", lambda o: log(o, "bu")
        )
        assert (await run(hit, {}))["log"] == ["bu"]
        # a None advice return falls through to the chain
        miss = advice_add(single_step_wf(), "t/step", "before-until", "test/bu", lambda o: None)
        assert (await run(miss, {}))["log"] == ["base"]


class TestAfterWhile:
    async def test_runs_oldest_to_newest_and_stops_on_none(self):
        seen = []

        def base(o):
            seen.append("base")
            return o

        def old(_o):
            seen.append("old")
            return None

        def new(_o):
            seen.append("new")
            return True

        f = advice.compose(base, [entry("after-while", old, 0), entry("after-while", new, 1)])
        assert await f({}) is None
        # base first, then oldest advice; its None skips the newer one
        assert seen == ["base", "old"]


class TestAfterWhileUntilUseBlueExitAsTruth:
    async def test_after_while_does_not_run_after_a_failing_step_result(self):
        seen = []

        def base(o):
            seen.append("base")
            return {**log(o, "base"), "blue/exit": 7}

        def after(o):
            seen.append("after")
            return {**log(o, "after"), "blue/exit": 0}

        f = advice.compose(base, [entry("after-while", after, 0)])
        ret = await f({})
        assert ret["blue/exit"] == 7
        assert ret["log"] == ["base"]
        assert seen == ["base"]

    async def test_after_until_falls_through_to_advice_after_a_failing_step_result(self):
        seen = []

        def base(o):
            seen.append("base")
            return {**log(o, "base"), "blue/exit": 7}

        def recover(o):
            seen.append("recover")
            return {**log(o, "recover"), "blue/exit": 0}

        f = advice.compose(base, [entry("after-until", recover, 0)])
        ret = await f({})
        assert ret["blue/exit"] == 0
        assert ret["log"] == ["recover"]
        assert seen == ["base", "recover"]


class TestAfterUntilSuppliesAResultWhenTheChainReturnsNone:
    async def test_compose_level_advice_only_fires_when_the_inward_call_is_none(self):
        f = advice.compose(lambda o: None, [entry("after-until", lambda o: log(o, "fallback"), 0)])
        assert (await f({}))["log"] == ["fallback"]
        g = advice.compose(
            lambda o: log(o, "base"), [entry("after-until", lambda o: log(o, "fallback"), 0)]
        )
        # a non-None chain result short-circuits
        assert (await g({}))["log"] == ["base"]

    async def test_in_a_workflow_a_before_while_gate_nils_the_step_out_and_an_outer_after_until_turns_that_into_a_real_result(
        self,
    ):
        wf = single_step_wf()
        wf = advice_add(wf, "t/step", "before-while", "test/gate", lambda o: None)
        wf = advice_add(wf, "t/step", "after-until", "test/fallback", lambda o: log(o, "fallback"))
        res = await run(wf, {})
        assert res["log"] == ["fallback"]
        assert res["blue/exit"] == 0


async def test_after_until_stack_runs_oldest_to_newest():
    seen = []

    def base(_o):
        seen.append("base")
        return None

    def old(_o):
        seen.append("old")
        return None

    def new(_o):
        seen.append("new")
        return "hit"

    f = advice.compose(base, [entry("after-until", old, 0), entry("after-until", new, 1)])
    assert await f({}) == "hit"
    assert seen == ["base", "old", "new"]


# --- depth ----------------------------------------------------------------------


async def test_depth_overrides_add_order():
    wf = single_step_wf()
    wf = advice_add(
        wf, "t/step", "filter-return", "test/outer", lambda o: log(o, "outer"), {"depth": -50}
    )
    wf = advice_add(wf, "t/step", "filter-return", "test/mid", lambda o: log(o, "mid"))
    wf = advice_add(
        wf, "t/step", "filter-return", "test/inner", lambda o: log(o, "inner"), {"depth": 50}
    )
    # lower depth is more outward, higher more inward, whatever the add order
    assert (await run(wf, {}))["log"] == ["base", "inner", "mid", "outer"]


async def test_equal_depth_falls_back_to_newest_outermost():
    wf = single_step_wf()
    wf = advice_add(wf, "t/step", "filter-return", "test/a", lambda o: log(o, "a"), {"depth": -50})
    wf = advice_add(wf, "t/step", "filter-return", "test/b", lambda o: log(o, "b"), {"depth": -50})
    assert (await run(wf, {}))["log"] == ["base", "a", "b"]


async def test_depth_applies_across_per_step_and_all_steps_advice():
    wf = two_step_wf()
    wf = advice_add_all(wf, "filter-return", "test/g", lambda o: log(o, "g"))
    wf = advice_add(wf, "t/a", "filter-return", "test/p", lambda o: log(o, "p"), {"depth": 100})
    res = await run(wf, {})
    # depth 100 pushes the later-added per-step advice inside the global one
    assert res["log"][:3] == ["a", "p", "g"]
    assert res["log"][3:] == ["b", "g"]


def test_invalid_how_and_out_of_range_depth_are_rejected_at_add_time():
    with pytest.raises(AdviceError, match="unsupported advice combinator"):
        advice_add(single_step_wf(), "t/step", "befor", "test/typo", lambda o: o)
    with pytest.raises(AdviceError, match=r"depth must be in -100\.\.100"):
        advice_add(single_step_wf(), "t/step", "before", "test/deep", lambda o: o, {"depth": 101})


# --- cross-workflow inheritance (advice through workflow.step) -------------------


def embed(sub):
    """A parent workflow that runs p/a, then `sub` as an embedded step, then p/z."""
    return workflow(
        start="p/a",
        wire_fn=lambda s, _: {
            "p/a": (lambda o: log(o, "p-a"), "p/sub"),
            "p/sub": (step(sub), "p/z"),
            "p/z": (lambda o: log(o, "p-z"),),
        }.get(s),
    )


async def test_parent_advice_reaches_an_embedded_step():
    sub = single_step_wf()
    parent = advice_add(embed(sub), "t/step", "filter-return", "test/p", lambda o: log(o, "p"))
    assert (await run(parent, {}))["log"] == ["p-a", "base", "p", "p-z"]
    # the child value is untouched — standalone runs stay bare
    assert (await run(sub, {}))["log"] == ["base"]


async def test_parent_override_redefines_an_embedded_step():
    parent = advice_add(
        embed(single_step_wf()), "t/step", "override", "test/o", lambda o: log(o, "redefined")
    )
    assert (await run(parent, {}))["log"] == ["p-a", "redefined", "p-z"]


async def test_inherited_advice_is_outermost():
    sub = advice_add(single_step_wf(), "t/step", "filter-return", "test/c", lambda o: log(o, "child"))
    parent = advice_add(embed(sub), "t/step", "filter-return", "test/p", lambda o: log(o, "parent"))
    assert (await run(parent, {}))["log"] == ["p-a", "base", "child", "parent", "p-z"]


async def test_same_id_parent_advice_replaces_the_childs():
    sub = advice_add(
        single_step_wf(), "t/step", "filter-return", "test/x", lambda o: log(o, "child-x")
    )
    parent = advice_add(embed(sub), "t/step", "filter-return", "test/x", lambda o: log(o, "parent-x"))
    assert (await run(parent, {}))["log"] == ["p-a", "base", "parent-x", "p-z"]


async def test_depth_overrides_inheritance_order():
    sub = advice_add(
        single_step_wf(),
        "t/step",
        "filter-return",
        "test/c",
        lambda o: log(o, "child"),
        {"depth": -50},
    )
    parent = advice_add(embed(sub), "t/step", "filter-return", "test/p", lambda o: log(o, "parent"))
    # the child's depth -50 stays outside the parent's depth-0 advice
    assert (await run(parent, {}))["log"] == ["p-a", "base", "parent", "child", "p-z"]


async def test_advice_add_all_propagates_into_embeds():
    parent = advice_add_all(embed(single_step_wf()), "filter-return", "test/g", lambda o: log(o, "g"))
    # every inner step is wrapped, and so is the embed step itself
    assert (await run(parent, {}))["log"] == ["p-a", "g", "base", "g", "g", "p-z", "g"]


async def test_inheritance_survives_a_scoping_in_fn():
    parent = advice_add(
        workflow(
            start="p/sub",
            wire_fn=lambda s, _: (step(single_step_wf(), in_=lambda o: {"n": o["n"]}),),
        ),
        "t/step",
        "filter-return",
        "test/p",
        lambda o: log(o, "p"),
    )
    # in_ built sub-opts from scratch; the engine re-stamped the registry
    assert (await run(parent, {"n": 1}))["log"] == ["base", "p"]


async def test_inheritance_is_transitive_through_nested_embeds():
    inner = single_step_wf()
    mid = workflow(start="m/sub", wire_fn=lambda s, _: (step(inner),))
    top = advice_add(
        workflow(start="top/sub", wire_fn=lambda s, _: (step(mid),)),
        "t/step",
        "filter-return",
        "test/p",
        lambda o: log(o, "top"),
    )
    assert (await run(top, {}))["log"] == ["base", "top"]


async def test_the_inherited_registry_does_not_leak_into_results():
    parent = advice_add(embed(single_step_wf()), "t/step", "filter-return", "test/p", lambda o: o)
    res = await run(parent, {})
    assert "blue.workflow/inherited" not in res


def test_advice_plan_shows_the_cross_workflow_stack():
    sub = single_step_wf()
    sub = advice_add(sub, "t/step", "before", "test/backend", lambda o: o)
    sub = advice_add(sub, "t/step", "filter-return", "test/inner", lambda o: o, {"depth": 50})
    parent = embed(sub)
    parent = advice_add(parent, "t/step", "before", "test/backend", lambda o: o)

    async def audit(f, o):
        return await f(o)

    parent = advice_add_all(parent, "around", "test/audit", audit)
    # outermost first, with provenance; the parent's backend replaced the child's
    assert [(e["id"], e["scope"], e["level"]) for e in advice_plan([parent, sub], "t/step")] == [
        ("test/audit", "all", 0),
        ("test/backend", "step", 0),
        ("test/inner", "step", 1),
    ]
    # a single workflow works too
    assert [(e["id"], e["scope"], e["level"]) for e in advice_plan(sub, "t/step")] == [
        ("test/backend", "step", 0),
        ("test/inner", "step", 0),
    ]


async def test_advice_add_all_workflow_is_untouched_by_later_adds():
    base = two_step_wf()
    g1 = advice_add_all(base, "filter-return", "test/g1", lambda o: log(o, "g1"))
    g2 = advice_add_all(g1, "filter-return", "test/g2", lambda o: log(o, "g2"))
    # g1 is untouched by adding g2
    assert (await run(g1, {}))["log"] == ["a", "g1", "b", "g1"]
    assert (await run(g2, {}))["log"] == ["a", "g1", "g2", "b", "g1", "g2"]
