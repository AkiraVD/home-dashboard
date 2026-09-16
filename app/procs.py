"""Terminal-session process helpers, shared by the web terminal and the Claude launcher."""
import asyncio
import os
import signal

import psutil


def session_members(sid: int) -> list[psutil.Process]:
    """Live processes in the terminal session `sid` (zombies excluded)."""
    out = []
    for p in psutil.process_iter():
        try:
            if os.getsid(p.pid) == sid and p.status() != psutil.STATUS_ZOMBIE:
                out.append(p)
        except (OSError, psutil.Error):
            continue
    return out


async def end_session(sid: int, grace: float = 0.3) -> None:
    """Hang up a terminal session and everything started in it, escalating until it's gone."""
    if grace:
        await asyncio.sleep(grace)
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        members = await asyncio.to_thread(session_members, sid)
        if not members:
            return
        for p in members:
            try:
                p.send_signal(sig)
            except psutil.Error:
                pass
        await asyncio.sleep(2)
