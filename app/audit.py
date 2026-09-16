"""Append-only audit log (JSON lines). Never pass terminal input here."""
import json
import logging
import os
import time

from . import config

log = logging.getLogger("home_dashboard.audit")


def audit(event: str, **fields) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}, ensure_ascii=False)
    log.info(line)
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(config.AUDIT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
    except OSError:
        log.exception("could not write audit log")
