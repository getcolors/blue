# Event-aware Ansible steps: any non-"delete" event (conventionally
# "create") runs the create playbook, "delete" runs the delete playbook —
# both via `ansible-playbook` over SSH. After a successful run the PLAY
# RECAP is parsed and merged into opts under a namespaced key
# ("ansible/recap" by default). The inventory is not hardwired: attach
# `inventory_advice` as a `before` advice to write an INI inventory from a
# function of opts before the step runs, the way blue.tofu attaches backends.

from __future__ import annotations

import json
import math
import os
import re
import shlex
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
    env: dict[str, str | None] | None = None,
    secret_env: list[str] | None = None,
) -> Opts:
    """Run `ansible-playbook` in `dir` according to "blue/event" (see
    `playbook`). On success the parsed PLAY RECAP is merged under `recap_key`
    (default "ansible/recap") — never top-level."""
    pb = playbook(opts, playbooks)
    env = dict(env or {})
    for name in [*(k for k in {*os.environ, *env} if k.startswith("COLORS_PAR_")), *(secret_env or [])]:
        env[name] = None
    if host_key_checking is False:
        env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
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
    """Render-only for build; render/run/remove safely for delete."""
    if opts.get("blue/event") == "build":
        return scaffold(opts, specs)
    if opts.get("blue/event") == "delete":
        rendered = scaffold({**opts, "blue/event": "create"}, specs)
        ran = await ansible_step({**rendered, "blue/event": "delete"}, **config)
        exit_code = ran.get("blue/exit") or 0
        return ran if exit_code > 0 else scaffold(ran, specs)
    return await ansible_step(scaffold(opts, specs), **config)


# --- inventory -----------------------------------------------------------------


def _python_literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_python_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(_python_literal(str(k)) + ":" + _python_literal(v)
                              for k, v in sorted(value.items())) + "}"
    raise ValueError("inventory variables require finite numbers or JSON-compatible values")


def _group_value(value: Any) -> str:
    # Both inventory forms evaluate Python literals. Keep ordinary names,
    # paths, and IPv4 addresses byte-compatible; quote all other strings.
    if isinstance(value, str) and (
        re.fullmatch(r"[A-Za-z_/@][A-Za-z0-9_./:@%+-]*", value)
        and value not in {"True", "False", "None"}
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
    ):
        return value
    return _python_literal(value)


def _host_value(value: Any) -> str:
    # Host lines also pass through shlex before Python literal evaluation.
    return shlex.quote(_group_value(value))


def _ini_vars(vars: dict | None, *, group: bool = False) -> list[str]:
    encode = _group_value if group else _host_value
    return [f"{k}={encode(v)}" for k, v in sorted((vars or {}).items())]


def _host_line(host: dict) -> str:
    return " ".join([host["name"], *_ini_vars(host.get("vars"))])


def _group_section(group: str, spec: dict) -> str:
    host_entries = sorted(spec["hosts"], key=lambda h: h["name"])
    lines = f"[{group}]\n" + "".join(f"{_host_line(h)}\n" for h in host_entries)
    var_entries = _ini_vars(spec.get("vars"), group=True)
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
        parent = os.path.dirname(file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file, "w") as f:
            f.write(inventory_ini(groups(opts) if callable(groups) else groups))
        return opts

    return advice
