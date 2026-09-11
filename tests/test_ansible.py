# Playbook selection, recap parsing, inventory rendering, and
# ansible_with_spec scaffolding need no ansible binary — only `ansible_step`
# itself shells out, and these tests stub the runtime exec seam.
import os
from pathlib import Path

import pytest

from blue.ansible import (
    ansible_step,
    ansible_with_spec,
    inventory_advice,
    inventory_ini,
    parse_recap,
    playbook,
)
from blue.runtime import ExecResult, runtime

RES = Path(__file__).parent / "resources"

create_yml = (RES / "bluetest/create.yml").read_text()
ansible_cfg = (RES / "bluetest/ansible.cfg").read_text()

original_exec = runtime.exec


@pytest.fixture(autouse=True)
def restore_exec():
    yield
    runtime.exec = original_exec


def test_playbook_follows_the_event():
    assert playbook({"blue/event": "create"}) == "create.yml"
    # any non-delete event provisions
    assert playbook({"blue/event": "rotate"}) == "create.yml"
    assert playbook({"blue/event": "delete"}) == "delete.yml"
    # partial overrides keep the other default
    assert playbook({"blue/event": "delete"}, {"delete": "teardown.yml"}) == "teardown.yml"
    assert playbook({"blue/event": "create"}, {"delete": "teardown.yml"}) == "create.yml"


def test_recap_parses_per_host_counters():
    out = (
        "PLAY RECAP *********************************************\n"
        "zk1                        : ok=7    changed=4    unreachable=0    failed=0"
        "    skipped=1    rescued=0    ignored=0\n"
        "zk2                        : ok=7    changed=0    unreachable=0    failed=1"
        "    skipped=1    rescued=0    ignored=0\n"
    )
    assert parse_recap(out) == {
        "zk1": {
            "ok": 7,
            "changed": 4,
            "unreachable": 0,
            "failed": 0,
            "skipped": 1,
            "rescued": 0,
            "ignored": 0,
        },
        "zk2": {
            "ok": 7,
            "changed": 0,
            "unreachable": 0,
            "failed": 1,
            "skipped": 1,
            "rescued": 0,
            "ignored": 0,
        },
    }
    assert parse_recap("no recap here") == {}


def test_inventory_ini_renders_groups_hosts_and_vars():
    assert inventory_ini(
        {
            "zookeeper": {
                "hosts": [
                    {"name": "zk2", "vars": {"zk_id": 2, "ansible_host": "10.0.0.2"}},
                    {"name": "zk1", "vars": {"ansible_host": "10.0.0.1", "zk_id": 1}},
                ],
                "vars": {
                    "ansible_user": "root",
                    "ansible_python_interpreter": "/usr/bin/python3",
                },
            }
        }
    ) == (
        "[zookeeper]\n"
        "zk1 ansible_host=10.0.0.1 zk_id=1\n"
        "zk2 ansible_host=10.0.0.2 zk_id=2\n"
        "\n"
        "[zookeeper:vars]\n"
        "ansible_python_interpreter=/usr/bin/python3\n"
        "ansible_user=root\n"
    )


def test_inventory_ini_without_group_vars():
    assert inventory_ini({"web": {"hosts": [{"name": "w1"}]}}) == "[web]\nw1\n"


def test_inventory_advice_writes_the_file(tmp_path):
    file = f"{tmp_path}/hosts/inventory.ini"
    advice = inventory_advice(
        lambda _o: file,
        lambda opts: {
            "zookeeper": {
                "hosts": [
                    {"name": s["name"], "vars": {"ansible_host": s["ip"]}}
                    for s in opts["servers"]
                ]
            }
        },
    )
    opts = {"servers": [{"name": "zk1", "ip": "10.0.0.1"}]}
    # before-advice passes opts through
    assert advice(opts) == opts
    assert Path(file).read_text() == "[zookeeper]\nzk1 ansible_host=10.0.0.1\n"


fake_recap = (
    "PLAY RECAP *********************************************\n"
    "localhost                  : ok=1    changed=0    unreachable=0"
    "    failed=0    skipped=0    rescued=0    ignored=0\n"
)


def stub_ansible_exec():
    calls = []

    async def fake(cmd, cwd=None, env=None):
        if cmd[0] == "ansible-playbook":
            calls.append(cmd)
            return ExecResult(exit=0, out=fake_recap, err="")
        return await original_exec(cmd, cwd=cwd, env=env)

    runtime.exec = fake
    return calls


def specs(dir):
    return [
        {
            "template": {"name": "bluetest/create.yml", "content": create_yml},
            "target": f"{dir}/create.yml",
            "data": {"group": "web", "name": "test"},
        },
        {
            "template": {"name": "bluetest/ansible.cfg", "content": ansible_cfg},
            "target": f"{dir}/ansible.cfg",
            "data": {"inventory": "inventory.ini", "host_key_checking": "False"},
        },
    ]


