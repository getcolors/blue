from __future__ import annotations
import inspect
from .runtime import ExecResult, runtime


def posix_quote(value) -> str: return "'" + str(value).replace("'", "'\\''") + "'"
def run_inherit(args: list[str], *, cwd: str | None = None, env: dict | None = None) -> ExecResult:
    return runtime.exec_inherit(args, cwd=cwd, env=env)


async def run_plan(commands: list[dict], *, runner=None, continue_on_error=None, cleanup=None):
    async def default_runner(command): return await runtime.exec(command["args"], **command.get("options", {}))
    invoke = runner or default_runner
    try:
        for command in commands:
            result = invoke(command)
            result = await result if inspect.isawaitable(result) else result
            if result.exit and not (continue_on_error and continue_on_error(command, result)):
                return {"exit": result.exit, "out": result.out, "err": result.err, "command": command}
        return {"exit": 0, "out": "", "err": ""}
    finally:
        if cleanup:
            result = cleanup()
            if inspect.isawaitable(result): await result
