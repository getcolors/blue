from typing_extensions import TypedDict

from blue.gates import schema_gate
from blue.workflow import advice_add, run, workflow

# functional TypedDict syntax carries the namespaced string keys
Server = TypedDict("Server", {"id": int})
GateSchema = TypedDict("GateSchema", {"zk/servers": list[Server]})


def gate_wf(effects):
    def probe(o):
        effects.append("ran")
        return o

    return advice_add(
        workflow(start="t/a", wire_fn=lambda s, _: (probe,)),
        "t/a",
        "before-while",
        "test/schema",
        schema_gate(GateSchema),
    )


async def test_valid_opts_pass_through_the_gate_untouched():
    effects = []
    res = await run(gate_wf(effects), {"zk/servers": [{"id": 1}], "other/key": "survives"})
    assert res["blue/exit"] == 0
    assert effects == ["ran"]
    # the gate validates, never transforms: unrelated namespaces survive
    assert res["other/key"] == "survives"


async def test_invalid_opts_fail_with_exit_2_and_a_readable_message_the_step_never_runs():
    effects = []
    res = await run(gate_wf(effects), {"zk/servers": [{"id": "one"}]})
    assert res["blue/exit"] == 2
    assert "schema gate failed" in res["blue/err"]
    assert "zk/servers" in res["blue/err"]
    assert effects == []
