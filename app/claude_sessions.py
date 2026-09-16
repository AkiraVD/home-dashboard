"""Claude Code projects: which folders you work in, which sessions run, start and close them.

Start opens a real terminal window on the laptop's desktop (the service inherits
DISPLAY from the graphical session) running the same command you'd type by hand:

    claude [--continue] --remote-control <name>

Close hangs up that terminal session, which ends Claude and closes the window.
"""
import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import time
from pathlib import Path

import psutil

from . import config, procs

PROJECTS_DIR = Path.home() / ".claude" / "projects"
TERMINAL = "gnome-terminal"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MARKER = "HD_SESSION"

_decoded: dict[str, str | None] = {}


def _decode_dir(encoded: str) -> str | None:
    """Recover a project path from a ~/.claude/projects folder name.

    The name replaces every '/' with '-', and literal '-' and '_' also become '-',
    so it's ambiguous; walk the filesystem to find the one that exists.
    """
    if encoded in _decoded:
        return _decoded[encoded]
    tokens = [t for t in encoded.split("-") if t]

    def walk(i: int, parent: str, partial: str) -> str | None:
        if i == len(tokens):
            path = os.path.join(parent, partial)
            return path if partial and os.path.isdir(path) else None
        token = tokens[i]
        if not partial:
            return walk(i + 1, parent, token)
        for sep in ("-", "_"):
            found = walk(i + 1, parent, f"{partial}{sep}{token}")
            if found:
                return found
        nxt = os.path.join(parent, partial)
        return walk(i + 1, nxt, token) if os.path.isdir(nxt) else None

    _decoded[encoded] = walk(0, "/", "") if tokens else None
    return _decoded[encoded]


def _cwd_from_session(path: Path) -> str | None:
    try:
        with path.open(errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("cwd"):
                    return record["cwd"]
    except OSError:
        pass
    return None


def list_projects() -> list[dict]:
    try:
        dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]
    except OSError:
        return []
    out = []
    for d in dirs:
        try:  # some projects keep conversations in subfolders
            sessions = sorted(d.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            sessions = []
        path = (_cwd_from_session(sessions[0]) if sessions else None) or _decode_dir(d.name)
        out.append({
            "id": d.name,
            "path": path,
            "name": os.path.basename(path) if path else d.name,
            "sessions": len(sessions),
            "last_used": sessions[0].stat().st_mtime if sessions else None,
            "exists": bool(path and os.path.isdir(path)),
        })
    # Alphabetical, never by activity: cards must not move under your finger.
    out.sort(key=lambda p: (p["name"] or p["id"]).lower())
    return out


def _marker_of(p: psutil.Process) -> str | None:
    try:
        return p.environ().get(MARKER)
    except (psutil.Error, OSError):
        return None


def running_sessions() -> list[dict]:
    """Every `claude` process of this user, however it was started."""
    uid = os.getuid()
    out = []
    for p in psutil.process_iter(["name", "cmdline", "create_time", "uids"]):
        try:
            if p.info["name"] != "claude" or p.info["uids"].real != uid:
                continue
            cmdline = p.info["cmdline"] or []
            label = None
            if "--remote-control" in cmdline:
                i = cmdline.index("--remote-control")
                nxt = cmdline[i + 1] if i + 1 < len(cmdline) else ""
                label = nxt if nxt and not nxt.startswith("-") else "(auto)"
            cwd = p.cwd()
            out.append({
                "pid": p.pid,
                "cwd": cwd,
                # Very likely the session maintaining this dashboard — possibly the one
                # whoever is looking at this page is talking to.
                "own_project": os.path.realpath(cwd) == os.path.realpath(config.ROOT),
                "remote_control": label,
                "continued": bool({"--continue", "-c"} & set(cmdline)),
                "tty": p.terminal(),
                "started": p.info["create_time"],
                "sid": os.getsid(p.pid),
                "marker": _marker_of(p),
            })
        except (psutil.Error, OSError):
            continue
    out.sort(key=lambda s: s["started"])
    return out


def overview() -> dict:
    return {"projects": list_projects(), "sessions": running_sessions(),
            "terminal_available": bool(shutil.which(TERMINAL))}


async def start(path: str, name: str, continue_conversation: bool) -> dict:
    if not os.path.isdir(path):
        return {"ok": False, "error": "That folder doesn't exist any more."}
    if not NAME_RE.match(name):
        return {"ok": False, "error": "Use letters, numbers, dot, dash or underscore (max 40 characters)."}
    if not shutil.which(TERMINAL):
        return {"ok": False, "error": f"{TERMINAL} isn't installed on the laptop."}
    if not os.environ.get("DISPLAY"):
        return {"ok": False, "error": "No desktop session to open a window in (DISPLAY is not set)."}

    marker = secrets.token_hex(6)
    flags = " --continue" if continue_conversation else ""
    # This gnome-terminal has no --title, so the shell sets the window title itself.
    # `exec bash` keeps the window open after Claude exits, like your own terminals.
    command = (f"printf '\\033]0;Claude: %s\\007' {shlex.quote(name)}; "
               f"cd {shlex.quote(path)} || exit 1; "
               f"{MARKER}={marker} claude{flags} --remote-control {shlex.quote(name)}; exec bash")
    argv = [TERMINAL, "--window", f"--working-directory={path}", "--", "bash", "-ic", command]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        _, err = await asyncio.wait_for(proc.communicate(), 20)
    except (OSError, asyncio.TimeoutError) as e:
        return {"ok": False, "error": f"Couldn't open a terminal window: {e}"}
    if proc.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip()[:300]
        return {"ok": False, "error": detail or f"{TERMINAL} exited with code {proc.returncode}."}

    for _ in range(40):  # wait up to ~10 s for Claude itself to appear
        await asyncio.sleep(0.25)
        for s in await asyncio.to_thread(running_sessions):
            if s["marker"] == marker:
                return {"ok": True, "session": s}
    return {"ok": True, "session": None,
            "warning": "The terminal window opened, but Claude hasn't appeared yet. Check the laptop screen."}


async def stop(pid: int) -> dict:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            if p.name() != "claude" or p.uids().real != os.getuid():
                return {"ok": False, "error": "That process isn't one of your Claude sessions."}
            cwd = p.cwd()
        sid = os.getsid(pid)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return {"ok": False, "error": "That session has already ended."}
    except (psutil.Error, OSError) as e:
        return {"ok": False, "error": f"Couldn't inspect that session: {e}"}
    if sid == os.getsid(os.getpid()):
        return {"ok": False, "error": "That session belongs to the dashboard itself."}
    await procs.end_session(sid, grace=0)
    return {"ok": True, "pid": pid, "cwd": cwd, "closed_at": time.time()}
