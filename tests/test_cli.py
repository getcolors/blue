import itertools

from blue.cli import load_yaml, par_name, read_pars, run_cli
from blue.workflow import workflow

_n = itertools.count()


def state_file(tmp_path, content):
    f = tmp_path / f"state-{next(_n)}.yml"
    f.write_text(content)
    return str(f)


def probe_wf():
    return workflow(
        start="t/a",
        wire_fn=lambda s, _: {
            "t/a": (
                lambda o: {
                    **o,
                    "seen": [*o.get("seen", []), ("a", o["blue/event"], o.get("x"))],
                },
                "t/b",
            ),
            "t/b": (lambda o: {**o, "seen": [*o["seen"], "b"]},),
        }.get(s),
    )


async def test_event_and_state_flow_into_the_workflow(tmp_path):
    res = await run_cli(probe_wf(), ["create", "-f", state_file(tmp_path, "x: 1")])
    assert res["blue/exit"] == 0
    assert res["seen"] == [("a", "create", 1), "b"]


async def test_arbitrary_events_are_allowed(tmp_path):
    res = await run_cli(probe_wf(), ["provision", "-f", state_file(tmp_path, "x: 2")])
    assert res["seen"] == [("a", "provision", 2), "b"]


async def test_slices_via_start_and_end(tmp_path):
    # --end is an inclusive boundary
    ended = await run_cli(
        probe_wf(), ["create", "-f", state_file(tmp_path, "x: 1"), "--end", "t/a"]
    )
    assert ended["seen"] == [("a", "create", 1)]
    # --start skips earlier steps (t/a never ran, so seen starts empty)
    started = await run_cli(
        probe_wf(), ["create", "-f", state_file(tmp_path, "x: 1\nseen: []"), "--start", "t/b"]
    )
    assert started["seen"] == ["b"]


async def test_dry_run_stamps_the_key(tmp_path):
    wf = workflow(
        start="t/a", wire_fn=lambda s, _: (lambda o: {**o, "dry": o.get("blue/dry-run")},)
    )
    assert (await run_cli(wf, ["create", "-f", state_file(tmp_path, "{}"), "--dry-run"]))[
        "dry"
    ] is True
    assert (await run_cli(wf, ["create", "-f", state_file(tmp_path, "{}")]))["dry"] is None


async def test_usage_errors_exit_2(tmp_path):
    # missing event
    no_event = await run_cli(probe_wf(), [])
    assert no_event["blue/exit"] == 2
    assert "Usage" in no_event["blue/err"]
    # missing state file
    no_file = await run_cli(probe_wf(), ["create", "-f", "/nonexistent/blue.yml"])
    assert no_file["blue/exit"] == 2
    assert "not found" in no_file["blue/err"]
    # unknown flag
    bad_flag = await run_cli(probe_wf(), ["create", "--bogus"])
    assert bad_flag["blue/exit"] == 2


def test_colors_par_overlays_flat_keys_with_type_coercion():
    assert par_name("compute-prevent-destroy") == "COLORS_PAR_COMPUTE_PREVENT_DESTROY"
    assert read_pars(
        {"compute-prevent-destroy": True, "port": 1},
        {
            "COLORS_PAR_COMPUTE_PREVENT_DESTROY": "false",
            "COLORS_PAR_PORT": "587",
            "COLORS_PAR_TOKEN": "x",
        },
    ) == {"compute-prevent-destroy": False, "port": 587, "token": "x"}


def test_integers_follow_the_1_2_core_schema():
    # YAML 1.1 would read 012 as octal 10 and 1:30 as sexagesimal 90; both the
    # resolver and the constructor have to be replaced to get base ten.
    loaded = load_yaml("a: 012\nb: 1:30\nc: 0o17\nd: 0x1f\ne: -012\n")
    assert loaded == {"a": 12, "b": "1:30", "c": 15, "d": 31, "e": -12}


def test_no_colour_keeps_a_prefix_of_its_own():
    assert read_pars(
        {},
        {
            "GREEN_PAR_DO_TOKEN": "tok",
            "RED_PAR_DO_TOKEN": "tok",
            "BLUE_PAR_DO_TOKEN": "tok",
            "ONCE_PAR_DO_TOKEN": "tok",
        },
    ) == {}


async def test_namespaced_keys_and_yaml_1_2_semantics_survive_the_load(tmp_path):
    wf = workflow(
        start="t/a",
        wire_fn=lambda s, _: (lambda o: {**o, "probe": [o.get("zk/workdir"), o.get("answer")]},),
    )
    res = await run_cli(
        wf, ["create", "-f", state_file(tmp_path, "zk/workdir: work\nanswer: no")]
    )
    # zk/workdir parses unquoted; `no` is the string "no", not False
    assert res["probe"] == ["work", "no"]


async def test_explicit_file_overrides_configured_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blue.yml").write_text("x: 1")
    (tmp_path / "colors.yml").write_text("x: 2")
    for option in (["-f", "blue.yml"], ["--file", "blue.yml"], ["--file=blue.yml"]):
        result = await run_cli(probe_wf(), ["create", *option], default_file="colors.yml")
        assert result["seen"][0] == ("a", "create", 1)
        assert result["blue/state-file"] == str(tmp_path / "blue.yml")
    result = await run_cli(probe_wf(), ["create"], default_file="colors.yml")
    assert result["seen"][0] == ("a", "create", 2)


async def test_parent_search_only_applies_to_omitted_file(tmp_path, monkeypatch):
    (tmp_path / "blue.yml").write_text("x: 1")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    result = await run_cli(probe_wf(), ["create"], search_parents=True)
    assert result["blue/state-file"] == str(tmp_path / "blue.yml")
    for option in (["-f", "blue.yml"], ["--file", "blue.yml"], ["--file=blue.yml"]):
        result = await run_cli(probe_wf(), ["create", *option], search_parents=True)
        assert result["blue/exit"] == 2
        assert "not found" in result["blue/err"]


def test_core_scalar_numbers_and_strings():
    import math
    import yaml

    loaded = load_yaml('''exponent: 1e3
fractional-exponent: 1.0e3
small-exponent: 1.25e-2
date: 2026-09-11
clock: 1:30.5
octal: 0o17
hex: 0x1f
quoted: ["1e3", "0o17", "0x1f", "2026-09-11"]
nested: {number: &n 0o17, alias: *n}
positive-infinity: .inf
negative-infinity: -.Inf
nan: .NaN
''')
    assert loaded["exponent"] == loaded["fractional-exponent"] == 1000
    assert loaded["small-exponent"] == 0.0125
    assert loaded["date"] == "2026-09-11"
    assert loaded["clock"] == "1:30.5"
    assert loaded["octal"] == 15 and loaded["hex"] == 31
    assert loaded["quoted"] == ["1e3", "0o17", "0x1f", "2026-09-11"]
    assert loaded["nested"] == {"number": 15, "alias": 15}
    assert loaded["positive-infinity"] == math.inf
    assert loaded["negative-infinity"] == -math.inf
    assert math.isnan(loaded["nan"])
    # The SDK's loader must not change PyYAML's process-global safe loader.
    assert yaml.safe_load("value: 1:30.5")["value"] == 90.5
