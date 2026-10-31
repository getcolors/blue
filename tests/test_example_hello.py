# Keeps examples/hello working: the launcher is loaded in-process for the
# workflow behaviour, and executed as a script for the exit-code contract.
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "hello"
LAUNCHER = EXAMPLE / "blue"


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("hello_launcher", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("hello_launcher", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def hello(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("COLORS_PAR_"):
            monkeypatch.delenv(name)
    state = tmp_path / "blue.yml"
    shutil.copy(EXAMPLE / "blue.yml", state)
    module = load_launcher()
    module.state = state
    return module


async def test_create_writes_one_file_per_name_and_joins(hello, tmp_path, capsys):
    result = await hello.run("create", "-f", str(hello.state))
    assert result["blue/exit"] == 0
    system = subprocess.run(["uname", "-s"], capture_output=True, text=True).stdout.strip()
    work = tmp_path / "work"
    assert sorted(p.name for p in work.iterdir()) == ["alice.txt", "bob.txt", "carol.txt"]
    assert (work / "bob.txt").read_text() == f"hello bob from {system}\n"
    assert result["blue/step"] == "hello/report"
    assert len(result["blue/branches"]) == 3
    assert result["hello/report"] == "".join(f"hello {n} from {system}\n" for n in ["alice", "bob", "carol"])
    output = capsys.readouterr()
    assert output.out == result["hello/report"]
    assert output.err.startswith(">>> hello/system (create)")
    assert output.err.count(">>> hello/greet (create)") == 3
    assert output.err.count(">>> hello/verify (create)") == 3


async def test_delete_removes_files_and_empty_workdir(hello, tmp_path):
    assert (await hello.run("create", "-f", str(hello.state)))["blue/exit"] == 0
    result = await hello.run("delete", "-f", str(hello.state))
    assert result["blue/exit"] == 0
    assert result["hello/report"] == "removed 3 files"
    assert not (tmp_path / "work").exists()


async def test_colors_par_overlay_overrides_workdir(hello, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("COLORS_PAR_WORKDIR", str(elsewhere))
    assert (await hello.run("create", "-f", str(hello.state)))["blue/exit"] == 0
    assert (elsewhere / "alice.txt").exists()
    assert not (tmp_path / "work").exists()


async def test_failing_cli_stops_before_the_fan_out(hello, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
    result = await hello.run("create", "-f", str(hello.state))
    assert result["blue/exit"] == 127
    assert result["blue/err"].startswith("uname failed:")
    assert not (tmp_path / "work").exists()


async def test_unknown_event_is_a_usage_error(hello):
    result = await hello.run("upgrade", "-f", str(hello.state))
    assert result["blue/exit"] == 2


def test_launcher_script_exit_codes(tmp_path):
    state = tmp_path / "blue.yml"
    shutil.copy(EXAMPLE / "blue.yml", state)
    env = {k: v for k, v in os.environ.items() if not k.startswith("COLORS_PAR_")}
    ok = subprocess.run([sys.executable, str(LAUNCHER), "create", "-f", str(state)],
                        capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr
    assert "hello carol from" in ok.stdout
    usage = subprocess.run([sys.executable, str(LAUNCHER)], capture_output=True, text=True, env=env)
    assert usage.returncode == 2
    assert "event" in usage.stderr


async def test_invalid_names_fail_before_writing(hello, tmp_path):
    hello.state.write_text("names: [../escape]\n")
    result = await hello.run("create", "-f", str(hello.state))
    assert result["blue/exit"] == 2
    assert not (tmp_path / "work").exists()


async def test_subworkflow_failure_prevents_report(hello, tmp_path):
    from blue.workflow import advice_add

    hello.wf = advice_add(hello.wf, "hello/verify", "override", "test/fail",
                          lambda opts: {**opts, "blue/exit": 7, "blue/err": "verification failed"})
    result = await hello.run("create", "-f", str(hello.state))
    assert result["blue/exit"] == 7
    assert result["blue/err"] == "verification failed"
    assert "hello/report" not in result


@pytest.mark.parametrize("content", ["[unclosed", "[alice, bob]", "names: [alice, alice]", "names: []"])
async def test_bad_configuration_is_a_usage_error(hello, tmp_path, content):
    hello.state.write_text(content)
    result = await hello.run("create", "-f", str(hello.state))
    assert result["blue/exit"] == 2
    assert not (tmp_path / "work").exists()


async def test_single_name_needs_no_fork_to_create_and_delete(hello, tmp_path):
    hello.state.write_text("names: [alice]\n")
    created = await hello.run("create", "-f", str(hello.state))
    assert created["blue/exit"] == 0
    assert (tmp_path / "work" / "alice.txt").is_file()
    assert "hello alice from" in created["hello/report"]
    deleted = await hello.run("delete", "-f", str(hello.state))
    assert deleted["blue/exit"] == 0
    assert deleted["hello/report"] == "removed 1 files"
    assert not (tmp_path / "work").exists()
