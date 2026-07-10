# Opt-in real floci end-to-end test. This is intentionally skipped by default
# because it needs Linux Docker bridge networking, a running floci container,
# tofu, AWS CLI, Ansible, and outbound network from instance containers.
import importlib.util
import os
import shutil
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUE_FLOCI_E2E") != "1", reason="set BLUE_FLOCI_E2E=1 to run"
)

EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "floci-zookeeper"
WORK_DIR = EXAMPLE_DIR / "work"


def load_launcher():
    # the ./blue launcher has no .py suffix; load it as a module explicitly
    loader = SourceFileLoader("floci_blue_launcher", str(EXAMPLE_DIR / "blue"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


async def checked(launcher, label, *args):
    res = await launcher.run(*args)
    assert res.get("blue/err") is None, f"{label} err: {res.get('blue/err')}"
    assert res["blue/exit"] == 0, f"{label} exit"
    return res


async def test_create_is_idempotent_and_delete_tears_down():
    launcher = load_launcher()
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    try:
        created = await checked(launcher, "create", "create")
        assert len(created["zk/health"]) == 3

        again = await checked(launcher, "second create", "create")
        assert len(again["zk/health"]) == 3
    finally:
        await checked(launcher, "delete", "delete")
