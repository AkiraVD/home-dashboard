"""Entry point: `python -m app`.

Listens on a Unix socket readable only by this user (tailscaled, as root, can
still connect). uvicorn's own --uds would chmod the socket 0666, so we bind it
ourselves and hand it over.
"""
import logging
import os
import socket

import uvicorn

from . import config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    path = config.SOCKET_PATH
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    logging.getLogger("home_dashboard").info("listening on unix:%s", path)

    server = uvicorn.Server(uvicorn.Config(
        "app.main:app",
        log_level="info",
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=3,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    ))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
