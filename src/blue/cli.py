"""Configuration helpers; the cli module path is retained for consumers.

Applications own argument parsing, file selection, validation and reporting.
"""

from __future__ import annotations

import os
import re

import yaml

from .workflow import Opts

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


# PyYAML parses YAML 1.1, where unquoted `no`/`yes`/`on`/`off` are booleans and
# an integer may be octal by leading zero (`012` is 10) or sexagesimal by colon
# (`1:30` is 90). blue documents YAML 1.2 core-schema semantics (matching red's
# Bun.YAML). Replace the scalar resolvers, including floats and timestamps:
# dates and sexagesimal values stay strings, scientific notation is numeric.
class _CoreLoader(yaml.SafeLoader):
    pass


_REPLACED = tuple(f"tag:yaml.org,2002:{kind}" for kind in ("bool", "int", "float", "timestamp"))
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

_CoreLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+0123456789."),
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
