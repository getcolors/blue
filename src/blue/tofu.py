# Event-aware OpenTofu steps: any non-"delete" event (conventionally
# "create") -> init + apply, "delete" -> init + destroy. After apply,
# `tofu output -json` is merged into opts under a namespaced key
# ("tofu/outputs" by default). The backend is not hardwired: attach a
# `before` advice built by `backend_advice`, `local_backend_advice`,
# `s3_backend_advice`, or `gcs_backend_advice` to write backend.tf before
# the tofu command runs.

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .runtime import ExecResult, runtime
from .workflow import Opts, StepError

_INIT_ARGS = ["init", "-input=false", "-no-color"]
_APPLY_ARGS = ["apply", "-auto-approve", "-input=false", "-no-color"]
_DESTROY_ARGS = ["destroy", "-auto-approve", "-input=false", "-no-color"]


async def _tofu(dir: str, *args: str) -> ExecResult:
    return await runtime.exec(["tofu", *args], cwd=dir)


def _fail(opts: Opts, res: ExecResult, cmd: str) -> Opts:
    return {
        **opts,
        "blue/exit": res.exit,
        "blue/err": f"tofu {cmd} failed: {res.err or res.out or '(no output)'}",
    }


def _parse_outputs(out: str) -> dict[str, Any]:
    parsed = json.loads(out)
    return {k: v["value"] for k, v in parsed.items()}


async def outputs(dir: str) -> dict[str, Any]:
    """Parse `tofu output -json` in `dir` into a plain dict of name -> value."""
    res = await _tofu(dir, "output", "-json")
    if res.exit > 0:
        raise StepError(f"tofu output failed: {res.err}")
    return _parse_outputs(res.out)


async def tofu_step(opts: Opts, *, dir: str, output_key: str = "tofu/outputs") -> Opts:
    """Run OpenTofu in `dir` according to "blue/event". On success, apply
    merges the outputs under `output_key` (default "tofu/outputs") — never
    top-level."""
    is_delete = opts.get("blue/event") == "delete"
    init = await _tofu(dir, *_INIT_ARGS)
    if init.exit > 0:
        return _fail(opts, init, "init")
    cmd = _DESTROY_ARGS if is_delete else _APPLY_ARGS
    res = await _tofu(dir, *cmd)
    if res.exit > 0:
        return _fail(opts, res, cmd[0])
    if is_delete:
        return {**opts, "blue/exit": 0}
    return {**opts, "blue/exit": 0, output_key: await outputs(dir)}


def _hcl_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))


def _backend_hcl(type: str, config: dict[str, Any]) -> str:
    attrs = "".join(f"    {k} = {_hcl_value(v)}\n" for k, v in sorted(config.items()))
    return f'terraform {{\n  backend "{type}" {{\n{attrs}  }}\n}}\n'


BackendConfig = dict | Callable[[Opts], dict]


def _resolve_config(config: BackendConfig, opts: Opts) -> dict[str, Any]:
    return config(opts) if callable(config) else config


def backend_advice(dir_fn: Callable[[Opts], str], type: str, config: BackendConfig):
    """Build a `before` advice that writes a backend config into the directory
    returned by dir_fn(opts) before the step runs. `type` is the backend name
    ("local", "s3", "gcs", …); `config` is a flat dict of backend attributes,
    or a function of opts returning one."""

    def advice(opts: Opts) -> Opts:
        dir = dir_fn(opts)
        os.makedirs(dir, exist_ok=True)
        with open(os.path.join(dir, "backend.tf"), "w") as f:
            f.write(_backend_hcl(type, _resolve_config(config, opts)))
        return opts

    return advice


def local_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig | None = None):
    """Backend advice for the local filesystem backend."""
    return backend_advice(dir_fn, "local", config if config is not None else {})


def s3_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig):
    """Backend advice for the S3 backend, e.g.
    {"bucket": "my-state", "key": "blue/node-1.tfstate", "region": "eu-west-1"}."""
    return backend_advice(dir_fn, "s3", config)


def gcs_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig):
    """Backend advice for the GCS backend, e.g.
    {"bucket": "my-state", "prefix": "blue/node-1"}."""
    return backend_advice(dir_fn, "gcs", config)
