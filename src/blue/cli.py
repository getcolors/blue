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

import yaml

from .workflow import Opts, Workflow, run

USAGE = "Usage: blue <event> [-f|--file blue.yml] [--start step] [--end step] [--dry-run]"
_PAR_PREFIX = "BLUE_PAR_"


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


# PyYAML parses YAML 1.1, where unquoted `no`/`yes`/`on`/`off` are booleans.
# blue documents YAML 1.2 core-schema semantics (matching red's Bun.YAML):
# only true/false variants are booleans, so a loader with the 1.1 bool
# resolver replaced is used.
class _CoreLoader(yaml.SafeLoader):
    pass


_CoreLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_CoreLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml(text: str) -> Opts:
    """Parse desired state with YAML 1.2 core-schema booleans: `no` is a
    string, `zk/…` keys parse unquoted."""
    return yaml.load(text, Loader=_CoreLoader) or {}


async def run_cli(workflow: Workflow, args: list[str]) -> Opts:
    """Parse `args`, load the desired state, stamp "blue/event", run
    `workflow`. Returns the final opts dict ("blue/exit" 2 on
    usage/state-file errors)."""
    try:
        values, positionals = _parse_args(args)
        event = positionals[0] if positionals else None
        if not event:
            return {"blue/exit": 2, "blue/err": USAGE}
        if not os.path.exists(values["file"]):
            return {"blue/exit": 2, "blue/err": f"desired state file not found: {values['file']}"}
        with open(values["file"]) as f:
            state = read_pars(load_yaml(f.read()))
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


def exec_cli(workflow: Workflow, args: list[str] | None = None) -> None:
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
        return await run_cli(workflow, argv)

    res = asyncio.run(main())
    if res.get("blue/err"):
        print(res["blue/err"], file=sys.stderr)
        if res.get("blue/trace"):
            print(res["blue/trace"], file=sys.stderr)
    exit_code = res.get("blue/exit")
    sys.exit(exit_code if exit_code is not None else 0)
