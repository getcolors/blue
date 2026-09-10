# The runtime seam: every subprocess and every log line goes through this
# mutable object so tests can stub them (the port of green's
# `with-redefs [sh/sh ...]` sites) and future features can record runs.

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass


@dataclass
class ExecResult:
    exit: int
    out: str
    err: str


async def _spawn_exec(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str | None] | None = None,
    timeout_ms: int | None = None,
) -> ExecResult:
    try:
        full_env = None
        if env is not None:
            full_env = {**os.environ, **{k: v for k, v in env.items() if v is not None}}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), None if timeout_ms is None else timeout_ms / 1000
            )
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timeout = f"command timed out after {timeout_ms}ms"
            captured = err.decode()
            return ExecResult(
                exit=124,
                out=out.decode(),
                err=f"{captured}\n{timeout}" if captured else timeout,
            )
        # asyncio uses negative return codes for signals, while Blue steps
        # reserve positive codes for failure. Preserve the shell convention
        # so interrupted commands cannot look successful to workflow helpers.
        code = proc.returncode or 0
        return ExecResult(exit=128 - code if code < 0 else code, out=out.decode(), err=err.decode())
    except OSError as e:
        # command not found and similar spawn failures follow the same shape
        return ExecResult(exit=127, out="", err=str(e))


def _print_log(*args: object) -> None:
    print(*args)


class Runtime:
    def __init__(self) -> None:
        self.exec = _spawn_exec
        self.log = _print_log


runtime = Runtime()
