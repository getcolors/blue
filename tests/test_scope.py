import asyncio
import pytest
from blue.scope import with_scope

@pytest.mark.asyncio
async def test_order_and_result():
    events = []
    def body(register):
        register("resource", lambda: events.append("agent"))
        register("process", lambda: events.append("first"))
        register("process", lambda: events.append("second"))
        return 42
    assert await with_scope(body) == 42
    assert events == ["second", "first", "agent"]

@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup_even_when_cancelled_again():
    started, cleaning = asyncio.Event(), asyncio.Event()
    events = []
    async def cleanup():
        cleaning.set()
        await asyncio.sleep(.02)
        events.append("process")
    async def body(register):
        register("resource", lambda: events.append("agent"))
        register("process", cleanup)
        started.set()
        await asyncio.Future()
    task = asyncio.create_task(with_scope(body))
    await started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["process", "agent"]

@pytest.mark.asyncio
async def test_body_failure_wins_and_all_finalizers_run():
    events = []
    error = ValueError("body")
    def fail():
        raise RuntimeError("cleanup")
    def body(register):
        register("resource", lambda: events.append("agent"))
        register("process", fail)
        raise error
    with pytest.raises(ValueError) as caught:
        await with_scope(body)
    assert caught.value is error
    assert events == ["agent"]

@pytest.mark.asyncio
async def test_workflow_cancellation_drains_parallel_steps_before_resource_cleanup():
    from blue.workflow import run, workflow
    events = []
    started = asyncio.Event()
    count = 0
    async def child(opts):
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)
            events.append("child")
    wf = workflow(start="t/start", wire_fn=lambda step, opts:
                  (lambda o: o, "t/a", "t/b") if step == "t/start" else (child,))
    async def body(register):
        register("resource", lambda: events.append("agent"))
        return await run(wf, {})
    task = asyncio.create_task(with_scope(body))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["child", "child", "agent"]

@pytest.mark.asyncio
async def test_ansible_excludes_secret_bindings(monkeypatch):
    from blue.ansible import ansible_step
    from blue.runtime import runtime, ExecResult
    seen = {}
    async def capture(cmd, **kwargs):
        seen.update(kwargs["env"])
        return ExecResult(exit=0, out="", err="")
    monkeypatch.setattr(runtime, "exec", capture)
    await ansible_step({}, dir=".", env={"COLORS_PAR_SECRET": "hidden", "CUSTOM_SECRET": "hidden", "SSH_AUTH_SOCK": "/scope/socket"}, secret_env=["CUSTOM_SECRET"])
    assert seen["COLORS_PAR_SECRET"] is None
    assert seen["CUSTOM_SECRET"] is None
    assert seen["SSH_AUTH_SOCK"] == "/scope/socket"
