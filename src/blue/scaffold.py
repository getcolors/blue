# Flat file-spec scaffolding DSL. A spec is a seq of dicts, one per file:
#
#   {"template": {"name": "zk/main.tf", "content": main_tf},  # package text
#    "target": "{{workdir}}/n/{{node.id}}/main.tf",           # rendered vs data
#    "data": {...}}
#
# The package owns its templates: read them next to the module
# (`(Path(__file__).parent / "main.tf").read_text()` or
# `importlib.resources`) and pass {"name", "content"}; nothing is resolved
# at run time. On "blue/event" "delete" the same specs name the targets to
# remove. Rendering is handled by blue's small Selmer-compatible renderer.

from __future__ import annotations

import os

from .renderer import render
from .workflow import Opts, StepError


def render_template(template: dict, data: dict, opts: dict | None = None) -> str:
    """Render a template's content with `data`. Optional `opts` are forwarded
    to the renderer (delimiter overrides and friends)."""
    content = (template or {}).get("content")
    if not isinstance(content, str):
        name = (template or {}).get("name", repr(template))
        raise StepError(
            f"template not found: {name} has no content — pass "
            '{"name", "content"} with content read from the owning package'
        )
    return render(content, data, opts)


def _prune_empty_dir(file: str) -> None:
    parent = os.path.dirname(file)
    try:
        if not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        # parent missing or not a directory — nothing to prune
        pass


def _target_path(spec: dict) -> str:
    return render(spec["target"], spec["data"])


def _delete_target(target: str) -> None:
    if os.path.exists(target):
        os.unlink(target)
    _prune_empty_dir(target)


def _create_target(spec: dict, target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(render_template(spec["template"], spec["data"], spec.get("opts")))


def scaffold(opts: Opts, specs: list[dict]) -> Opts:
    """Materialize `specs` (create) or remove their targets (delete), driven
    by "blue/event" in `opts`. Returns opts with "blue/exit" 0 and the
    affected paths under "blue.scaffold/written" or "blue.scaffold/deleted"."""
    all_specs = list(specs)
    targets = [_target_path(spec) for spec in all_specs]
    if opts.get("blue/event") == "delete":
        for target in targets:
            _delete_target(target)
        return {**opts, "blue/exit": 0, "blue.scaffold/deleted": targets}
    for spec, target in zip(all_specs, targets):
        _create_target(spec, target)
    return {**opts, "blue/exit": 0, "blue.scaffold/written": targets}
