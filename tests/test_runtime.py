import os
import signal
import sys

import pytest

from blue.runtime import runtime
from blue.scaffold import content_spec
from blue.tofu import tofu_with_spec
from blue.workflow import failed


@pytest.mark.parametrize("code", [0, 7])
async def test_normal_process_exit_is_preserved(code):
    result = await runtime.exec([sys.executable, "-c", f"raise SystemExit({code})"])
    assert result.exit == code


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal return codes")
async def test_signal_is_a_positive_workflow_failure():
    result = await runtime.exec([
        sys.executable, "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    ])
    assert result.exit == 128 + signal.SIGTERM
    assert failed({"blue/exit": result.exit})


async def test_real_timeout_reports_positive_failure():
    result = await runtime.exec([
        sys.executable, "-c",
        "import time; time.sleep(60)",
    ], timeout_ms=500)
    assert result.exit == 124
    assert failed({"blue/exit": result.exit})
    assert "command timed out after 500ms" in result.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable and signals")
async def test_signaled_destroy_retains_scaffolded_files(tmp_path, monkeypatch):
    # Execute a real subprocess through the normal runtime, not an ExecResult
    # stub: init succeeds, then destroy is interrupted before completion.
    binary = tmp_path / "bin" / "tofu"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys\n"
        "if sys.argv[1] == 'destroy':\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "elif sys.argv[1] != 'init':\n"
        "    raise SystemExit(3)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    target = tmp_path / "deployment" / "main.tf"
    result = await tofu_with_spec(
        {"blue/event": "delete"},
        [content_spec(str(target), "# resource configuration\n")],
        dir=str(target.parent),
    )
    assert result["blue/exit"] == 128 + signal.SIGTERM
    assert failed(result)
    assert "destroy failed" in result["blue/err"]
    assert target.read_text() == "# resource configuration\n"


async def test_environment_overrides_can_remove_inherited_values(monkeypatch):
    monkeypatch.setenv("BLUE_TEST_REMOVE", "inherited")
    monkeypatch.setenv("BLUE_TEST_KEEP", "kept")
    monkeypatch.setenv("BLUE_TEST_REPLACE", "old")
    result = await runtime.exec(
        [sys.executable, "-c", "import os; print('|'.join(os.getenv(k, 'absent') for k in "
         "['BLUE_TEST_REMOVE', 'BLUE_TEST_KEEP', 'BLUE_TEST_REPLACE', 'BLUE_TEST_EMPTY']))"],
        env={"BLUE_TEST_REMOVE": None, "BLUE_TEST_REPLACE": "new", "BLUE_TEST_EMPTY": ""},
    )
    assert result.exit == 0
    assert result.out == "absent|kept|new|\n"
    assert os.environ["BLUE_TEST_REMOVE"] == "inherited"


def _child_command(marker, *, detached=False, parent_exits=False):
    child = (
        "import pathlib, time; time.sleep(0.7); "
        f"pathlib.Path({str(marker)!r}).write_text('survived'); time.sleep(30)"
    )
    return [sys.executable, "-c", (
        "import pathlib, subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session={detached!r}); "
        f"pathlib.Path({str(marker.with_suffix('.pid'))!r}).write_text(str(p.pid)); "
        "print('started', flush=True); print('diagnostic', file=sys.stderr, flush=True); "
        + ("" if parent_exits else "time.sleep(30)")
    )]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("parent_exits", [False, True])
async def test_timeout_kills_descendants_even_after_parent_exit(tmp_path, parent_exits):
    import asyncio
    import time

    marker = tmp_path / "child"
    started = time.monotonic()
    try:
        result = await runtime.exec(_child_command(marker, parent_exits=parent_exits), timeout_ms=300)
        elapsed = time.monotonic() - started
        assert result.exit == 124
        assert result.out == "started\n"
        assert result.err == "diagnostic\n\ncommand timed out after 300ms"
        assert elapsed < 1.5
        await asyncio.sleep(0.8)
        assert not marker.exists()
    finally:
        _kill_child(marker)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
async def test_cancellation_kills_descendants(tmp_path):
    import asyncio

    marker = tmp_path / "child"
    task = asyncio.create_task(runtime.exec(_child_command(marker)))
    try:
        async with asyncio.timeout(3):
            while not marker.with_suffix('.pid').exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.8)
        assert not marker.exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _kill_child(marker)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
async def test_timeout_cleanup_is_bounded_when_detached_child_holds_pipes(tmp_path):
    import time

    marker = tmp_path / "child"
    started = time.monotonic()
    try:
        result = await runtime.exec(_child_command(marker, detached=True), timeout_ms=300)
        assert result.exit == 124
        assert result.out == "started\n"
        assert time.monotonic() - started < 2.5
    finally:
        _kill_child(marker)


def _kill_child(marker):
    pid_file = marker.with_suffix('.pid')
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_timeout_preserves_failure_when_output_ends_mid_character():
    result = await runtime.exec([
        sys.executable, "-c",
        "import os, time; os.write(1, bytes([0xe2, 0x82])); "
        "os.write(2, bytes([0xe2, 0x82])); time.sleep(30)",
    ], timeout_ms=300)
    assert result.exit == 124
    assert result.out == "\ufffd"
    assert result.err == "\ufffd\ncommand timed out after 300ms"


async def test_successful_command_preserves_multibyte_output():
    result = await runtime.exec([
        sys.executable, "-c",
        "import os, time; os.write(1, bytes([0xe2])); time.sleep(0.01); "
        "os.write(1, bytes([0x82, 0xac])); os.write(2, bytes([0xe2, 0x82, 0xac]))",
    ])
    assert result.exit == 0
    assert result.out == "€"
    assert result.err == "€"
