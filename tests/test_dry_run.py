import pytest

from blue import dry_run
from blue.runtime import runtime
from blue.workflow import advice_remove, run, workflow

original_log = runtime.log


@pytest.fixture(autouse=True)
def restore_log():
    yield
    runtime.log = original_log


def capture_log():
    lines = []
    runtime.log = lambda *args: lines.append(" ".join(str(a) for a in args))
    return lines


def effect_wf(effects):
    def step(k):
        def f(o):
            effects.append(k)
            return o

        return f

    return dry_run.advise(
        workflow(
            start="t/a",
            wire_fn=lambda s, _: {
                "t/a": (step("a"), "t/b"),
                "t/b": (step("b"),),
            }.get(s),
        ),
        ["t/a", "t/b"],
    )


async def test_dry_run_skips_advised_steps():
    effects = []
    out = capture_log()
    res = await run(effect_wf(effects), {"blue/event": "create", "blue/dry-run": True})
    assert res["blue/exit"] == 0
    # no side effects ran
    assert effects == []
    assert any("would run t/a" in line for line in out)
    assert any("would run t/b" in line for line in out)


async def test_without_the_flag_steps_run_normally():
    effects = []
    res = await run(effect_wf(effects), {"blue/event": "create"})
    assert res["blue/exit"] == 0
    assert effects == ["a", "b"]


async def test_dry_run_advice_is_removable_per_step():
    effects = []
    w = advice_remove(effect_wf(effects), "t/b", dry_run.SKIP_ID)
    capture_log()
    await run(w, {"blue/event": "create", "blue/dry-run": True})
    # t/a skipped, t/b ran
    assert effects == ["b"]
