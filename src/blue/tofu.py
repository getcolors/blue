# Event-aware OpenTofu steps and deterministic Terraform configuration helpers.

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .runtime import ExecResult, runtime
from .scaffold import scaffold
from .workflow import Opts, StepError

_INIT_ARGS = ["init", "-input=false", "-no-color"]
_APPLY_ARGS = ["apply", "-auto-approve", "-input=false", "-no-color"]
_DESTROY_ARGS = ["destroy", "-auto-approve", "-input=false", "-no-color"]


async def _tofu(
    dir: str, *args: str, env: dict[str, str | None] | None = None
) -> ExecResult:
    return await runtime.exec(["tofu", *args], cwd=dir, env=env)


def _fail(opts: Opts, res: ExecResult, cmd: str) -> Opts:
    return {
        **opts,
        "blue/exit": res.exit,
        "blue/err": f"tofu {cmd} failed: {res.err or res.out or '(no output)'}",
    }


def _parse_outputs(out: str) -> dict[str, Any]:
    parsed = json.loads(out)
    return {k: v["value"] for k, v in parsed.items()}


async def outputs(dir: str, env: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Parse ``tofu output -json`` using the same environment as the backend."""
    res = await _tofu(dir, "output", "-json", env=env)
    if res.exit > 0:
        raise StepError(f"tofu output failed: {res.err}")
    return _parse_outputs(res.out)


async def tofu_step(
    opts: Opts,
    *,
    dir: str,
    output_key: str = "tofu/outputs",
    env: dict[str, str | None] | None = None,
) -> Opts:
    is_delete = opts.get("blue/event") == "delete"
    init = await _tofu(dir, *_INIT_ARGS, env=env)
    if init.exit > 0:
        return _fail(opts, init, "init")
    cmd = _DESTROY_ARGS if is_delete else _APPLY_ARGS
    res = await _tofu(dir, *cmd, env=env)
    if res.exit > 0:
        return _fail(opts, res, cmd[0])
    if is_delete:
        return {**opts, "blue/exit": 0}
    return {**opts, "blue/exit": 0, output_key: await outputs(dir, env)}


async def tofu_with_spec(opts: Opts, specs: list[dict], **config: Any) -> Opts:
    """Render without running for build; render/run/remove safely for delete."""
    if opts.get("blue/event") == "build":
        return scaffold(opts, specs)
    if opts.get("blue/event") == "delete":
        rendered = scaffold({**opts, "blue/event": "create"}, specs)
        ran = await tofu_step({**rendered, "blue/event": "delete"}, **config)
        exit_code = ran.get("blue/exit") or 0
        return ran if exit_code > 0 else scaffold(ran, specs)
    return await tofu_step(scaffold(opts, specs), **config)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _json_value(nested)
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _pretty_json(value: Any, indent: int = 0) -> str:
    """Match Cheshire's pretty JSON, Green's byte-level artifact contract."""
    if isinstance(value, (list, tuple)):
        if not value:
            return "[ ]"
        return "[ " + ", ".join(_pretty_json(item, indent) for item in value) + " ]"
    if isinstance(value, dict):
        if not value:
            return "{ }"
        pad = " " * (indent + 2)
        close = " " * indent
        body = ",\n".join(
            f"{pad}{json.dumps(str(key))} : {_pretty_json(nested, indent + 2)}"
            for key, nested in value.items()
        )
        return f"{{\n{body}\n{close}}}"
    return json.dumps(value, separators=(",", ":"))


def _backend_json(type: str, config: dict[str, Any]) -> str:
    document = _json_value({"terraform": {"backend": {str(type): config}}})
    return _pretty_json(document) + "\n"


BackendConfig = dict | Callable[[Opts], dict]


def _resolve_config(config: BackendConfig, opts: Opts) -> dict[str, Any]:
    return config(opts) if callable(config) else config


def backend_advice(dir_fn: Callable[[Opts], str], type: str, config: BackendConfig):
    def advice(opts: Opts) -> Opts:
        dir = dir_fn(opts)
        os.makedirs(dir, exist_ok=True)
        with open(os.path.join(dir, "backend.tf.json"), "w") as f:
            f.write(_backend_json(type, _resolve_config(config, opts)))
        return opts

    return advice


def local_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig | None = None):
    return backend_advice(dir_fn, "local", config if config is not None else {})


def s3_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig):
    return backend_advice(dir_fn, "s3", config)


def gcs_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig):
    return backend_advice(dir_fn, "gcs", config)


def r2_backend_advice(dir_fn: Callable[[Opts], str], config: BackendConfig):
    def r2_config(opts: Opts) -> dict:
        resolved = _resolve_config(config, opts)
        return {
            "bucket": resolved.get("bucket"),
            "key": resolved.get("key"),
            "region": "auto",
            "endpoints": {"s3": resolved.get("endpoint")},
            "skip_credentials_validation": True,
            "skip_metadata_api_check": True,
            "skip_region_validation": True,
            "skip_requesting_account_id": True,
            "use_path_style": False,
        }

    return s3_backend_advice(dir_fn, r2_config)


def conventional_backend_advice(
    *, dir: Callable[[Opts], str], key: Callable[[Opts], str],
    choose: Callable[[Opts], str] | None = None, fields: dict | None = None,
):
    fields = fields or {}
    s3 = {"bucket": "s3-bucket", "region": "s3-region", **fields.get("s3", {})}
    r2 = {"bucket": "r2-bucket", "endpoint": "r2-endpoint", **fields.get("r2", {})}
    return backends(choose or (lambda o: str(o.get("provider-backend") or "local")), {
        "local": local_backend_advice(dir),
        "s3": s3_backend_advice(dir, lambda o: {
            "bucket": o.get(s3["bucket"]), "key": key(o), "region": o.get(s3["region"]),
        }),
        "r2": r2_backend_advice(dir, lambda o: {
            "bucket": o.get(r2["bucket"]), "key": key(o), "endpoint": o.get(r2["endpoint"]),
        }),
    })


def backends(choose: Callable[[Opts], str], advices: dict[str, Callable[[Opts], Opts]]):
    def advice(opts: Opts) -> Opts:
        name = choose(opts)
        if name not in advices:
            raise StepError(f"unsupported OpenTofu backend: {name}")
        return advices[name](opts)

    return advice


def hcl_list(values: list[Any]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def hcl_map(values: dict[str, str]) -> str:
    if not values:
        return "{}"
    entries = ",\n".join(
        f"    {json.dumps(str(key))} : {json.dumps(value)}"
        for key, value in sorted(values.items())
    )
    return f"{{\n{entries}\n  }}"


def construct_name(name: str) -> str:
    namespace, separator, local_name = str(name).partition("/")
    if not separator:
        namespace, local_name = "", namespace
    sanitize = lambda part: part.replace("-", "_").replace(".", "_")
    return f"{sanitize(namespace)}_" * bool(namespace) + sanitize(local_name)


def construct(group: str, type: str, name: str, block: dict) -> dict:
    return {group: {type: {construct_name(name): block}}}


def deep_merge(*maps: dict) -> dict:
    result: dict = {}
    for map_ in maps:
        for key, value in map_.items():
            old = result.get(key)
            result[key] = (
                deep_merge(old, value)
                if isinstance(old, dict) and isinstance(value, dict)
                else value
            )
    return result


def constructs_json(constructs: list[dict]) -> str:
    document = deep_merge(*constructs) if constructs else {}
    return _pretty_json(_json_value(document))
