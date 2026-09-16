import asyncio
import copy
import json
from pathlib import Path
import pytest
from blue import kubernetes as k
from blue.kubernetes_client import kubectl_client, request

DESCRIPTOR = dict(group="colors.test", version="v1", plural="examples", kind="Example")


class Fixture:
    def __init__(self):
        self.r = dict(
            apiVersion="colors.test/v1",
            kind="Example",
            metadata=dict(
                name="demo",
                namespace="dev",
                uid="demo",
                generation=1,
                resourceVersion="1",
            ),
            spec=dict(
                state="running", reconcileInterval="1s", config=dict(profile="demo")
            ),
        )
        self.now, self.actual, self.effects = 100000, None, []
        self.client = dict(
            list_resources=lambda *_: [self.r],
            get_resource=lambda *_: self.r,
            update_status=lambda _, r, s: self.update(r, status=s),
            update_finalizers=lambda _, r, f: self.update(
                r, metadata={**r["metadata"], "finalizers": f}
            ),
        )
        self.p = dict(
            resource=DESCRIPTOR,
            identity=lambda o: ["backend", o["profile"]],
            validate=lambda o: ["SECRET"] if o.get("invalid") else [],
            observe=lambda o: dict(
                exists=self.actual is not None,
                matches=self.actual == o["blue/event"],
                ready=True,
            ),
            converge=self.converge,
            delete=self.delete,
        )
        self.cfg = k.controller(
            packages=[self.p], client=self.client, now=lambda: self.now, poll_ms=2
        )

    def update(self, r, **patch):
        if r["metadata"]["resourceVersion"] != self.r["metadata"]["resourceVersion"]:
            error = RuntimeError("SECRET conflict")
            error.status = 409
            raise error
        self.r = {
            **self.r,
            **patch,
            "metadata": {
                **patch.get("metadata", self.r["metadata"]),
                "resourceVersion": str(int(self.r["metadata"]["resourceVersion"]) + 1),
            },
        }
        return self.r

    def edit(self, **config):
        self.r = copy.deepcopy(self.r)
        self.r["metadata"]["generation"] += 1
        self.r["metadata"]["resourceVersion"] = str(
            int(self.r["metadata"]["resourceVersion"]) + 1
        )
        self.r["spec"]["config"].update(config)

    def converge(self, o):
        self.effects.append(o["blue/event"])
        self.actual = o["blue/event"]
        return {}

    def delete(self, _):
        self.effects.append("delete")
        self.actual = None
        return {}

    async def run(self):
        return await k.reconcile(self.cfg, self.p, "dev", "demo")


async def test_status_records_the_identity_used_to_acquire_the_lock():
    f = Fixture()
    identities = iter([
        dict(backend="s3", profile="demo"),
        dict(profile="demo", backend="s3"),
    ])
    f.p["identity"] = lambda _: next(identities)
    await f.run()
    assert f.r["status"]["phase"] == "Ready"
    assert f.r["status"]["stateIdentity"] == (
        "b31cd38f9fcf4d1acda385fd2fbef1167dc22a1d8945ac71aa45a974aae6b144"
    )


async def test_explicit_empty_profile_reaches_package_validation():
    f = Fixture()
    f.r["spec"]["config"]["profile"] = ""
    f.p["validate"] = lambda o: ["profile is empty"] if o["profile"] == "" else []
    await f.run()
    assert f.r["status"]["phase"] == "Invalid"
    assert f.effects == []


async def test_convergence_drift_state_suspend_request():
    f = Fixture()
    await f.run()
    assert f.r["status"]["phase"] == "Ready"
    assert (
        f.r["status"]["stateIdentity"]
        == "dedb8c5ac00ab11f7a5322e9b72801c297052fb02724949dad6c19fbc28fd4ac"
    )
    assert f.r["metadata"]["finalizers"] == [k.finalizer]
    await f.run()
    assert f.effects == ["create"]
    f.actual, f.now = None, f.now + 1001
    await f.run()
    f.edit()
    f.r["spec"]["state"] = "stopped"
    await f.run()
    assert f.effects == ["create", "create", "stop"]
    f.edit()
    f.r["spec"]["suspend"] = True
    await f.run()
    f.now += 1001
    f.actual = None
    await f.run()
    assert f.r["status"]["phase"] == "Suspended"
    assert len(f.effects) == 3
    f.edit()
    f.r["spec"]["suspend"] = False
    await f.run()
    f.actual = None
    f.r["metadata"]["annotations"] = {k.request_annotation: "now"}
    await f.run()
    assert len(f.effects) == 5


