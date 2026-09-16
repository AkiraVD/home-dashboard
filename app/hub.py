"""Samples the system on a fixed beat, keeps 1h of history in memory and fans
updates out to every connected dashboard."""
import asyncio
import json
import logging
from collections import deque

from . import collectors, config

log = logging.getLogger("home_dashboard.hub")

HISTORY_KEYS = ("cpu", "mem", "swap", "gpu_util", "gpu_mem", "cpu_temp", "gpu_temp",
                "disk_r", "disk_w", "net_rx", "net_tx", "bat")


def history_point(s: dict) -> dict:
    gpu = s["gpu"]
    gpu_live = gpu.get("state") in ("active", "idle")
    return {
        "t": s["t"],
        "cpu": s["cpu"]["total"],
        "mem": s["mem"]["percent"],
        "swap": s["mem"]["swap_percent"] if s["mem"]["swap_total"] else None,
        "gpu_util": gpu.get("util") if gpu_live else None,
        "gpu_mem": gpu.get("mem_percent") if gpu_live else None,
        "cpu_temp": s["cpu"]["temp"],
        "gpu_temp": gpu.get("temp") if gpu_live else None,
        "disk_r": s["disk_io"]["read"],
        "disk_w": s["disk_io"]["write"],
        "net_rx": s["net"]["rx"],
        "net_tx": s["net"]["tx"],
        "bat": (s["battery"] or {}).get("percent"),
    }


class History:
    def __init__(self, maxlen: int):
        self.t = deque(maxlen=maxlen)
        self.cols = {k: deque(maxlen=maxlen) for k in HISTORY_KEYS}

    def append(self, point: dict) -> None:
        self.t.append(point["t"])
        for k in HISTORY_KEYS:
            self.cols[k].append(point[k])

    def to_json(self) -> dict:
        return {"t": list(self.t), **{k: list(v) for k, v in self.cols.items()}}


class Hub:
    def __init__(self):
        self.clients: set[asyncio.Queue] = set()
        self.history = History(int(config.HISTORY_SECONDS / config.SAMPLE_INTERVAL))
        self.host = collectors.host_info()
        self.fast = collectors.FastCollector()
        self.procs = collectors.ProcCollector()
        self.docker = collectors.DockerCollector()
        self.sample: dict | None = None
        self.latest: dict = {}  # procs / docker / system, while anyone watches
        self._want_full = True

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.clients.add(q)
        self._want_full = True
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def init_message(self) -> str:
        return json.dumps({
            "type": "init",
            "host": self.host,
            "interval": config.SAMPLE_INTERVAL,
            "history": self.history.to_json(),
            "sample": self.sample,
            **self.latest,
        }, separators=(",", ":"))

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        tick, next_at = 0, loop.time()
        while True:
            try:
                await self._step(tick)
            except Exception:
                log.exception("sampling failed")
            tick += 1
            next_at += config.SAMPLE_INTERVAL
            delay = next_at - loop.time()
            if delay < 0:  # fell behind: don't burst to catch up
                next_at, delay = loop.time(), 0
            await asyncio.sleep(delay)

    async def _step(self, tick: int) -> None:
        sample = await asyncio.to_thread(self.fast.collect)
        point = history_point(sample)
        self.history.append(point)
        self.sample = sample
        msg = {"type": "tick", "s": sample, "p": point}

        if self.clients:
            full, self._want_full = self._want_full, False
            jobs = {"procs": asyncio.to_thread(self.procs.collect)}
            if full or tick % 2 == 0:
                jobs["docker"] = self.docker.collect()
            if full or tick % 5 == 0:
                jobs["system"] = collectors.system_status()
            if full or tick % 15 == 0:
                jobs["sites"] = collectors.tailscale_sites(self.docker)
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            for key, res in zip(jobs, results):
                if isinstance(res, BaseException):
                    log.warning("%s collector failed: %r", key, res)
                    continue
                msg[key] = self.latest[key] = res
        else:
            self.latest.clear()  # don't greet the next visitor with stale tables

        text = json.dumps(msg, separators=(",", ":"))
        for q in list(self.clients):
            if q.full():  # slow client: drop its oldest update
                q.get_nowait()
            q.put_nowait(text)