async def test_ansible_with_spec_scaffolds_on_create(tmp_path):
    dir = str(tmp_path)
    stub_ansible_exec()
    opts = await ansible_with_spec(
        {"blue/event": "create"}, specs(dir), dir=dir, inventory="inventory.ini"
    )
    # scaffolded playbook is rendered
    assert os.path.exists(f"{dir}/create.yml")
    assert "hosts: web" in Path(f"{dir}/create.yml").read_text()
    # scaffolded ansible.cfg is rendered
    assert os.path.exists(f"{dir}/ansible.cfg")
    assert "host_key_checking = False" in Path(f"{dir}/ansible.cfg").read_text()
    # ansible-step ran and returned recap
    assert opts["blue/exit"] == 0
    assert opts["ansible/recap"] == {
        "localhost": {
            "ok": 1,
            "changed": 0,
            "unreachable": 0,
            "failed": 0,
            "skipped": 0,
            "rescued": 0,
            "ignored": 0,
        }
    }


async def test_ansible_with_spec_cleans_up_on_delete(tmp_path):
    dir = str(tmp_path)
    Path(f"{dir}/create.yml").write_text("placeholder")
    Path(f"{dir}/ansible.cfg").write_text("placeholder")
    stub_ansible_exec()
    opts = await ansible_with_spec(
        {"blue/event": "delete"}, specs(dir), dir=dir, inventory="inventory.ini"
    )
    # ansible-step ran the delete playbook
    assert opts["blue/exit"] == 0
    # scaffolded files are removed
    assert not os.path.exists(f"{dir}/create.yml")
    assert not os.path.exists(f"{dir}/ansible.cfg")


async def test_extra_vars_user_key_and_host_key_checking_shape_the_command(tmp_path):
    dir = str(tmp_path)
    Path(f"{dir}/create.yml").write_text("placeholder")
    calls = stub_ansible_exec()
    seen_env = {}
    stub = runtime.exec

    async def capture(cmd, cwd=None, env=None):
        seen_env["env"] = env
        return await stub(cmd, cwd=cwd, env=env)

    runtime.exec = capture
    await ansible_step(
        {"blue/event": "create"},
        dir=dir,
        private_key="/keys/id_ed25519",
        user="root",
        extra_vars={"ensemble": [{"id": 1}]},
        host_key_checking=False,
    )
    assert calls[0] == [
        "ansible-playbook",
        "-i",
        "inventory.ini",
        "--private-key",
        "/keys/id_ed25519",
        "-u",
        "root",
        "-e",
        '{"ensemble":[{"id":1}]}',
        "create.yml",
    ]
    assert seen_env["env"] == {"ANSIBLE_HOST_KEY_CHECKING": "False"}


def test_inventory_quotes_host_and_group_values_separately():
    rendered = inventory_ini({'web': {
        'hosts': [{'name': 'localhost', 'vars': {'path': '/tmp/my keys/id', 'count': 3,
                                               'text': '123', 'enabled': True}}],
        'vars': {'path': '/tmp/my keys/id', 'text': '123', 'enabled': True},
    }})
    assert rendered == (
        '[web]\nlocalhost count=3 enabled=True path=\'"/tmp/my keys/id"\' text=\'"123"\'\n'
        '\n[web:vars]\nenabled=True\npath="/tmp/my keys/id"\ntext="123"\n'
    )


def test_inventory_advice_accepts_a_bare_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = {'app/name': 'test'}
    assert inventory_advice(lambda _: 'inventory.ini', {'web': {'hosts': [{'name': 'localhost'}]}})(opts) is opts
    assert Path('inventory.ini').read_text() == '[web]\nlocalhost\n'


def test_inventory_round_trips_through_ansible(tmp_path):
    import json
    import shutil
    import subprocess

    binary = shutil.which('ansible-inventory')
    if not binary:
        pytest.skip('ansible-inventory is not on PATH')
    values = {
        'path': '/tmp/my keys/id_ed25519', 'quote': 'he said "it\'s ready"',
        'backslash': 'C:\\keys\\new', 'comment': '#hash; value', 'empty': '',
        'numeric_string': '123', 'boolean_string': 'False', 'null_string': 'None',
        'boolean': True, 'number': 3, 'null': None, 'unicode': 'café',
        'whitespace': ' surrounding whitespace ', 'newline': 'first\nsecond',
        'nested': {'a': [1, False, 'x y']},
    }
    expected = {f'{prefix}_{k}': v for prefix in ['host', 'group'] for k, v in values.items()}
    target = tmp_path / 'inventory.ini'
    target.write_text(inventory_ini({'web': {
        'hosts': [{'name': 'localhost', 'vars': {f'host_{k}': v for k, v in values.items()}}],
        'vars': {f'group_{k}': v for k, v in values.items()},
    }}))
    parsed = subprocess.run([binary, '-i', str(target), '--list'], capture_output=True, text=True, check=True)
    assert 'Failed to parse' not in parsed.stderr
    actual = json.loads(parsed.stdout)['_meta']['hostvars']['localhost']
    assert actual == expected