async def test_validation_immutable_identity_and_safe_backoff():
    f = Fixture()
    f.edit(invalid=True)
    await f.run()
    assert f.r["status"]["phase"] == "Invalid"
    assert "SECRET" not in json.dumps(f.r["status"])
    f.edit(invalid=False)
    await f.run()
    identity = f.r["status"]["stateIdentity"]
    f.edit(profile="other")
    await f.run()
    assert f.r["status"]["profile"] == "demo"
    assert f.r["status"]["stateIdentity"] == identity
    g = Fixture()

    def fail(_):
        raise ValueError("SECRET")

    g.p["validate"] = fail
    await g.run()
    assert g.r["status"]["retryCount"] == 1
    g.now += 1001
    await g.run()
    assert g.r["status"]["retryCount"] == 2
    g.edit(value=1)
    await g.run()
    assert g.r["status"]["retryCount"] == 1
    assert "SECRET" not in json.dumps(g.r["status"])


async def test_matching_but_unready_never_reapplies():
    f = Fixture()
    f.p["observe"] = lambda _: dict(exists=True, matches=True, ready=False)
    await f.run()
    f.now += 1001
    await f.run()
    assert f.r["status"]["retryCount"] == 2
    assert not f.effects


async def test_destroy_protection_incomplete_deletion_and_retain():
    f = Fixture()
    await f.run()
    f.edit(**{"compute-prevent-destroy": True})
    f.r["metadata"]["deletionTimestamp"] = "now"
    f.r["spec"].update(deletionPolicy="Destroy", suspend=True)
    await f.run()
    assert f.r["status"]["phase"] == "Blocked"
    f.edit(**{"compute-prevent-destroy": False})
    f.p["delete"] = lambda _: {}
    await f.run()
    assert f.r["status"]["phase"] == "Failed"
    assert k.finalizer in f.r["metadata"]["finalizers"]
    f.p["delete"] = f.delete
    f.now += 1001
    await f.run()
    assert not f.r["metadata"]["finalizers"]
    g = Fixture()
    await g.run()
    g.edit(invalid=True)
    g.r["metadata"].update(deletionTimestamp="now", finalizers=[k.finalizer, "other"])
    await g.run()
    assert g.r["metadata"]["finalizers"] == ["other"]
    assert g.effects == ["create"]


@pytest.mark.parametrize("method", ["update_status", "update_finalizers"])
async def test_conflicts_prevent_effects_and_release_lock(method):
    f = Fixture()
    original = f.client[method]

    def conflict(*_):
        error = RuntimeError("conflict")
        error.status = 409
        raise error

    f.client[method] = conflict
    with pytest.raises(RuntimeError):
        await f.run()
    assert not f.effects
    f.client[method] = original
    await f.run()
    assert f.r["status"]["phase"] == "Ready"


async def test_stale_completion_cannot_acknowledge_generation():
    f = Fixture()

    def converge(o):
        f.edit(value=1)
        return f.converge(o)

    f.p["converge"] = converge
    await f.run()
    assert f.r["metadata"]["generation"] == 2
    assert f.r["status"]["observedGeneration"] == 1
    assert f.r["status"]["phase"] == "Reconciling"


