# Small Selmer-compatible renderer for the subset blue exercises.
#
# Supported:
# - Variables with dotted paths: {{name}}, {{node.id}}; missing values -> "".
# - HTML escaping by default; the `safe` filter bypasses escaping.
# - `for` loops and `if` blocks; blocks may nest.
# - Filter pipelines: `safe`, `not-empty`, and `sort(attribute='field')`.
# - Selmer-style delimiter overrides: tag_open/tag_close/filter_open/
#   filter_close are single-character pieces. Defaults make variables {{ }}
#   and tags {% %}; e.g. {"tag_open": "<", "tag_close": ">",
#   "filter_open": "{", "filter_close": "}"} makes variables <{ }> and tags
#   <% %>, leaving Jinja2 {{ }}/{% %} intact.

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Delimiters:
    var_open: str
    var_close: str
    tag_open: str
    tag_close: str


def _delimiters(opts: dict | None) -> _Delimiters:
    o = opts or {}
    tag_open = o.get("tag_open", "{")
    tag_close = o.get("tag_close", "}")
    filter_open = o.get("filter_open", "{")
    filter_close = o.get("filter_close", "}")
    return _Delimiters(
        var_open=f"{tag_open}{filter_open}",
        var_close=f"{filter_close}{tag_close}",
        tag_open=f"{tag_open}%",
        tag_close=f"%{tag_close}",
    )


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"}


def _html_escape(s: str) -> str:
    return re.sub(r"[&<>\"']", lambda m: _ESCAPES[m.group(0)], s)


