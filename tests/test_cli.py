from blue.cli import load_yaml, par_name, read_pars


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


def test_namespaced_keys_and_yaml_booleans():
    assert load_yaml("zk/workdir: work\nanswer: no") == {"zk/workdir": "work", "answer": "no"}