async def test_locks_span_instances_and_shutdown_drains_callbacks():
    f = Fixture()
    entered, release = asyncio.Event(), asyncio.Event()

    async def converge(o):
        entered.set()
        await release.wait()
        return f.converge(o)

    f.p["converge"] = converge
    rt = k.start(f.cfg)
    await asyncio.wait_for(entered.wait(), 2)
    assert await f.run() == "busy"
    done = asyncio.create_task(k.stop(rt))
    await asyncio.sleep(0.01)
    assert not done.done()
    release.set()
    await asyncio.wait_for(done, 2)
    assert f.effects == ["create"]


async def test_transport_context_private_input_versions_cleanup():
    with pytest.raises(ValueError):
        kubectl_client({})
    paths, calls = [], []

    def runner(argv, env, timeout):
        assert timeout == 45000
        calls.append(argv)
        if "-f" in argv:
            path = Path(argv[argv.index("-f") + 1])
            paths.append(path)
            assert path.stat().st_mode & 0o777 == 0o600
            assert json.loads(path.read_text())["metadata"]["resourceVersion"] == "1"
        return dict(exit=0, out='{"items":[]}', err="")

    client = kubectl_client(context="test", runner=runner)
    await client["list_resources"](DESCRIPTOR, "dev")
    await client["update_status"](DESCRIPTOR, Fixture().r, dict(phase="Ready"))
    assert all(not p.exists() for p in paths)
    await client["update_finalizers"](DESCRIPTOR, Fixture().r, [])
    assert (
        json.loads(calls[-1][calls[-1].index("-p") + 1])["metadata"]["resourceVersion"]
        == "1"
    )
    with pytest.raises(RuntimeError, match="Kubernetes request failed"):
        await request(
            dict(runner=lambda *_: dict(exit=1, out="SECRET", err="SECRET")), ["get"]
        )


async def test_in_cluster_token_rotation_ipv6():
    def runner(argv, *_):
        cfg = json.loads(Path(argv[argv.index("--kubeconfig") + 1]).read_text())
        assert cfg["clusters"][0]["cluster"]["server"] == "https://[::1]:443"
        assert cfg["users"][0]["user"]["tokenFile"].endswith("/token")
        return dict(exit=0, out="", err="")

    await request(dict(in_cluster=True, service_host="::1", runner=runner), ["get"])


async def test_worker_bound_coalescing_shutdown_and_poll_isolation():
    f = Fixture()
    objects = {}
    for name in ("a", "b", "c", "d"):
        r = copy.deepcopy(f.r)
        r["metadata"].update(name=name, uid=name)
        r["spec"]["config"]["profile"] = name
        objects[name] = r

    def update(r, **patch):
        value = {**r, **patch}
        objects[r["metadata"]["name"]] = value
        return value

    def listing(d, _):
        if d["kind"] == "Broken":
            raise ValueError("SECRET")
        return list(objects.values())

    f.cfg["client"] = dict(
        list_resources=listing,
        get_resource=lambda _, ns, name: objects[name],
        update_status=lambda _, r, s: update(r, status=s),
        update_finalizers=lambda _, r, v: update(
            r, metadata={**r["metadata"], "finalizers": v}
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    active, peak, completed = 0, 0, 0
    done = set()

    async def converge(o):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        done.add(o["profile"])
        active -= 1
        completed += 1
        return {}

    f.p["converge"] = converge
    f.p["observe"] = lambda o: dict(
        exists=o["profile"] in done, matches=o["profile"] in done, ready=True
    )
    f.cfg["packages"] = [{**f.p, "resource": {**DESCRIPTOR, "kind": "Broken"}}, f.p]
    rt = k.start(f.cfg)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.sleep(0.01)
    assert peak == 2
    assert rt["errors"] > 0
    stopping = asyncio.create_task(k.stop(rt))
    await asyncio.sleep(0)
    release.set()
    await stopping
    assert completed == 2


async def test_invalid_controller_and_interval():
    f = Fixture()
    with pytest.raises(ValueError):
        k.controller({**f.cfg, "workers": 0})
    for value in ("0s", "-1s", "1.5s", "5d", 22):
        f.edit()
        f.r["spec"]["reconcileInterval"] = value
        await f.run()
        assert f.r["status"]["phase"] == "Invalid"
    assert not f.effects
