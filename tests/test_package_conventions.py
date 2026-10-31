import blue


def test_public_api_is_the_reduced_core():
    assert set(blue.__all__) == {
        "advice",
        "load_yaml", "par_name", "read_pars",
        "Opts", "StepError", "Workflow",
        "advice_add", "advice_add_all", "advice_plan", "advice_remove", "advice_remove_all",
        "failed", "run", "step", "workflow",
    }
    for name in blue.__all__:
        assert hasattr(blue, name)
