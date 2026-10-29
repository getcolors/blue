import blue
from blue.cli import find_up
from blue.lifecycle import preflight


async def test_package_conventions_are_reusable(tmp_path):
    state = tmp_path / "colors.yml"
    state.write_text("x: 1\n")
    (tmp_path / "deep").mkdir()
    assert find_up("colors.yml", str(tmp_path / "deep")) == str(state)
    assert find_up("absent-colors.yml", str(tmp_path / "deep")) is None
    assert await preflight({"blue/event": "build"}, defaults={"x": 1}, env={}) == {"blue/event": "build", "x": 1, "blue/exit": 0}


async def test_preflight_collects_validator_errors_and_skips_callback():
    called = []
    result = await preflight(
        {"blue/event": "create", "blue/dry-run": True},
        validators=[lambda opts, env, ctx: ["a missing"], lambda opts, env, ctx: None,
                    lambda opts, env, ctx: [f"real={ctx['real']}"]],
        after_validate=lambda *a: called.append(a),
        env={},
    )
    assert result["blue/exit"] == 2
    assert result["blue/err"] == "a missing\nreal=False"
    assert called == []


async def test_preflight_overlay_and_async_callback_see_environment():
    async def after(opts, env, ctx):
        return {**opts, "seen": (env["X"], ctx["event"], ctx["real"]), "blue/exit": 0}

    result = await preflight(
        {"blue/event": "delete"},
        overlay=lambda opts, env: {**opts, "token": env["X"]},
        after_validate=after,
        env={"X": "secret"},
    )
    assert result == {"blue/event": "delete", "token": "secret", "seen": ("secret", "delete", True), "blue/exit": 0}


def test_public_api_is_the_reduced_core():
    assert set(blue.__all__) == {
        "advice", "dry_run", "lifecycle", "progress",
        "USAGE", "exec_cli", "find_up", "load_yaml", "par_name", "read_pars", "run_cli",
        "runtime", "Opts", "StepError", "Workflow",
        "advice_add", "advice_add_all", "advice_plan", "advice_remove", "advice_remove_all",
        "failed", "run", "step", "workflow",
    }
    for name in blue.__all__:
        assert hasattr(blue, name)
