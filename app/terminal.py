"""Web terminal: a login shell on a pseudo-terminal, bridged to a WebSocket.

Protocol: binary frames carry terminal bytes both ways; text frames carry JSON
control messages ({"type": "resize", "cols", "rows"} from the browser;
{"type": "exit" | "closed" | "error", ...} from us).

Closing the WebSocket ends the shell and everything started in its session.
Terminal input is never logged.
"""
import asyncio
import fcntl
import json
import os
import pwd
import secrets
import struct
import subprocess
import termios
import time

from starlette.websockets import WebSocket, WebSocketState

from . import config, procs
from .aio import race
from .audit import audit
from .auth import Visitor

_sessions: dict[str, "Session"] = {}

_ENV_DROP = {"INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES",
             "MANAGERPID", "SYSTEMD_EXEC_PID", "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE",
             "PYTHONUNBUFFERED", "RUNTIME_DIRECTORY"}


def _shell_env(user: pwd.struct_passwd) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP and not k.startswith("HD_")}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", HOME=user.pw_dir, USER=user.pw_name,
               LOGNAME=user.pw_name, SHELL=user.pw_shell or "/bin/bash")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class Session:
    def __init__(self, cols: int, rows: int):
        user = pwd.getpwuid(os.getuid())
        master, slave = os.openpty()
        try:
            _set_winsize(slave, cols, rows)
            # `setsid --ctty` makes the pty the shell's controlling terminal, which
            # sudo needs to prompt for a password (and bash needs for job control).
            self.proc = subprocess.Popen(
                ["setsid", "--ctty", user.pw_shell or "/bin/bash", "-l"],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=user.pw_dir, env=_shell_env(user), close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.fd: int | None = master
        self._pending = bytearray()
        self._writing = False

    def write(self, data: bytes) -> None:
        if self.fd is None or len(self._pending) > 4 << 20:  # 4 MiB backlog: drop the paste
            return
        self._pending += data
        self._flush()

    def _flush(self) -> None:
        if self.fd is None:
            return
        try:
            while self._pending:
                n = os.write(self.fd, self._pending)
                del self._pending[:n]
        except BlockingIOError:
            pass
        except OSError:
            self._pending.clear()
        loop = asyncio.get_running_loop()
        if self._pending and not self._writing:
            loop.add_writer(self.fd, self._flush)
            self._writing = True
        elif not self._pending and self._writing:
            loop.remove_writer(self.fd)
            self._writing = False

    def resize(self, cols: int, rows: int) -> None:
        if self.fd is not None:
            _set_winsize(self.fd, cols, rows)  # the kernel sends SIGWINCH

    def close(self) -> None:
        if self.fd is None:
            return
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.fd)
        if self._writing:
            loop.remove_writer(self.fd)
        os.close(self.fd)  # hangs up the terminal: the shell gets SIGHUP
        self.fd = None
        loop.create_task(self._reap())

    async def _reap(self) -> None:
        """Make sure nothing started in this shell's session outlives it."""
        self.proc.poll()  # reap the shell as soon as it has exited
        # setsid didn't need to fork, so the shell leads its own session
        await procs.end_session(self.proc.pid)
        await asyncio.to_thread(self.proc.wait)


def close_all() -> None:
    for s in list(_sessions.values()):
        s.close()


async def serve(ws: WebSocket, v: Visitor, device: str) -> None:
    if len(_sessions) >= config.TERMINAL_MAX_SESSIONS:
        await ws.send_text(json.dumps({"type": "error", "message": "Too many open terminal sessions."}))
        await ws.close(code=4429)
        return

    cols = _clamp(ws.query_params.get("cols"), 10, 500, 80)
    rows = _clamp(ws.query_params.get("rows"), 4, 200, 24)
    try:
        sess = Session(cols, rows)
    except OSError as e:
        await ws.send_text(json.dumps({"type": "error", "message": f"Could not start a shell: {e}"}))
        await ws.close(code=1011)
        return

    sid = secrets.token_hex(4)
    _sessions[sid] = sess
    started = time.monotonic()
    audit("terminal_open", session=sid, login=v.login, device=device, ip=v.ip, pid=sess.proc.pid)

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    state = {"paused": False, "last_activity": time.monotonic()}

    def on_readable():
        try:
            data = os.read(sess.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the shell has exited
            data = b""
        if not data:
            loop.remove_reader(sess.fd)
            out_q.put_nowait(None)
            return
        out_q.put_nowait(data)
        if out_q.qsize() > 64:  # the browser can't keep up: stop reading, the pty blocks the program
            loop.remove_reader(sess.fd)
            state["paused"] = True

    loop.add_reader(sess.fd, on_readable)

    async def pump_out():
        while True:
            chunk = await out_q.get()
            eof = chunk is None
            buf = bytearray(chunk or b"")
            while not eof and not out_q.empty() and len(buf) < 256 << 10:
                nxt = out_q.get_nowait()
                eof = nxt is None
                buf += nxt or b""
            if buf:
                await ws.send_bytes(bytes(buf))
                state["last_activity"] = time.monotonic()
            if eof:
                return "exit"
            if state["paused"] and out_q.qsize() < 8 and sess.fd is not None:
                state["paused"] = False
                loop.add_reader(sess.fd, on_readable)

    async def pump_in():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "client_closed"
            if msg.get("bytes") is not None:
                sess.write(msg["bytes"])
                state["last_activity"] = time.monotonic()
            elif msg.get("text"):
                try:
                    ctl = json.loads(msg["text"])
                except ValueError:
                    continue
                if ctl.get("type") == "resize":
                    sess.resize(_clamp(ctl.get("cols"), 10, 500, cols), _clamp(ctl.get("rows"), 4, 200, rows))

    async def idle_watch():
        while True:
            await asyncio.sleep(15)
            if time.monotonic() - state["last_activity"] > config.TERMINAL_IDLE_SECONDS:
                return "idle_timeout"

    exit_code = None
    reason, err = await race(pump_out(), pump_in(), idle_watch())
    if err is not None:
        reason = "error"
    try:
        if reason == "exit":
            exit_code = await asyncio.to_thread(sess.proc.wait)
            await _send_final(ws, {"type": "exit", "code": exit_code})
        elif reason == "idle_timeout":
            await _send_final(ws, {"type": "closed", "reason": "idle",
                                   "minutes": config.TERMINAL_IDLE_SECONDS // 60})
    finally:
        sess.close()
        _sessions.pop(sid, None)
        audit("terminal_close", session=sid, login=v.login, device=device, reason=reason,
              exit_code=exit_code, duration_s=round(time.monotonic() - started))


async def _send_final(ws: WebSocket, message: dict) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_text(json.dumps(message))
        await ws.close(code=1000)
    except Exception:  # the browser may already be gone
        pass
