# Flagship end-to-end test: a fake ZooKeeper cluster. Real template renders,
# real `tofu apply`/`destroy` — but the HCL contains only locals and outputs,
# so nothing real is created and no credentials are needed.
import os
import shutil
from pathlib import Path

import pytest

from blue import tofu
from blue.scaffold import scaffold
from blue.workflow import advice_add, run, step, workflow

pytestmark = pytest.mark.skipif(shutil.which("tofu") is None, reason="tofu not on PATH")

RES = Path(__file__).parent / "resources"

main_tf = (RES / "zk/main.tf").read_text()
zoo_cfg = (RES / "zk/zoo.cfg").read_text()


def node_dir(opts, node):
    return f"{opts['zk/workdir']}/nodes/{node['id']}"


# --- steps ---------------------------------------------------------------------


def start_step(opts):
    return opts


async def node_step(opts):
    """Scaffold one node's main.tf and drive tofu over it. On delete, destroy
    before removing the files tofu still needs."""
    node = opts["zk/node"]
    dir = node_dir(opts, node)
    specs = [
        {
            "template": {"name": "zk/main.tf", "content": main_tf},
            "target": f"{dir}/main.tf",
            "data": {"node": node},
        }
    ]
    if opts.get("blue/event") == "delete":
        out = await tofu.tofu_step(opts, dir=dir)
        return out if (out.get("blue/exit") or 0) > 0 else scaffold(out, specs)
    return await tofu.tofu_step(scaffold(opts, specs), dir=dir)


def zoo_cfg_step(opts):
    """Join step: render zoo.cfg (listing every member) into each server's
    directory. On create the member list comes from the branches' observed
    tofu outputs; on delete the desired state names the targets to remove."""
    nodes = opts["zk/servers"]
    if opts.get("blue/event") == "delete":
        servers = nodes
    else:
        servers = sorted(
            (b["tofu/outputs"] for b in opts["blue/branches"]), key=lambda o: o["id"]
        )
    specs = [
        {
            "template": {"name": "zk/zoo.cfg", "content": zoo_cfg},
            "target": f"{opts['zk/workdir']}/nodes/{n['id']}/zoo.cfg",
            "data": {"servers": servers},
        }
        for n in nodes
    ]
    return scaffold(opts, specs)


# --- workflow --------------------------------------------------------------------


def wire_fn(s, _run_opts):
    return {
        "zk/start": (start_step, "zk/node"),
        "zk/node": (node_step, "zk/zoo-cfg"),
        "zk/zoo-cfg": (zoo_cfg_step,),
    }.get(s)


def next_fn(s, default_next, opts):
    if (opts.get("blue/exit") or 0) > 0:
        return []
    # fan out: one zk/node branch per server, in parallel
    if s == "zk/start":
        return [("zk/node", {**opts, "zk/node": n}) for n in opts["zk/servers"]]
    return [(n, opts) for n in (default_next or [])]


# the backend is not hardwired: the local filesystem backend is injected
# with the advice facility
cluster_wf = advice_add(
    workflow(start="zk/start", wire_fn=wire_fn, next_fn=next_fn),
    "zk/node",
    "before",
    "zk/backend",
    tofu.local_backend_advice(lambda o: node_dir(o, o["zk/node"])),
)

desired_state = {
    "zk/servers": [
        {"id": 1, "name": "zk1", "ip": "10.0.0.1"},
        {"id": 2, "name": "zk2", "ip": "10.0.0.2"},
        {"id": 3, "name": "zk3", "ip": "10.0.0.3"},
    ]
}

# --- composition: two clusters from the same workflow -----------------------------
# `step` turns cluster_wf into an ordinary step; the parent fans it out once
# per cluster (in parallel), scoping each run with `in_`.


def _cluster_in(opts):
    c = opts["zk/cluster"]
    return {
        **opts,
        "zk/servers": c["servers"],
        "zk/workdir": f"{opts['zk/workdir']}/{c['name']}",
    }


def _report_step(opts):
    return {
        **opts,
        "clusters/reported": {b["zk/workdir"] for b in opts["blue/branches"]},
    }


def _clusters_next_fn(s, default_next, opts):
    if (opts.get("blue/exit") or 0) > 0:
        return []
    if s == "clusters/start":
        return [("clusters/cluster", {**opts, "zk/cluster": c}) for c in opts["zk/clusters"]]
    return [(n, opts) for n in (default_next or [])]


