# CLI plumbing: `./blue <event> [-f|--file blue.yml] [--start step]
# [--end step] [--dry-run]`. The first positional argument is the lifecycle
# event, stamped into opts as "blue/event". --start/--end run a slice of the
# graph.

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import yaml

from .workflow import Opts, Workflow, run

USAGE = "Usage: blue <event> [-f|--file blue.yml] [--start step] [--end step] [--dry-run]"

# The parameter namespace every colour shares, so one variable serves green,
# red and blue without naming any of them.
_PAR_PREFIX = "COLORS_PAR_"


def par_name(key: str) -> str:
    return _PAR_PREFIX + str(key).upper().replace("-", "_")


def _coerce(old: object, value: str) -> object:
    if isinstance(old, bool):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    if isinstance(old, int) and not isinstance(old, bool):
        try:
            return int(value)
        except ValueError:
            pass
    return value


def read_pars(opts: Opts, env: dict[str, str] | None = None) -> Opts:
    result = dict(opts)
    for name, value in (os.environ if env is None else env).items():
        if name.startswith(_PAR_PREFIX) and len(name) > len(_PAR_PREFIX):
            key = name[len(_PAR_PREFIX) :].lower().replace("_", "-")
            result[key] = _coerce(result.get(key), value)
    return result


class _UsageError(Exception):
    pass


def _parse_args(args: list[str]) -> tuple[dict, list[str]]:
    values: dict = {"file": "blue.yml", "start": None, "end": None, "dry-run": False}
    positionals: list[str] = []
    names = {"-f": "file", "--file": "file", "--start": "start", "--end": "end"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            values["dry-run"] = True
        elif a in names:
            i += 1
            if i >= len(args):
                raise _UsageError(f"missing value for option: {a}")
            values[names[a]] = args[i]
        elif a.startswith("--") and "=" in a and a.split("=", 1)[0] in names:
            name, value = a.split("=", 1)
            values[names[name]] = value
        elif a.startswith("-") and a != "-":
            raise _UsageError(f"unknown option: {a}\n{USAGE}")
        else:
            positionals.append(a)
        i += 1
    return values, positionals


# PyYAML parses YAML 1.1, where unquoted `no`/`yes`/`on`/`off` are booleans and
# an integer may be octal by leading zero (`012` is 10) or sexagesimal by colon
# (`1:30` is 90). blue documents YAML 1.2 core-schema semantics (matching red's
# Bun.YAML), so both the bool and int resolvers are replaced: only true/false
# variants are booleans, and an integer is decimal unless prefixed 0o or 0x.
class _CoreLoader(yaml.SafeLoader):
    pass


_REPLACED = ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int")
_CoreLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag not in _REPLACED]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_CoreLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
_CoreLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)


def _construct_core_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    """PyYAML's own int constructor reads a leading zero as octal even when the
    resolver above has already accepted the scalar as decimal, so the base has
    to be decided here too: only an explicit 0o or 0x prefix leaves base ten."""
    text = str(loader.construct_scalar(node))
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("-+")
    prefix = digits[:2].lower()
    if prefix == "0o":
        return sign * int(digits[2:], 8)
    if prefix == "0x":
        return sign * int(digits[2:], 16)
    return sign * int(digits, 10)


_CoreLoader.add_constructor("tag:yaml.org,2002:int", _construct_core_int)


def load_yaml(text: str) -> Opts:
    """Parse desired state with YAML 1.2 core-schema booleans: `no` is a
    string, `zk/…` keys parse unquoted."""
    return yaml.load(text, Loader=_CoreLoader) or {}


def find_up(name: str, start: str | None = None) -> str | None:
    directory = Path(start or os.getcwd()).resolve()
    while True:
        candidate = directory / name
        if candidate.exists():
            return str(candidate)
        if directory.parent == directory:
            return None
        directory = directory.parent


def stage_dir(opts: Opts, tool: str, *, default_workdir: str = ".colors",
              default_profile: str = "default", state_file_key: str = "blue/state-file") -> str:
    workdir = Path(str(opts.get("workdir") or default_workdir))
    state_file = opts.get(state_file_key)
    root = Path(str(state_file)).parent / workdir if not workdir.is_absolute() and isinstance(state_file, str) else workdir
    return str(root / str(opts.get("profile") or default_profile) / tool)


async def run_cli(workflow: Workflow, args: list[str], *, default_file: str = "blue.yml",
                  search_parents: bool = False, allowed_events: list[str] | None = None) -> Opts:
    """Parse `args`, load the desired state, stamp "blue/event", run
    `workflow`. Returns the final opts dict ("blue/exit" 2 on
    usage/state-file errors)."""
    try:
        values, positionals = _parse_args(args)
        if values["file"] == "blue.yml" and default_file != "blue.yml":
            values["file"] = default_file
        if search_parents and values["file"] == default_file:
            values["file"] = find_up(default_file) or default_file
        event = positionals[0] if positionals else None
        if not event or (allowed_events is not None and event not in allowed_events):
            return {"blue/exit": 2, "blue/err": USAGE}
        if not os.path.exists(values["file"]):
            return {"blue/exit": 2, "blue/err": f"desired state file not found: {values['file']}"}
        # "blue/state-file" is the absolute path the state came from, so a
        # project can resolve its own relative paths against the file rather
        # than against whatever directory the command happened to run in.
        with open(values["file"]) as f:
            state = read_pars({
                **load_yaml(f.read()),
                "blue/state-file": os.path.abspath(values["file"]),
            })
        wf = replace(
            workflow,
            **({"start": values["start"]} if values["start"] else {}),
            **({"end": values["end"]} if values["end"] else {}),
        )
        opts: Opts = {
            **state,
            "blue/event": event,
            **({"blue/dry-run": True} if values["dry-run"] else {}),
        }
        return await run(wf, opts)
    except Exception as t:
        return {"blue/exit": 2, "blue/err": str(t) or type(t).__name__}


def exec_cli(workflow: Workflow, args: list[str] | None = None, **config) -> None:
    """Run and exit the process with "blue/exit", printing "blue/err" and
    "blue/trace" to stderr. For use from the project's ./blue uv script."""
    argv = sys.argv[1:] if args is None else args

    async def main() -> Opts:
        # a stray un-awaited failure cannot be attributed to a branch; report
        # it and fail the process instead of letting the runtime decide
        def on_unhandled(loop: asyncio.AbstractEventLoop, context: dict) -> None:
            exc = context.get("exception")
            print(f"unhandled exception: {exc or context.get('message')}", file=sys.stderr)
            os._exit(1)

        asyncio.get_running_loop().set_exception_handler(on_unhandled)
        return await run_cli(workflow, argv, **config)

    res = asyncio.run(main())
    if res.get("blue/err"):
        print(res["blue/err"], file=sys.stderr)
        if res.get("blue/trace"):
            print(res["blue/trace"], file=sys.stderr)
    exit_code = res.get("blue/exit")
    sys.exit(exit_code if exit_code is not None else 0)
