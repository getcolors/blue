# The runtime seam: every log line written by the optional advice modules
# (dry_run, progress) goes through this mutable object so tests and packages
# can capture or redirect output without patching print.

from __future__ import annotations


def _print_log(*args: object) -> None:
    print(*args)


class Runtime:
    def __init__(self) -> None:
        self.log = _print_log


runtime = Runtime()
