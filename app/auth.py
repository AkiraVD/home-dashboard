"""Who gets in.

Everything: the request must come through `tailscale serve` from the allowed
Tailscale login (Serve sets the Tailscale-User-Login header; the app listens on
a Unix socket only this user and tailscaled can open, so nothing else can
forge it). WebSockets and POSTs must also carry our own Origin.

Terminal: additionally a TOTP-unlocked cookie, bound to the login and device.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

import pyotp

from . import config
from .aio import run_json

COOKIE_NAME = "hd_unlock"


@dataclass(frozen=True)
class Visitor:
    login: str
    ip: str  # the device's Tailscale IP, from Serve's X-Forwarded-For


def visitor(headers) -> Visitor | None:
    """`headers` is a case-insensitive mapping (Starlette Headers)."""
    if not config.ALLOWED_LOGIN:
        return None
    login = headers.get("tailscale-user-login", "")
    if not hmac.compare_digest(login.encode(), config.ALLOWED_LOGIN.encode()):
        return None
    ip = headers.get("x-forwarded-for", "").split(",")[0].strip()
    return Visitor(login, ip)


_ORIGINS_TTL = 60.0
_ORIGINS_RETRY = 5.0  # on an unknown Origin, re-read Serve at most this often
_origins: frozenset[str] = frozenset()
_origins_at = float("-inf")


async def _serve_origins() -> frozenset[str]:
    """Every https origin where Tailscale Serve proxies to our socket."""
    serve = await run_json("tailscale", "serve", "status", "--json", default="{}", timeout=5)
    target = f"unix:{config.SOCKET_PATH}"
    out = set()
    for hostport, web in (serve.get("Web") or {}).items():
        if any(h.get("Proxy") == target for h in (web.get("Handlers") or {}).values()):
            host, _, port = hostport.rpartition(":")
            out.add(f"https://{host}" if port == "443" else f"https://{host}:{port}")
    return frozenset(out)


async def allowed_origins(refresh: bool = False) -> frozenset[str]:
    global _origins, _origins_at
    if config.PUBLIC_ORIGINS:
        return config.PUBLIC_ORIGINS
    age = time.monotonic() - _origins_at
    if age > _ORIGINS_TTL or (refresh and age > _ORIGINS_RETRY):
        _origins_at = time.monotonic()
        try:
            _origins = await _serve_origins()
        except (OSError, asyncio.TimeoutError, ValueError):
            pass  # keep the last known set
    return _origins


async def origin_ok(headers) -> bool:
    origin = headers.get("origin", "")
    if not origin:
        return False
    if origin in await allowed_origins():
        return True
    return origin in await allowed_origins(refresh=True)  # Serve may have just been re-pointed


_device_cache: dict[str, tuple[float, str]] = {}


async def device_name(ip: str) -> str:
    if not ip:
        return "unknown device"
    hit = _device_cache.get(ip)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    name = ip
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "whois", "--json", ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        node = json.loads(out)["Node"]
        name = node.get("ComputedName") or node.get("Name", "").split(".")[0] or ip
    except (OSError, asyncio.TimeoutError, ValueError, KeyError):
        pass
    _device_cache[ip] = (time.monotonic() + 600, name)
    return name


# ---------- secrets ----------

def totp_secret() -> str | None:
    try:
        return config.TOTP_KEY_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _cookie_key() -> bytes:
    try:
        return config.COOKIE_KEY_FILE.read_bytes()
    except FileNotFoundError:
        pass
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(config.COOKIE_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return config.COOKIE_KEY_FILE.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    # Mixing in the TOTP secret means re-running totp_setup signs every device out.
    key = hmac.new(_cookie_key(), secret.encode(), hashlib.sha256).digest()
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def make_unlock_cookie(v: Visitor, secret: str) -> str:
    payload = _b64(json.dumps({"l": v.login, "ip": v.ip, "exp": int(time.time()) + config.UNLOCK_SECONDS}).encode())
    return f"{payload}.{_sign(payload, secret)}"


def unlock_valid(cookie: str | None, v: Visitor, secret: str) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig.encode(), _sign(payload, secret).encode()):
        return False
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return False
    return data.get("l") == v.login and data.get("ip") == v.ip and data.get("exp", 0) > time.time()


class TotpGate:
    """Verifies codes with replay protection and a lockout after repeated failures."""

    def __init__(self):
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._last_counter = -1

    def lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - time.time() + 0.999))

    def verify(self, secret: str, code: str) -> bool:
        now = time.time()
        code = "".join(code.split())
        totp = pyotp.TOTP(secret)
        base = int(now // totp.interval)
        if len(code) == 6 and code.isdigit():
            for counter in (base, base - 1, base + 1):
                if hmac.compare_digest(totp.generate_otp(counter), code) and counter > self._last_counter:
                    self._last_counter = counter
                    self._fails.clear()
                    return True
        self._fails = [t for t in self._fails if now - t < config.TOTP_LOCKOUT_SECONDS]
        self._fails.append(now)
        if len(self._fails) >= config.TOTP_MAX_FAILS:
            self._locked_until = now + config.TOTP_LOCKOUT_SECONDS
            self._fails.clear()
        return False