def _value_string(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _split_pipeline(expr: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    for i, ch in enumerate(expr):
        prev = expr[i - 1] if i > 0 else ""
        if quote:
            if ch == quote and prev != "\\":
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "|" and depth == 0:
            parts.append(expr[start:i].strip())
            start = i + 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]


def _split_path(path: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    bracket_depth = 0
    for i, ch in enumerate(path):
        prev = path[i - 1] if i > 0 else ""
        if quote:
            if ch == quote and prev != "\\":
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "." and bracket_depth == 0:
            parts.append(path[start:i].strip())
            start = i + 1
    parts.append(path[start:].strip())
    return [p for p in parts if p]


def _unquote(s: str) -> str:
    t = s.strip()
    if (t.startswith("'") and t.endswith("'")) or (t.startswith('"') and t.endswith('"')):
        return re.sub(r"\\(['\"\\])", r"\1", t[1:-1])
    return t


_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _lookup(ctx: Any, expr: str) -> Any:
    trimmed = expr.strip()
    if trimmed == "":
        return None
    if trimmed == "true":
        return True
    if trimmed == "false":
        return False
    if trimmed in ("null", "nil"):
        return None
    if _NUMBER_RE.match(trimmed):
        return float(trimmed) if "." in trimmed else int(trimmed)
    if (trimmed.startswith("'") and trimmed.endswith("'")) or (
        trimmed.startswith('"') and trimmed.endswith('"')
    ):
        return _unquote(trimmed)

    cur: Any = ctx
    for segment in _split_path(trimmed):
        if cur is None:
            return None
        key = _unquote(segment)
        if isinstance(cur, (list, tuple)) and key.isdigit():
            i = int(key)
            cur = cur[i] if i < len(cur) else None
        elif isinstance(cur, dict):
            if key in cur:
                cur = cur[key]
            else:
                return None
        elif hasattr(cur, key):
            cur = getattr(cur, key)
        else:
            return None
    return cur


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _compare_values(a: Any, b: Any) -> int:
    if a is b or (type(a) is type(b) and a == b):
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if _is_number(a) and _is_number(b):
        return -1 if a < b else (1 if a > b else 0)
    a_s, b_s = str(a), str(b)
    return -1 if a_s < b_s else (1 if a_s > b_s else 0)


_ATTR_RE = re.compile(r"attribute\s*=\s*(['\"])(.*?)\1")


def _sort_value(value: Any, filter_expr: str) -> Any:
    if value is None:
        return []
    if isinstance(value, dict):
        items: list[Any] = []
    elif isinstance(value, (list, tuple, str)):
        items = list(value)
    elif hasattr(value, "__iter__"):
        items = list(value)
    else:
        items = []
    m = _ATTR_RE.search(filter_expr)
    attr = m.group(2) if m else None

    def key_of(x: Any) -> Any:
        return _lookup({"item": x}, f"item.{attr}") if attr else x

    return sorted(items, key=functools.cmp_to_key(lambda a, b: _compare_values(key_of(a), key_of(b))))


def _apply_filter(value: Any, safe: bool, filter_expr: str) -> tuple[Any, bool]:
    name = re.split(r"[(:\s]", filter_expr, maxsplit=1)[0].strip()
    if name == "safe":
        return value, True
    if name == "sort":
        return _sort_value(value, filter_expr), safe
    if name == "not-empty":
        try:
            return (value if value is not None and len(value) > 0 else None), safe
        except TypeError:
            return value, safe
    # Unknown filters are deliberately conservative: keep the value as-is.
    # This mirrors the renderer's small-subset role without corrupting data.
    return value, safe


def _eval_expr(expr: str, ctx: Any) -> tuple[Any, bool]:
    parts = _split_pipeline(expr)
    head, filters = (parts[0], parts[1:]) if parts else ("", [])
    value: Any = _lookup(ctx, head)
    safe = False
    for f in filters:
        value, safe = _apply_filter(value, safe, f)
    return value, safe


def _render_value(expr: str, ctx: Any) -> str:
    value, safe = _eval_expr(expr, ctx)
    s = _value_string(value)
    return s if safe else _html_escape(s)


def _to_iterable(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        return list(value)
    if hasattr(value, "__iter__"):
        return list(value)
    return []


_FOR_RE = re.compile(r"^for\s+(\S+)\s+in\s+(.+)$", re.S)


def _parse_for(tag: str) -> tuple[str, str] | None:
    m = _FOR_RE.match(tag.strip())
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def _parse_if(tag: str) -> str | None:
    m = re.match(r"^if\s+(.+)$", tag.strip(), re.S)
    return m.group(1).strip() if m else None


def _truthy(value: Any) -> bool:
    return value is not None and value is not False


def _find_matching_if(
    content: str, body_start: int, d: _Delimiters
) -> tuple[int, int | None, int | None, int]:
    depth = 1
    idx = body_start
    else_tag: tuple[int, int] | None = None
    while idx < len(content):
        tag_start = content.find(d.tag_open, idx)
        if tag_start < 0:
            break
        content_start = tag_start + len(d.tag_open)
        tag_end = content.find(d.tag_close, content_start)
        if tag_end < 0:
            break
        tag = content[content_start:tag_end].strip()
        if _parse_if(tag):
            depth += 1
        elif tag == "endif":
            depth -= 1
            if depth == 0:
                return (
                    else_tag[0] if else_tag else tag_start,
                    else_tag[1] if else_tag else None,
                    tag_start if else_tag else None,
                    tag_end + len(d.tag_close),
                )
        elif tag == "else" and depth == 1:
            else_tag = (tag_start, tag_end + len(d.tag_close))
        idx = tag_end + len(d.tag_close)
    raise ValueError("unterminated if block in template")


def _find_matching_endfor(content: str, body_start: int, d: _Delimiters) -> tuple[int, int]:
    depth = 1
    idx = body_start
    while idx < len(content):
        tag_start = content.find(d.tag_open, idx)
        if tag_start < 0:
            break
        tag_content_start = tag_start + len(d.tag_open)
        tag_end = content.find(d.tag_close, tag_content_start)
        if tag_end < 0:
            break
        tag = content[tag_content_start:tag_end].strip()
        if _parse_for(tag):
            depth += 1
        elif tag == "endfor":
            depth -= 1
            if depth == 0:
                return tag_start, tag_end + len(d.tag_close)
        idx = tag_end + len(d.tag_close)
    raise ValueError("unterminated for loop in template")


def _next_token(content: str, idx: int, d: _Delimiters) -> tuple[str, int] | None:
    var_at = content.find(d.var_open, idx)
    tag_at = content.find(d.tag_open, idx)
    if var_at < 0 and tag_at < 0:
        return None
    if var_at >= 0 and (tag_at < 0 or var_at <= tag_at):
        return ("var", var_at)
    return ("tag", tag_at)


def _render_block(content: str, ctx: dict, d: _Delimiters) -> str:
    out: list[str] = []
    idx = 0
    while idx < len(content):
        tok = _next_token(content, idx, d)
        if tok is None:
            out.append(content[idx:])
            break
        kind, at = tok
        out.append(content[idx:at])
        if kind == "var":
            expr_start = at + len(d.var_open)
            expr_end = content.find(d.var_close, expr_start)
            if expr_end < 0:
                out.append(content[at:])
                break
            out.append(_render_value(content[expr_start:expr_end].strip(), ctx))
            idx = expr_end + len(d.var_close)
        else:
            tag_start = at + len(d.tag_open)
            tag_end = content.find(d.tag_close, tag_start)
            if tag_end < 0:
                out.append(content[at:])
                break
            tag = content[tag_start:tag_end].strip()
            loop = _parse_for(tag)
            condition = _parse_if(tag)
            if loop:
                name, expr = loop
                body_start = tag_end + len(d.tag_close)
                body_end, after_end = _find_matching_endfor(content, body_start, d)
                body = content[body_start:body_end]
                value, _safe = _eval_expr(expr, ctx)
                for item in _to_iterable(value):
                    out.append(_render_block(body, {**ctx, name: item}, d))
                idx = after_end
            elif condition:
                body_start = tag_end + len(d.tag_close)
                then_end, else_start, else_end, after_end = _find_matching_if(
                    content, body_start, d
                )
                value, _safe = _eval_expr(condition, ctx)
                if _truthy(value):
                    body = content[body_start:then_end]
                elif else_start is not None and else_end is not None:
                    body = content[else_start:else_end]
                else:
                    body = ""
                out.append(_render_block(body, ctx, d))
                idx = after_end
            else:
                # Matching closing tags are consumed by their block handlers.
                # Unsupported tags render empty rather than leaking syntax.
                idx = tag_end + len(d.tag_close)
    return "".join(out)


def render(content: str, data: dict | None = None, opts: dict | None = None) -> str:
    return _render_block(str(content), data or {}, _delimiters(opts))
