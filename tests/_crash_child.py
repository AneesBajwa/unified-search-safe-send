"""A worker process that dies mid-dispatch, for openspec task 5.11b.

Run as a subprocess with ``CRASH_AT`` and ``CRASH_MODE=exit`` set. The seam then
calls ``os._exit(1)``: no unwinding, no ``finally``, no rollback handler, no
atexit, no buffered writes flushed. That is what a container being killed looks
like, and it is the one thing an in-process ``raise`` cannot model — a raised
exception still runs every cleanup path on its way out, which is precisely the
code a real crash skips.

Exit codes: ``1`` means the seam fired (what the test wants). ``7`` means the
dispatch completed without crashing, which would make the test meaningless.
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    # Importing the module is what populates the handler and reconciler
    # registries — the same import every app does.
    import core.jobs.handlers  # noqa: F401
    from core.jobs.runtime import run_once

    await run_once(limit=1)
    return 7


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
