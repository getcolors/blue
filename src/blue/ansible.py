# Event-aware Ansible steps: any non-"delete" event (conventionally
# "create") runs the create playbook, "delete" runs the delete playbook —
# both via `ansible-playbook` over SSH. After a successful run the PLAY
# RECAP is parsed and merged into opts under a namespaced key
# ("ansible/recap" by default). The inventory is not hardwired: attach
# `inventory_advice` as a `before` advice to write an INI inventory from a
# function of opts before the step runs, the way blue.tofu attaches backends.

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from .runtime import ExecResult, runtime
from .scaffold import scaffold
from .workflow import Opts

# Event -> playbook file, relative to the step's dir.
default_playbooks = {"create": "create.yml", "delete": "delete.yml"}


def playbook(opts: Opts, playbooks: dict | None = None) -> str:
    """The playbook `ansible_step` runs for opts' "blue/event": "delete"
    selects the delete entry, every other event the create entry."""
    merged = {**default_playbooks, **(playbooks or {})}
    return merged["delete"] if opts.get("blue/event") == "delete" else merged["create"]


_RECAP_LINE_RE = re.compile(
    r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)"
    r"\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)",
    re.M,
)

_RECAP_COUNTERS = ("ok", "changed", "unreachable", "failed", "skipped", "rescued", "ignored")


def parse_recap(out: str) -> dict[str, dict[str, int]]:
    """Parse the PLAY RECAP section of `ansible-playbook` output into
    {host: {ok, changed, unreachable, failed, skipped, rescued, ignored}}."""
    recap: dict[str, dict[str, int]] = {}
    for m in _RECAP_LINE_RE.finditer(str(out)):
        host = m.group(1)
        recap[host] = {k: int(m.group(i + 2)) for i, k in enumerate(_RECAP_COUNTERS)}
    return recap


def _fail(opts: Opts, res: ExecResult, pb: str) -> Opts:
    return {
        **opts,
        "blue/exit": res.exit,
        "blue/err": f"ansible-playbook {pb} failed: {res.out or res.err or '(no output)'}",
    }


async def ansible_step(
    opts: Opts,
    *,
    # working directory; playbook and inventory paths resolve relative to it
    dir: str,
    # inventory file (default "inventory.ini")
    inventory: str = "inventory.ini",
    # {"create", "delete"} overriding default_playbooks
    playbooks: dict | None = None,
    # SSH private key file passed as --private-key
    private_key: str | None = None,
    # remote user passed as -u
    user: str | None = None,
    # dict passed as -e in JSON form
    extra_vars: dict | None = None,
    # set to False to export ANSIBLE_HOST_KEY_CHECKING=False for the run —
    # for ephemeral or emulated hosts whose host keys change on every create.
    # Omitted or True, the environment is left untouched.
    host_key_checking: bool | None = None,
    # namespaced key for the parsed recap
    recap_key: str = "ansible/recap",
) -> Opts:
    """Run `ansible-playbook` in `dir` according to "blue/event" (see
    `playbook`). On success the parsed PLAY RECAP is merged under `recap_key`
    (default "ansible/recap") — never top-level."""
    pb = playbook(opts, playbooks)
    env = {"ANSIBLE_HOST_KEY_CHECKING": "False"} if host_key_checking is False else None
    args = ["-i", inventory]
    if private_key:
        args += ["--private-key", str(private_key)]
    if user:
        args += ["-u", user]
    if extra_vars is not None:
        args += ["-e", json.dumps(extra_vars, separators=(",", ":"))]
    args.append(pb)
    res = await runtime.exec(["ansible-playbook", *args], cwd=dir, env=env)
    if res.exit > 0:
        return _fail(opts, res, pb)
    return {**opts, "blue/exit": 0, recap_key: parse_recap(res.out)}


async def ansible_with_spec(opts: Opts, specs: list[dict], **config: Any) -> Opts:
    """Scaffold ansible config files (playbooks, ansible.cfg, …) then run
    ansible-playbook (create); or run ansible-playbook then remove the
    scaffolded files (delete). Mirrors the tofu-with-spec pattern. `config`
    keywords are `ansible_step`'s."""
    if opts.get("blue/event") == "delete":
        ran = await ansible_step(opts, **config)
        exit = ran.get("blue/exit")
        return ran if (exit if exit is not None else 0) > 0 else scaffold(ran, specs)
    return await ansible_step(scaffold(opts, specs), **config)


# --- inventory -----------------------------------------------------------------


def _ini_vars(vars: dict | None) -> list[str]:
    return [f"{k}={v}" for k, v in sorted((vars or {}).items())]


def _host_line(host: dict) -> str:
    return " ".join([host["name"], *_ini_vars(host.get("vars"))])


def _group_section(group: str, spec: dict) -> str:
    host_entries = sorted(spec["hosts"], key=lambda h: h["name"])
    lines = f"[{group}]\n" + "".join(f"{_host_line(h)}\n" for h in host_entries)
    var_entries = _ini_vars(spec.get("vars"))
    if not var_entries:
        return lines
    return f"{lines}\n[{group}:vars]\n" + "".join(f"{v}\n" for v in var_entries)


def inventory_ini(groups: dict[str, dict]) -> str:
    """Render an Ansible INI inventory from
    {group: {"hosts": [{"name": "zk1", "vars": {"ansible_host": "172.17.0.3"}}, ...],
             "vars": {"ansible_user": "root"}}}.
    Groups, hosts, and vars are emitted in sorted order for deterministic
    output."""
    return "\n".join(_group_section(group, spec) for group, spec in sorted(groups.items()))


InventoryGroups = dict | Callable[[Opts], dict]


def inventory_advice(file_fn: Callable[[Opts], str], groups: InventoryGroups):
    """Build a `before` advice that writes an INI inventory to the file
    returned by file_fn(opts) before the step runs. `groups` is the inventory
    data (see `inventory_ini`), or a function of opts returning it."""

    def advice(opts: Opts) -> Opts:
        file = file_fn(opts)
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "w") as f:
            f.write(inventory_ini(groups(opts) if callable(groups) else groups))
        return opts

    return advice
