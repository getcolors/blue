import os
import signal
import sys

import pytest

from blue.process import run_inherit
from blue.runtime import ExecResult, runtime
from blue.workflow import run, workflow


@pytest.mark.parametrize("code", [0, 7])
def test_inherited_terminal_preserves_exit_and_output(code, capfd):
    result = run_inherit([
        sys.executable, "-c",
        f"import sys; print('out'); print('err', file=sys.stderr); raise SystemExit({code})",
    ])
    assert result == ExecResult(code, "", "")
    assert capfd.readouterr() == ("out\n", "err\n")


def test_inherited_terminal_uses_mutable_runtime(monkeypatch, tmp_path):
    calls = []
    expected = ExecResult(7, "", "stubbed")

    def stub(args, **options):
        calls.append((args, options))
        return expected

    monkeypatch.setattr(runtime, "exec_inherit", stub)
    args = ["example-command", "arg"]
    env = {"TOKEN": "value", "REMOVED": None}
    assert run_inherit(args, cwd=str(tmp_path), env=env) is expected
    assert calls == [(args, {"cwd": str(tmp_path), "env": env})]


def test_inherited_terminal_applies_working_directory_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUE_TEST_REMOVE", "inherited")
    monkeypatch.setenv("BLUE_TEST_KEEP", "kept")
    monkeypatch.setenv("BLUE_TEST_REPLACE", "old")
    result = run_inherit([
        sys.executable, "-c",
        "import os, pathlib; pathlib.Path('result').write_text('|'.join("
        "os.getenv(k, 'absent') for k in ['BLUE_TEST_REMOVE', 'BLUE_TEST_KEEP', "
        "'BLUE_TEST_REPLACE', 'BLUE_TEST_EMPTY']))",
    ], cwd=str(tmp_path), env={
        "BLUE_TEST_REMOVE": None, "BLUE_TEST_REPLACE": "new", "BLUE_TEST_EMPTY": "",
    })
    assert result.exit == 0
    assert (tmp_path / "result").read_text() == "absent|kept|new|"
    assert os.environ["BLUE_TEST_REMOVE"] == "inherited"


@pytest.mark.parametrize("failure", [
    "missing",
    pytest.param("signal", marks=pytest.mark.skipif(os.name != "posix", reason="POSIX signals")),
])
async def test_inherited_terminal_failure_stops_workflow(failure, tmp_path):
    if failure == "missing":
        args = [str(tmp_path / "nonexistent-command")]
        expected = 127
    else:
        args = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
        expected = 128 + signal.SIGTERM
    seen = []

    def command(opts):
        seen.append("command")
        result = run_inherit(args)
        assert result.exit == expected
        if failure == "missing":
            assert "nonexistent-command" in result.err
        return {**opts, "blue/exit": result.exit, "blue/err": result.err}

    def successor(opts):
        seen.append("successor")
        return dict(opts)

    wf = workflow(start="test/command", wire_fn=lambda name, _: {
        "test/command": (command, "test/successor"),
        "test/successor": (successor,),
    }.get(name))
    result = await run(wf, {})
    assert result["blue/exit"] == expected
    assert seen == ["command"]
