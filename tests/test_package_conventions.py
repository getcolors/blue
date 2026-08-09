from blue.cli import find_up, stage_dir
from blue.lifecycle import preflight
from blue.process import posix_quote, run_plan
from blue.providers import tool_env
from blue.runtime import ExecResult
from blue.tofu import conventional_backend_advice


async def test_package_conventions_are_reusable(tmp_path):
    state = tmp_path / "colors.yml"
    state.write_text("x: 1\n")
    assert find_up("colors.yml", str(tmp_path / "deep")) == str(state)
    assert stage_dir({"blue/state-file": str(state), "profile": "demo"}, "tool") == str(tmp_path / ".colors/demo/tool")
    assert await preflight({"blue/event": "build"}, defaults={"x": 1}, env={}) == {"blue/event": "build", "x": 1, "blue/exit": 0}
    registry = {"provider": {"x": {"tofu-env": {"token": "TOKEN"}}}}
    assert tool_env(registry, {"provider": "x", "token": "secret"}, ["provider"]) == {"TOKEN": "secret"}
    assert posix_quote("a'b") == "'a'\\''b'"
    result = await run_plan([{"label": "ignored", "args": []}], runner=lambda _: ExecResult(1, "", "gone"), continue_on_error=lambda *_: True)
    assert result["exit"] == 0
    assert callable(conventional_backend_advice(dir=lambda _: str(tmp_path), key=lambda _: "demo/tool.tfstate"))
