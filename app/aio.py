"""Small asyncio helpers."""
import asyncio
import json


async def run_json(*argv, default="[]", timeout=10):
    """Run a command and parse its stdout as JSON."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return json.loads(out or default)


async def race(*coros):
    """Run coroutines until the first one finishes, cancel the rest.

    Returns (result, exception) of the first to finish.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    first = next(iter(done))
    if first.cancelled():
        return None, asyncio.CancelledError()
    if first.exception() is not None:
        return None, first.exception()
    return first.result(), None
