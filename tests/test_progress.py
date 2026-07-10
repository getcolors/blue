import pytest

from blue import progress
from blue.runtime import runtime
from blue.workflow import advice_remove_all, run, workflow

original_log = runtime.log


@pytest.fixture(autouse=True)
def restore_log():
    yield
    runtime.log = original_log


def capture_log():
    lines = []
    runtime.log = lambda *args: lines.append(" ".join(str(a) for a in args))
    return lines


def simple_wf():
    return progress.advise(
        workflow(
            start="t/a",
            wire_fn=lambda s, _: {
                "t/a": (lambda o: o, "t/b"),
                "t/b": (lambda o: o,),
            }.get(s),
        )
    )


async def test_progress_prints_step_names():
    out = capture_log()
    await run(simple_wf(), {"blue/event": "create"})
    text = "\n".join(out)
    assert ">>> t/a (create)" in text
    assert "<<< t/a" in text
    assert ">>> t/b (create)" in text
    assert "<<< t/b" in text
    # elapsed time is printed
    assert "ms)" in text


async def test_progress_prints_the_event_name():
    out = capture_log()
    await run(simple_wf(), {"blue/event": "delete"})
    assert ">>> t/a (delete)" in "\n".join(out)


async def test_progress_is_removable():
    w = advice_remove_all(simple_wf(), progress.PROGRESS_ID)
    out = capture_log()
    await run(w, {"blue/event": "create"})
    assert out == []


async def test_progress_with_forks():
    def next_fn(s, default_next, o):
        if s == "t/start":
            return [("t/join", {**o, "branch": "a"}), ("t/join", {**o, "branch": "b"})]
        return [(n, o) for n in (default_next or [])]

    w = progress.advise(
        workflow(
            start="t/start",
            wire_fn=lambda s, _: {
                "t/start": (lambda o: o, "t/join"),
                "t/join": (lambda o: o,),
            }.get(s),
            next_fn=next_fn,
        )
    )
    out = capture_log()
    await run(w, {"blue/event": "create"})
    text = "\n".join(out)
    assert ">>> t/start" in text
    assert ">>> t/join" in text


async def test_blue_step_is_set_in_opts():
    seen = []

    def probe(o):
        seen.append(o["blue/step"])
        return o

    w = workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (probe, "t/b"),
            "t/b": (probe,),
        }.get(s),
    )
    res = await run(w, {"blue/event": "create"})
    assert seen == ["t/a", "t/b"]
    assert res["blue/exit"] == 0