two_clusters_wf = workflow(
    start="clusters/start",
    wire_fn=lambda s, _: {
        "clusters/start": (start_step, "clusters/cluster"),
        "clusters/cluster": (step(cluster_wf, in_=_cluster_in), "clusters/report"),
        "clusters/report": (_report_step,),
    }.get(s),
    next_fn=_clusters_next_fn,
)

two_clusters_state = {
    "zk/clusters": [
        {
            "name": "alpha",
            "servers": [
                {"id": 1, "name": "zk1", "ip": "10.0.1.1"},
                {"id": 2, "name": "zk2", "ip": "10.0.1.2"},
            ],
        },
        {
            "name": "beta",
            "servers": [
                {"id": 1, "name": "zk1", "ip": "10.0.2.1"},
                {"id": 2, "name": "zk2", "ip": "10.0.2.2"},
            ],
        },
    ]
}


async def test_fake_cluster_create_fans_out_joins_is_idempotent_delete_tears_down(tmp_path):
    state = {**desired_state, "zk/workdir": str(tmp_path)}
    work = state["zk/workdir"]

    # create: fan-out, parallel tofu applies, join renders zoo.cfg
    created = await run(cluster_wf, {**state, "blue/event": "create"})
    assert created.get("blue/err") is None
    assert created["blue/exit"] == 0
    assert len(created["blue/branches"]) == 3
    # observed outputs flowed back into each branch
    assert {b["tofu/outputs"]["name"] for b in created["blue/branches"]} == {"zk1", "zk2", "zk3"}
    for server in state["zk/servers"]:
        dir = f"{work}/nodes/{server['id']}"
        assert os.path.exists(f"{dir}/main.tf")
        # advice wrote the backend
        assert os.path.exists(f"{dir}/backend.tf.json")
        # local backend state
        assert os.path.exists(f"{dir}/terraform.tfstate")
        cfg = Path(f"{dir}/zoo.cfg").read_text()
        for member in state["zk/servers"]:
            # every zoo.cfg lists every member
            assert f"server.{member['id']}={member['ip']}:2888:3888" in cfg

    # create is idempotent
    again = await run(cluster_wf, {**state, "blue/event": "create"})
    assert again["blue/exit"] == 0

    # delete: destroys each node and removes the generated files
    deleted = await run(cluster_wf, {**state, "blue/event": "delete"})
    assert deleted.get("blue/err") is None
    assert deleted["blue/exit"] == 0
    for server in state["zk/servers"]:
        assert not os.path.exists(f"{work}/nodes/{server['id']}/main.tf")
        assert not os.path.exists(f"{work}/nodes/{server['id']}/zoo.cfg")


async def test_two_clusters_from_one_workflow_composed_and_isolated(tmp_path):
    work = str(tmp_path)
    state = {**two_clusters_state, "zk/workdir": work}

    # create: the cluster workflow runs twice, in parallel
    created = await run(two_clusters_wf, {**state, "blue/event": "create"})
    assert created.get("blue/err") is None
    assert created["blue/exit"] == 0
    assert len(created["blue/branches"]) == 2
    assert created["clusters/reported"] == {f"{work}/alpha", f"{work}/beta"}
    for cluster in state["zk/clusters"]:
        for server in cluster["servers"]:
            dir = f"{work}/{cluster['name']}/nodes/{server['id']}"
            assert os.path.exists(f"{dir}/main.tf")
            assert os.path.exists(f"{dir}/terraform.tfstate")
            cfg = Path(f"{dir}/zoo.cfg").read_text()
            for member in cluster["servers"]:
                # zoo.cfg lists this cluster's members
                assert f"server.{member['id']}={member['ip']}:2888:3888" in cfg
    # clusters stay isolated: alpha's zoo.cfg must not list beta's nodes
    assert "10.0.2." not in Path(f"{work}/alpha/nodes/1/zoo.cfg").read_text()

    # delete tears down both clusters
    deleted = await run(two_clusters_wf, {**state, "blue/event": "delete"})
    assert deleted.get("blue/err") is None
    assert deleted["blue/exit"] == 0
    for cluster in state["zk/clusters"]:
        for server in cluster["servers"]:
            assert not os.path.exists(f"{work}/{cluster['name']}/nodes/{server['id']}/main.tf")
            assert not os.path.exists(f"{work}/{cluster['name']}/nodes/{server['id']}/zoo.cfg")
