# The runtime seam: every subprocess and every log line goes through this
# mutable object so tests can stub them (the port of green's
# `with-redefs [sh/sh ...]` sites) and future features can record runs.

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class ExecResult:
    exit: int
    out: str
    err: str


def _spawn_inherit(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str | None] | None = None,
) -> ExecResult:
    try:
        full_env = None
        if env is not None:
            full_env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
        proc = subprocess.run(cmd, cwd=cwd, env=full_env, check=False)
        code = proc.returncode
        return ExecResult(exit=128 - code if code < 0 else code, out="", err="")
    except OSError as error:
        return ExecResult(exit=127, out="", err=str(error))


async def _capture(stream: asyncio.StreamReader, chunks: bytearray) -> None:
    while chunk := await stream.read(65536):
        chunks.extend(chunk)


async def _stop_process(proc: asyncio.subprocess.Process, completion: asyncio.Future) -> None:
    try:
        if os.name == "posix":
            # The group can outlive its leader and keep its output pipes open.
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            # Windows has no killpg. taskkill terminates the process tree while
            # the command is alive; keep a direct kill as a fallback.
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(killer.wait(), 1)
                except TimeoutError:
                    try:
                        killer.kill()
                    except ProcessLookupError:
                        pass
            except OSError:
                pass
            if proc.returncode is None:
                proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(completion), 1)
    except TimeoutError:
        # A descendant may have detached from the group with inherited pipes.
        # asyncio has no public Process.close; close its transport to release
        # those pipes without waiting for the detached process to exit.
        proc._transport.close()
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)


async def _spawn_exec(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str | None] | None = None,
    timeout_ms: int | None = None,
) -> ExecResult:
    try:
        full_env = None
        if env is not None:
            full_env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        out, err = bytearray(), bytearray()
        completion = asyncio.gather(
            proc.wait(), _capture(proc.stdout, out), _capture(proc.stderr, err)
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(completion),
                None if timeout_ms is None else timeout_ms / 1000,
            )
        except TimeoutError:
            await asyncio.shield(_stop_process(proc, completion))
            timeout = f"command timed out after {timeout_ms}ms"
            captured = err.decode(errors="replace")
            return ExecResult(
                exit=124,
                out=out.decode(errors="replace"),
                err=f"{captured}\n{timeout}" if captured else timeout,
            )
        except asyncio.CancelledError:
            await asyncio.shield(_stop_process(proc, completion))
            raise
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
        self.exec_inherit = _spawn_inherit
        self.log = _print_log


runtime = Runtime()
