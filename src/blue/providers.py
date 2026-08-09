from __future__ import annotations
from typing import Callable
from .workflow import Opts


def placeholder(value): return value is None or isinstance(value, str) and (not value.strip() or value.upper() == "REPLACE_ME")
def entry(registry, opts, slot): return registry.get(slot, {}).get(str(opts.get(slot)))
def selected(registry, opts, slots): return {slot: entry(registry, opts, slot) for slot in slots}
def slot_keys(registry, opts, slots, field): return [key for slot in slots for key in (entry(registry, opts, slot) or {}).get(field, [])]
def missing_keys(opts, keys): return [key for key in keys if placeholder(opts.get(key))]
def selection_errors(registry, opts, slots): return [f"{slot} has unsupported provider: {opts.get(slot)}" for slot in slots if entry(registry, opts, slot) is None]
def required_errors(registry, opts, slots, own=()): return [f"{key} is required" for key in missing_keys(opts, [*slot_keys(registry, opts, slots, "required"), *own])]
def secret_errors(registry, opts, slots, own, par_name: Callable): return [f"{par_name(key)} is required" for key in missing_keys(opts, [*slot_keys(registry, opts, slots, "secrets"), *own])]
def tofu_env(registry, opts, slot): return (entry(registry, opts, slot) or {}).get("tofu-env", {})
def tool_env(registry, opts, slots):
    mapping = {key: name for slot in slots for key, name in tofu_env(registry, opts, slot).items()}
    result = {name: str(opts[key]) for key, name in mapping.items() if not placeholder(opts.get(key))}
    return result or None
def refuse_overlay(env, key, reason, par_name: Callable):
    name = par_name(key)
    return [f"{name} is set; {reason}"] if env.get(name) else []
