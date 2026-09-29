"""HTTP + WebSocket app. Reachable only through `tailscale serve` (see __main__.py)."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from . import auth, claude_sessions, config, terminal
from .aio import race
from .audit import audit
from .hub import Hub

log = logging.getLogger("home_dashboard")
hub = Hub()
gate = auth.TotpGate()

SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
     b"connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-cache"),
]


class TailscaleGate:
    """Refuses everyone but the allowed Tailscale user; adds security headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if auth.visitor(Headers(scope=scope)) is None:
            log.warning("refused %s %s: not the allowed Tailscale user", scope["type"], scope.get("path"))
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"), *SECURITY_HEADERS]})
                await send({"type": "http.response.body", "body": b"Forbidden\n"})
            return
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                message["headers"] = headers + [h for h in SECURITY_HEADERS if h[0] not in present]
            await send(message)

        return await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(app):
    if not config.ALLOWED_LOGIN:
        log.error("HD_ALLOWED_LOGIN is not set: every request will be refused")
    origins = await auth.allowed_origins()
    log.info("allowed origins: %s", ", ".join(sorted(origins)) or "none yet (not published with tailscale serve?)")
    task = asyncio.create_task(hub.run())
    try:
        yield
    finally:
        task.cancel()
        terminal.close_all()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TailscaleGate)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/api/session")
async def session(request: Request):
    v = auth.visitor(request.headers)
    secret = auth.totp_secret()
    return {
        "login": v.login,
        "device": await auth.device_name(v.ip),
        "totp_configured": bool(secret),
        "unlocked": bool(secret) and auth.unlock_valid(request.cookies.get(auth.COOKIE_NAME), v, secret),
        "lockout_seconds": gate.lockout_remaining(),
        "idle_minutes": config.TERMINAL_IDLE_SECONDS // 60,
        "setup_command": f"cd {config.ROOT} && ./venv/bin/python -m app.totp_setup",
    }


@app.post("/api/unlock")
async def unlock(request: Request):
    if not await auth.origin_ok(request.headers):
        return JSONResponse({"ok": False, "error": "bad_origin"}, 403)
    v = auth.visitor(request.headers)
    secret = auth.totp_secret()
    if not secret:
        return JSONResponse({"ok": False, "error": "not_configured"}, 409)
    remaining = gate.lockout_remaining()
    if remaining:
        return JSONResponse({"ok": False, "error": "locked", "retry_after": remaining}, 429)
    try:
        code = str((await request.json()).get("code", ""))
    except (ValueError, AttributeError):
        return JSONResponse({"ok": False, "error": "bad_request"}, 400)

    device = await auth.device_name(v.ip)
    if not gate.verify(secret, code):
        remaining = gate.lockout_remaining()
        audit("totp_fail", login=v.login, device=device, ip=v.ip)
        if remaining:
            audit("totp_lockout", login=v.login, device=device, ip=v.ip, seconds=remaining)
            return JSONResponse({"ok": False, "error": "locked", "retry_after": remaining}, 429)
        return JSONResponse({"ok": False, "error": "invalid"}, 401)

    audit("totp_ok", login=v.login, device=device, ip=v.ip)
    resp = JSONResponse({"ok": True, "expires_in": config.UNLOCK_SECONDS})
    resp.set_cookie(auth.COOKIE_NAME, auth.make_unlock_cookie(v, secret), max_age=config.UNLOCK_SECONDS,
                    path="/", secure=True, httponly=True, samesite="strict")
    return resp


@app.post("/api/lock")
async def lock(request: Request):
    if not await auth.origin_ok(request.headers):
        return JSONResponse({"ok": False, "error": "bad_origin"}, 403)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return resp


@app.get("/api/claude")
async def claude_overview():
    return await asyncio.to_thread(claude_sessions.overview)


@app.post("/api/claude/start")
async def claude_start(request: Request):
    """Opens a terminal window on the laptop running claude --remote-control.

    Tailscale identity + same-origin only: the user chose no second factor here,
    since this starts and stops sessions rather than changing anything.
    """
    if not await auth.origin_ok(request.headers):
        return JSONResponse({"ok": False, "error": "bad_origin"}, 403)
    try:
        body = await request.json()
    except (ValueError, AttributeError):
        return JSONResponse({"ok": False, "error": "bad_request"}, 400)

    projects = await asyncio.to_thread(claude_sessions.list_projects)
    project = next((p for p in projects if p["id"] == str(body.get("project", ""))), None)
    if not project or not project["path"]:
        return JSONResponse({"ok": False, "error": "Unknown project folder."}, 404)
    name = (str(body.get("name", "")).strip() or project["name"])
    # --continue with nothing to continue makes Claude quit at once, leaving an empty shell.
    keep_going = bool(body.get("continue", True)) and project["can_continue"]

    result = await claude_sessions.start(project["path"], name, keep_going)
    v = auth.visitor(request.headers)
    audit("claude_start", login=v.login, device=await auth.device_name(v.ip), path=project["path"],
          name=name, continued=keep_going, ok=result["ok"],
          pid=(result.get("session") or {}).get("pid"), error=result.get("error"))
    return JSONResponse(result, 200 if result["ok"] else 400)


@app.post("/api/claude/stop")
async def claude_stop(request: Request):
    if not await auth.origin_ok(request.headers):
        return JSONResponse({"ok": False, "error": "bad_origin"}, 403)
    try:
        pid = int((await request.json()).get("pid"))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "error": "bad_request"}, 400)

    result = await claude_sessions.stop(pid)
    v = auth.visitor(request.headers)
    audit("claude_stop", login=v.login, device=await auth.device_name(v.ip), pid=pid,
          ok=result["ok"], path=result.get("cwd"), error=result.get("error"))
    return JSONResponse(result, 200 if result["ok"] else 400)


@app.websocket("/ws/stats")
async def ws_stats(ws: WebSocket):
    if not await auth.origin_ok(ws.headers):
        await ws.close(code=1008)
        return
    await ws.accept()
    q = hub.subscribe()

    async def pump():
        await ws.send_text(hub.init_message())
        while True:
            await ws.send_text(await q.get())

    async def drain():
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    try:
        await race(pump(), drain())
    finally:
        hub.unsubscribe(q)


@app.websocket("/ws/term")
async def ws_term(ws: WebSocket):
    if not await auth.origin_ok(ws.headers):
        await ws.close(code=1008)
        return
    v = auth.visitor(ws.headers)
    secret = auth.totp_secret()
    await ws.accept()
    if not secret or not auth.unlock_valid(ws.cookies.get(auth.COOKIE_NAME), v, secret):
        await ws.close(code=4401)  # the page shows the unlock form again
        return
    await terminal.serve(ws, v, await auth.device_name(v.ip))
