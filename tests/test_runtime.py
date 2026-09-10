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
