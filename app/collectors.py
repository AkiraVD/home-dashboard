"""Readers for system state.

The synchronous collectors run in a worker thread (see hub.py); Docker and
systemd are queried with async socket/subprocess calls.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import platform
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from . import config
from .aio import run_json

try:
    import pynvml
except ImportError:  # no NVIDIA library installed
    pynvml = None


def _read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _read_int(path):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return None


def host_info() -> dict:
    cpu_model = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        os_name = "Linux"
    return {
        "hostname": socket.gethostname(),
        "os": os_name,
        "kernel": platform.release(),
        "cpu_model": cpu_model,
        "cpus": psutil.cpu_count(),
        "boot_time": psutil.boot_time(),
    }


# ---------- fast sample: every tick, always (feeds the 1h history) ----------

class GpuCollector:
    """NVIDIA stats via NVML without keeping an on-demand (Optimus) GPU awake.

    An open NVML handle blocks runtime suspend, so: read the PCI power state
    first (sysfs doesn't wake the device), open NVML only for one query, and
    back off while the GPU is idle so it's free to power down.
    """

    IDLE_BACKOFF = 30.0

    def __init__(self):
        self.pci = self._find_pci()
        self.name = None
        self._skip_until = 0.0
        self._last = None

    @staticmethod
    def _find_pci():
        try:
            for dev in Path("/sys/bus/pci/devices").iterdir():
                if _read(dev / "vendor") == "0x10de" and (_read(dev / "class") or "").startswith("0x03"):
                    return dev
        except OSError:
            pass
        return None

    def collect(self, now: float) -> dict:
        if pynvml is None or self.pci is None:
            return {"state": "none"}
        if _read(self.pci / "power" / "runtime_status", "active") == "suspended":
            self._last = None
            return {"state": "asleep", "name": self.name}
        if self._last and now < self._skip_until:
            return {**self._last, "state": "idle"}
        try:
            data = self._query()
        except pynvml.NVMLError as e:
            return {"state": "error", "name": self.name, "error": str(e)}
        self._last = data
        if not data["procs"] and not data["util"]:
            self._skip_until = now + self.IDLE_BACKOFF
        return data

    def _query(self) -> dict:
        def opt(fn, *args):
            try:
                return fn(*args)
            except pynvml.NVMLError:
                return None

        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = opt(pynvml.nvmlDeviceGetName, h)
            if isinstance(name, bytes):
                name = name.decode()
            self.name = name or self.name
            util = opt(pynvml.nvmlDeviceGetUtilizationRates, h)
            mem = opt(pynvml.nvmlDeviceGetMemoryInfo, h)
            temp = opt(pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU)
            power = opt(pynvml.nvmlDeviceGetPowerUsage, h)
            used_by_pid = {}
            for fn in (pynvml.nvmlDeviceGetComputeRunningProcesses, pynvml.nvmlDeviceGetGraphicsRunningProcesses):
                for p in opt(fn, h) or []:
                    used_by_pid[p.pid] = p.usedGpuMemory
        finally:
            pynvml.nvmlShutdown()

        procs = []
        for pid, used in used_by_pid.items():
            try:
                pname = psutil.Process(pid).name()
            except psutil.Error:
                pname = "?"
            procs.append({"pid": pid, "name": pname, "mem": used})
        return {
            "state": "active",
            "name": self.name,
            "util": util.gpu if util else None,
            "mem_used": mem.used if mem else None,
            "mem_total": mem.total if mem else None,
            "mem_percent": round(mem.used / mem.total * 100, 1) if mem and mem.total else None,
            "temp": temp,
            "power_w": round(power / 1000, 1) if power is not None else None,
            "procs": procs,
        }


def _cpu_temp_inputs() -> list[tuple[str, Path]]:
    """(label, path) of the CPU sensor's hwmon inputs. Reading these directly is
    ~10x cheaper than psutil.sensors_temperatures(), which reads every sensor."""
    try:
        hwmons = sorted(Path("/sys/class/hwmon").iterdir())
    except OSError:
        return []
    for hw in hwmons:
        if _read(hw / "name") in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            inputs = sorted(hw.glob("temp*_input"), key=lambda p: int(p.name[4:-6] or 0))
            return [(_read(p.with_name(p.name.replace("_input", "_label")), ""), p) for p in inputs]
    return []


def _cpu_temps(inputs: list[tuple[str, Path]]):
    pkg, cores, other = None, [], []
    for label, path in inputs:
        value = _read_int(path)
        if value is None:
            continue
        celsius = round(value / 1000, 1)
        if label.startswith(("Package", "Tctl", "Tdie")):
            pkg = celsius
        elif label.startswith("Core"):
            cores.append(celsius)
        else:
            other.append(celsius)
    if pkg is None and (cores or other):
        pkg = max(cores or other)
    return pkg, cores


def _battery():
    base = Path("/sys/class/power_supply")
    try:
        supplies = list(base.iterdir())
    except OSError:
        return None
    bats = [p for p in supplies if _read(p / "type") == "Battery"]
    if not bats:
        return None
    b = bats[0]
    status = _read(b / "status", "Unknown")
    plugged = any(_read(p / "online") == "1" for p in supplies if _read(p / "type") == "Mains")
    power_uw = _read_int(b / "power_now")
    current, voltage = _read_int(b / "current_now"), _read_int(b / "voltage_now")
    if power_uw is None and current is not None and voltage is not None:
        power_uw = current * voltage / 1e6
    # Remaining time: µAh / µA or µWh / µW gives hours.
    level, full, rate = None, None, None
    if _read_int(b / "charge_now") is not None and current:
        level, full, rate = _read_int(b / "charge_now"), _read_int(b / "charge_full"), abs(current)
    elif _read_int(b / "energy_now") is not None and power_uw:
        level, full, rate = _read_int(b / "energy_now"), _read_int(b / "energy_full"), abs(power_uw)
    secs = None
    if rate and level is not None:
        if status == "Discharging":
            secs = level / rate * 3600
        elif status == "Charging" and full:
            secs = max(full - level, 0) / rate * 3600
    return {
        "percent": _read_int(b / "capacity"),
        "status": status,
        "plugged": plugged,
        "watts": round(abs(power_uw) / 1e6, 1) if power_uw is not None else None,
        "secs": int(secs) if secs else None,
    }


def _physical(sys_dir: str) -> set[str]:
    try:
        return {n for n in os.listdir(sys_dir) if os.path.exists(f"{sys_dir}/{n}/device")}
    except OSError:
        return set()


class FastCollector:
    def __init__(self):
        psutil.cpu_percent(percpu=True)  # prime: the first call has no baseline
        self.gpu = GpuCollector()
        self._temp_inputs = _cpu_temp_inputs()
        self._prev_t = time.monotonic()
        self._prev_disk = psutil.disk_io_counters(perdisk=True) or {}
        self._prev_net = psutil.net_io_counters(pernic=True)

    def collect(self) -> dict:
        now = time.monotonic()
        dt = max(now - self._prev_t, 1e-3)
        self._prev_t = now

        cores = psutil.cpu_percent(percpu=True)
        freq = psutil.cpu_freq()
        pkg_temp, core_temps = _cpu_temps(self._temp_inputs)

        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        used = vm.total - vm.available

        disks, seen = [], set()
        for p in psutil.disk_partitions(all=False):
            if (not p.device.startswith("/dev/") or p.device.startswith("/dev/loop")
                    or p.device in seen or p.mountpoint == "/boot/efi" or p.fstype == "squashfs"):
                continue
            seen.add(p.device)
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            disks.append({"mount": p.mountpoint, "device": p.device, "total": u.total, "used": u.used,
                          "percent": round(u.percent, 1)})

        disk_now = psutil.disk_io_counters(perdisk=True) or {}
        read = write = 0.0
        for name in _physical("/sys/block"):
            cur, prev = disk_now.get(name), self._prev_disk.get(name)
            if cur and prev:
                read += max(cur.read_bytes - prev.read_bytes, 0) / dt
                write += max(cur.write_bytes - prev.write_bytes, 0) / dt
        self._prev_disk = disk_now

        net_now = psutil.net_io_counters(pernic=True)
        phys_nics = _physical("/sys/class/net")
        ifaces, rx_total, tx_total = [], 0.0, 0.0
        for name, c in net_now.items():
            if name == "lo" or name.startswith("veth"):
                continue
            prev = self._prev_net.get(name)
            rx = max(c.bytes_recv - prev.bytes_recv, 0) / dt if prev else 0.0
            tx = max(c.bytes_sent - prev.bytes_sent, 0) / dt if prev else 0.0
            physical = name in phys_nics
            if physical:  # tailscale/docker traffic already crosses a physical NIC
                rx_total += rx
                tx_total += tx
            flags = _read(f"/sys/class/net/{name}/flags", "0x0")
            up = bool(int(flags, 16) & 0x1)  # IFF_UP (operstate is "unknown" for tun devices)
            ifaces.append({"name": name, "rx": round(rx), "tx": round(tx), "up": up, "physical": physical})
        self._prev_net = net_now

        return {
            "t": round(time.time(), 1),
            "uptime": int(time.time() - psutil.boot_time()),
            "cpu": {
                "total": round(sum(cores) / len(cores), 1),
                "cores": cores,
                "load": [round(x, 2) for x in os.getloadavg()],
                "freq_mhz": round(freq.current) if freq else None,
                "temp": pkg_temp,
                "core_temps": core_temps,
            },
            "mem": {
                "total": vm.total, "used": used, "available": vm.available,
                "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
                "percent": round(used / vm.total * 100, 1),
                "swap_total": sw.total, "swap_used": sw.used, "swap_percent": round(sw.percent, 1),
            },
            "gpu": self.gpu.collect(now),
            "disks": disks,
            "disk_io": {"read": round(read), "write": round(write)},
            "net": {"rx": round(rx_total), "tx": round(tx_total), "ifaces": ifaces},
            "battery": _battery(),
        }


# ---------- heavier views: only while someone is watching ----------

class ProcCollector:
    """Process table. psutil keeps Process objects between calls, so CPU % is
    measured over the tick; name/user/cmdline are cached per (pid, start time)."""

    def __init__(self):
        self._static: dict[tuple[int, float], tuple[str, str, str]] = {}

    def collect(self) -> dict:
        rows, seen = [], set()
        for p in psutil.process_iter():
            try:
                with p.oneshot():
                    if p.pid == 2 or p.ppid() == 2:  # kernel threads
                        continue
                    key = (p.pid, p.create_time())
                    static = self._static.get(key)
                    if static is None:
                        try:
                            user = p.username()
                        except (KeyError, psutil.Error):
                            user = "?"
                        try:
                            cmd = " ".join(p.cmdline())[:400]
                        except psutil.AccessDenied:
                            cmd = ""
                        static = self._static[key] = (p.name(), user, cmd)
                    rows.append([p.pid, static[0], static[1], round(p.cpu_percent(), 1),
                                 p.memory_info().rss, p.status(), static[2]])
                    seen.add(key)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self._static = {k: v for k, v in self._static.items() if k in seen}
        return {"rows": rows}


class DockerCollector:
    SOCK = "/var/run/docker.sock"

    def __init__(self):
        self._prev_cpu: dict[str, tuple[int, int]] = {}

    async def _get(self, path: str):
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.SOCK), 3)
        try:
            # HTTP/1.0: no chunked encoding, the daemon closes when done.
            writer.write(f"GET {path} HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 10)
        finally:
            writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        if status != 200:
            raise RuntimeError(f"Docker API {path.split('?')[0]} returned HTTP {status}")
        return json.loads(body)

    async def collect(self) -> dict:
        if not os.path.exists(self.SOCK):
            return {"available": False, "error": "Docker isn't running (no docker.sock)."}
        try:
            containers = await self._get("/containers/json?all=1")
        except PermissionError:
            return {"available": False, "error": "No permission to read docker.sock."}
        except (OSError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
            return {"available": False, "error": f"Docker query failed: {e}"}

        running = [c for c in containers if c.get("State") == "running"]
        stats = await asyncio.gather(
            *(self._get(f"/containers/{c['Id']}/stats?stream=false&one-shot=true") for c in running),
            return_exceptions=True,
        )
        stats_by_id = {c["Id"]: s for c, s in zip(running, stats) if isinstance(s, dict)}

        rows, prev_cpu = [], {}
        for c in containers:
            row = {
                "id": c["Id"][:12],
                "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                "image": c.get("Image", ""),
                "state": c.get("State", ""),
                "status": c.get("Status", ""),
                "cpu": None, "mem": None, "mem_limit": None,
            }
            s = stats_by_id.get(c["Id"])
            if s:
                cpu = s.get("cpu_stats", {})
                total, system = cpu.get("cpu_usage", {}).get("total_usage"), cpu.get("system_cpu_usage")
                if total is not None and system is not None:
                    prev = self._prev_cpu.get(c["Id"])
                    if prev and system > prev[1]:
                        ncpu = cpu.get("online_cpus") or psutil.cpu_count()
                        row["cpu"] = round(max(total - prev[0], 0) / (system - prev[1]) * ncpu * 100, 1)
                    prev_cpu[c["Id"]] = (total, system)
                mem = s.get("memory_stats", {})
                if mem.get("usage") is not None:
                    inactive = mem.get("stats", {}).get("inactive_file", 0)  # same as `docker stats`
                    row["mem"] = max(mem["usage"] - inactive, 0)
                    row["mem_limit"] = mem.get("limit")
            rows.append(row)
        self._prev_cpu = prev_cpu
        return {"available": True, "containers": rows}

    async def published_ports(self) -> dict[int, str]:
        """Host port -> name of the running container that publishes it."""
        if not os.path.exists(self.SOCK):
            return {}
        try:
            containers = await self._get("/containers/json")
        except (OSError, asyncio.TimeoutError, RuntimeError, ValueError):
            return {}
        out = {}
        for c in containers:
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            for p in c.get("Ports") or []:
                if p.get("PublicPort"):
                    out[p["PublicPort"]] = name
        return out


async def _systemctl(*args):
    return await run_json("systemctl", *args, "--output=json", "--no-pager")


async def _services() -> dict:
    queries = {
        ("system", "failed"): ("list-units", "--state=failed", "--all"),
        ("user", "failed"): ("--user", "list-units", "--state=failed", "--all"),
        ("system", "running"): ("list-units", "--type=service", "--state=running"),
        ("user", "running"): ("--user", "list-units", "--type=service", "--state=running"),
    }
    results = await asyncio.gather(*(_systemctl(*a) for a in queries.values()), return_exceptions=True)
    out = {"failed": [], "running": {"system": [], "user": []}, "errors": []}
    for (scope, kind), res in zip(queries, results):
        if isinstance(res, BaseException):
            out["errors"].append(f"systemctl ({scope}, {kind}) failed: {res}")
            continue
        rows = [{"unit": u.get("unit", "?"), "scope": scope, "description": u.get("description", ""),
                 "sub": u.get("sub", "")} for u in res]
        if kind == "failed":
            out["failed"].extend(rows)
        else:
            out["running"][scope] = rows
    return out


def _listening_ports() -> list[dict]:
    keys = set()
    for c in psutil.net_connections(kind="inet"):
        tcp = c.type == socket.SOCK_STREAM
        if (tcp and c.status != psutil.CONN_LISTEN) or (not tcp and c.raddr):
            continue
        proto = ("tcp" if tcp else "udp") + ("6" if c.family == socket.AF_INET6 else "")
        keys.add((proto, c.laddr.ip, c.laddr.port, c.pid))
    names: dict[int, str | None] = {}
    out = []
    for proto, ip, port, pid in sorted(keys, key=lambda k: (k[2], k[0], k[1])):
        if pid and pid not in names:
            try:
                names[pid] = psutil.Process(pid).name()
            except psutil.Error:
                names[pid] = None
        out.append({"proto": proto, "ip": ip, "port": port, "pid": pid, "process": names.get(pid)})
    return out


async def system_status() -> dict:
    services, ports = await asyncio.gather(_services(), asyncio.to_thread(_listening_ports))
    return {"services": services, "ports": ports}


# ---------- sites published with `tailscale serve` ----------

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


def _probe_error(e: BaseException) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused (nothing listening)"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "No response within 3 s"
    if isinstance(e, FileNotFoundError):
        return "Socket not found"
    return str(e) or type(e).__name__


async def _probe(target: str, timeout: float = 3.0) -> dict:
    """GET a Serve backend once (no redirects followed): status, latency, page title."""
    started = time.monotonic()
    try:
        if target.startswith("unix:"):
            opener, host, path = asyncio.open_unix_connection(target[5:]), "localhost", "/"
        else:
            insecure = target.startswith("https+insecure://")
            u = urlsplit(target.replace("https+insecure://", "https://", 1))
            ctx = None
            if u.scheme == "https":
                ctx = ssl.create_default_context()
                if insecure:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
            port = u.port or (443 if u.scheme == "https" else 80)
            opener, host, path = asyncio.open_connection(u.hostname, port, ssl=ctx), u.netloc, u.path or "/"
        reader, writer = await asyncio.wait_for(opener, timeout)
        try:
            writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: home-dashboard\r\n"
                         f"Accept: text/html\r\n\r\n".encode())
            await writer.drain()
            data = b""
            while len(data) < 65536:
                chunk = await asyncio.wait_for(reader.read(65536), timeout)
                if not chunk:
                    break
                data += chunk
        finally:
            writer.close()
    except (OSError, asyncio.TimeoutError, ssl.SSLError, ValueError) as e:
        return {"up": False, "error": _probe_error(e)}

    head, _, body = data.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return {"up": False, "error": "Not an HTTP response"}
    m = _TITLE_RE.search(body)
    title = " ".join(html.unescape(m.group(1).decode("utf-8", "replace")).split())[:120] if m else ""
    return {"up": status < 500, "status": status, "ms": round((time.monotonic() - started) * 1000),
            "title": title or None}


async def _port_owners(docker: DockerCollector) -> dict[int, str]:
    owners: dict[int, str] = {}
    for row in await asyncio.to_thread(_listening_ports):
        if row["process"] and row["proto"].startswith("tcp"):
            owners.setdefault(row["port"], row["process"])
    for port, name in (await docker.published_ports()).items():
        owners[port] = f"{name} (Docker)"
    return owners


async def tailscale_sites(docker: DockerCollector) -> dict:
    try:
        cfg = await run_json("tailscale", "serve", "status", "--json", default="{}")
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        return {"error": f"Couldn't read the Tailscale Serve config: {e}", "sites": []}

    funnel = cfg.get("AllowFunnel") or {}
    own_socket = f"unix:{config.SOCKET_PATH}"
    sites = []
    for hostport, web in (cfg.get("Web") or {}).items():
        host, _, port = hostport.rpartition(":")
        base = f"https://{host}" + ("" if port == "443" else f":{port}")
        for path, handler in sorted((web.get("Handlers") or {}).items()):
            site = {"url": base + path, "port": int(port), "public": bool(funnel.get(hostport)),
                    "probe": None, "name": None, "owner": None}
            if "Proxy" in handler:
                site.update(kind="proxy", target=handler["Proxy"])
            elif "Path" in handler:
                site.update(kind="files", target=handler["Path"])
            else:
                site.update(kind="text", target="Static text")
            sites.append(site)
    for port, tcp in (cfg.get("TCP") or {}).items():
        if tcp.get("TCPForward"):
            sites.append({"url": None, "port": int(port), "public": False, "kind": "tcp",
                          "target": tcp["TCPForward"], "probe": None, "name": None, "owner": None})

    owners = await _port_owners(docker)

    async def fill(site):
        target = site["target"]
        if site["kind"] != "proxy":
            return
        if target == own_socket:
            site.update(self=True, name="Home dashboard", probe={"up": True})
            return
        if not target.startswith("unix:"):
            try:
                site["owner"] = owners.get(urlsplit(target.replace("https+insecure://", "https://", 1)).port)
            except ValueError:
                pass
        site["probe"] = await _probe(target)
        site["name"] = site["probe"].get("title") or site["owner"]

    await asyncio.gather(*(fill(s) for s in sites))
    sites.sort(key=lambda s: (s["port"], s["url"] or ""))
    return {"sites": sites}
