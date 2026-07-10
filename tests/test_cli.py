import itertools

from blue.cli import run_cli
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
