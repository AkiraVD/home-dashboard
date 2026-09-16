"""Paths, limits and environment-driven settings."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

_home = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or _home / ".config") / "home-dashboard"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or _home / ".local" / "state") / "home-dashboard"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

SOCKET_PATH = Path(os.environ.get("HD_SOCKET") or RUNTIME_DIR / "home-dashboard" / "dash.sock")
TOTP_KEY_FILE = CONFIG_DIR / "totp.key"
COOKIE_KEY_FILE = CONFIG_DIR / "cookie.key"
AUDIT_LOG = STATE_DIR / "audit.log"

# The only Tailscale login allowed in. Unset means nobody (fail closed).
ALLOWED_LOGIN = os.environ.get("HD_ALLOWED_LOGIN", "").strip()
# Origins allowed for WebSockets/POSTs (comma-separated). Empty: every address where
# `tailscale serve` proxies to our socket, so changing the Serve port needs no restart.
PUBLIC_ORIGINS = frozenset(o.strip().rstrip("/") for o in os.environ.get("HD_PUBLIC_ORIGIN", "").split(",") if o.strip())

SAMPLE_INTERVAL = 2.0
HISTORY_SECONDS = 3600

UNLOCK_SECONDS = 12 * 3600
TOTP_MAX_FAILS = 5
TOTP_LOCKOUT_SECONDS = 15 * 60

TERMINAL_IDLE_SECONDS = 15 * 60
TERMINAL_MAX_SESSIONS = 4
