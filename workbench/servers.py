"""The lab servers: their list (state/workbench/servers.json, set in Admin → Servers) and their load.

The monitor collects every server every `interval` seconds, all at once: a small Python program (COLLECTOR, run with
python3 on the server through remote.Target) samples /proc for one second and prints JSON: CPU (total, per core,
iowait), load, memory and swap, disk and network throughput, logged-in users, CPU and memory per account, the busiest
processes, GPUs (nvidia-smi) and file system usage. The monitor keeps the latest sample and two hours of history.
Servers are reached with the service account's SSH key (Admin → Servers installs it with a password, once).

Everyone signed in reads the summary (the analysis pages show it to choose a server); the details (accounts,
processes) are for administrators. Standard library only.
"""

import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import remote

def default_servers():
    """Until Admin → Servers sets the list (state/workbench/servers.json): this machine, in the service's account."""
    import getpass
    import socket
    name = socket.gethostname().split(".")[0].split("-")[0] or "this server"
    return [{"id": name.lower(), "name": name.upper(), "host": "127.0.0.1", "port": 22, "user": getpass.getuser()}]



COLLECTOR = r'''
import json, os, socket, subprocess, time
def cpu():
    out = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu"):
            p = line.split(); v = [int(x) for x in p[1:9]]
            out[p[0]] = (sum(v), v[3] + v[4], v[4])
    return out
def disk():
    r = w = 0
    names = set(os.listdir("/sys/block"))
    for line in open("/proc/diskstats"):
        p = line.split()
        if len(p) < 10 or p[2] not in names or p[2].startswith(("loop", "ram", "zram", "sr", "dm-", "md", "fd")):
            continue
        r += int(p[5]); w += int(p[9])
    return r * 512, w * 512
def net():
    rx = tx = 0
    for line in open("/proc/net/dev").readlines()[2:]:
        name, data = line.split(":", 1); name = name.strip(); d = data.split()
        if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr", "cni", "flannel", "tun", "tap")):
            continue
        rx += int(d[0]); tx += int(d[8])
    return rx, tx
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return ""
c1, d1, n1, t1 = cpu(), disk(), net(), time.time()
time.sleep(1.0)
c2, d2, n2, t2 = cpu(), disk(), net(), time.time()
dt = max(t2 - t1, 0.1)
def pct(k):
    tot = c2[k][0] - c1[k][0]
    return (round(100.0 * (1 - (c2[k][1] - c1[k][1]) / tot), 1), round(100.0 * (c2[k][2] - c1[k][2]) / tot, 1)) if tot > 0 else (0.0, 0.0)
cores = sorted((k for k in c2 if k != "cpu"), key=lambda k: int(k[3:]))
mem = {}
for line in open("/proc/meminfo"):
    k, v = line.split(":", 1); mem[k] = int(v.split()[0]) * 1024
users = {}
for line in run(["who"]).splitlines():
    p = line.split()
    if p:
        users.setdefault(p[0], {"user": p[0], "sessions": 0, "from": set()})
        users[p[0]]["sessions"] += 1
        if p[-1].startswith("("):
            users[p[0]]["from"].add(p[-1].strip("()"))
for u in users.values():
    u["from"] = sorted(u["from"])[:4]
accounts = {}
procs = []
for line in run(["ps", "-eo", "user:32,pid,pcpu,pmem,rss,etimes,comm", "--no-headers", "--sort=-pcpu"]).splitlines():
    p = line.split(None, 6)
    if len(p) < 7:
        continue
    a = accounts.setdefault(p[0], {"user": p[0], "cpu": 0.0, "rss": 0, "procs": 0})
    a["cpu"] += float(p[2]); a["rss"] += int(p[4]) * 1024; a["procs"] += 1
    if len(procs) < 15 and float(p[2]) >= 1:
        procs.append({"user": p[0], "pid": int(p[1]), "cpu": float(p[2]), "mem": float(p[3]), "rss": int(p[4]) * 1024, "elapsed": int(p[5]), "command": p[6]})
gpus = []
for line in run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"]).splitlines():
    p = [x.strip() for x in line.split(",")]
    if len(p) == 6:
        try:
            gpus.append({"index": int(p[0]), "name": p[1], "util": float(p[2]), "mem_used": float(p[3]) * 2**20, "mem_total": float(p[4]) * 2**20, "temp": float(p[5])})
        except ValueError:
            pass
fs = []
seen = set()
for line in open("/proc/mounts"):
    dev, mount, kind = line.split()[:3]
    if kind in ("ext4", "xfs", "btrfs", "zfs", "nfs", "nfs4", "lustre", "beegfs", "f2fs") and not mount.startswith(("/snap", "/boot", "/var/lib/docker", "/run")):
        if mount.startswith("/nfs/") and mount.count("/") > 2:
            continue
        try:
            s = os.statvfs(mount)
        except OSError:
            continue
        key = (s.f_blocks, s.f_bfree)
        if key in seen or s.f_blocks == 0:
            continue
        seen.add(key)
        fs.append({"mount": mount, "kind": kind, "size": s.f_blocks * s.f_frsize, "free": s.f_bavail * s.f_frsize})
model = ""
for line in open("/proc/cpuinfo"):
    if line.startswith("model name"):
        model = line.split(":", 1)[1].strip(); break
total, iowait = pct("cpu")
print(json.dumps({
    "hostname": socket.gethostname(), "time": time.time(), "uptime": float(open("/proc/uptime").read().split()[0]),
    "cpu_model": model, "ncpu": len(cores), "cpu": total, "iowait": iowait, "cores": [pct(k)[0] for k in cores],
    "load": list(os.getloadavg()),
    "mem_total": mem.get("MemTotal", 0), "mem_available": mem.get("MemAvailable", 0),
    "swap_total": mem.get("SwapTotal", 0), "swap_free": mem.get("SwapFree", 0),
    "disk_read": (d2[0] - d1[0]) / dt, "disk_write": (d2[1] - d1[1]) / dt,
    "net_rx": (n2[0] - n1[0]) / dt, "net_tx": (n2[1] - n1[1]) / dt,
    "users": sorted(users.values(), key=lambda u: -u["sessions"]),
    "accounts": sorted((a for a in accounts.values() if a["cpu"] >= 1 or a["rss"] > 2**30), key=lambda a: -a["cpu"])[:12],
    "processes": procs, "gpus": gpus, "filesystems": fs[:12],
}))
'''


class Servers:
    def __init__(self, state_dir, interval=20):
        self.file = Path(state_dir) / "servers.json"
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {}      # id -> {"ok", "time", "data" | "error"}
        self.history = {}     # id -> deque of compact samples
        self.list = self.load()
        self.wake = threading.Event()
        self.viewed = 0.0

    # ---------------------------------------------------------------- the list

    def load(self):
        try:
            items = json.loads(self.file.read_text())
            if isinstance(items, list) and items:
                return [self.clean(s) for s in items]
        except (OSError, ValueError):
            pass
        return [self.clean(s) for s in default_servers()]

    @staticmethod
    def clean(item):
        sid = "".join(c for c in str(item.get("id") or item.get("name") or "").lower() if c.isalnum() or c in "-_")[:32]
        if not sid or not item.get("host"):
            raise ValueError("Each server needs a name and a host")
        return {"id": sid, "name": str(item.get("name") or sid.upper())[:40], "host": str(item["host"]).strip()[:200],
                "port": int(item.get("port") or 22), "user": str(item.get("user") or "").strip()[:64],
                "enabled": item.get("enabled", True) is not False, "note": str(item.get("note") or "")[:200]}

    def save(self, items):
        cleaned = [self.clean(s) for s in items]
        ids = [s["id"] for s in cleaned]
        if len(set(ids)) != len(ids):
            raise ValueError("Two servers have the same name")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cleaned, indent=2))
        tmp.replace(self.file)
        with self.lock:
            self.list = cleaned
            for gone in set(self.latest) - set(ids):
                self.latest.pop(gone, None)
                self.history.pop(gone, None)
        self.wake.set()
        return cleaned

    def get(self, sid):
        for s in self.list:
            if s["id"] == sid:
                return s
        raise KeyError("Unknown server: {}".format(sid))

    def default(self):
        for s in self.list:
            if s["host"].lower() in remote.LOCAL:
                return s
        return self.list[0]

    # ---------------------------------------------------------------- collecting

    def collect(self, server):
        started = time.time()
        try:
            code, out, err = remote.Target(server).run("python3 -", input=COLLECTOR.encode(), timeout=30)
            if code != 0:
                raise remote.RemoteError((err or out).strip()[-300:] or "exit {}".format(code))
            data = json.loads(out.strip().splitlines()[-1])
            result = {"ok": True, "time": time.time(), "latency": round(time.time() - started, 2), "data": data}
        except (remote.RemoteError, ValueError, IndexError) as error:
            result = {"ok": False, "time": time.time(), "error": str(error)}
        with self.lock:
            self.latest[server["id"]] = result
            if result["ok"]:
                d = result["data"]
                h = self.history.setdefault(server["id"], deque(maxlen=int(7200 / self.interval)))
                h.append([round(d["time"]), d["cpu"], round(100 * (1 - d["mem_available"] / max(d["mem_total"], 1)), 1),
                          round(d["load"][0], 2), round(d["disk_read"]), round(d["disk_write"]), round(d["net_rx"]), round(d["net_tx"])])
        return result

    def loop(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                servers = [s for s in self.list if s["enabled"]]
                list(pool.map(self.collect, servers))
                self.wake.wait(self.interval)
                self.wake.clear()

    def start(self):
        threading.Thread(target=self.loop, name="server-monitor", daemon=True).start()

    # ---------------------------------------------------------------- views

    def summary(self):
        """For everyone: enough to choose where to run."""
        rows = []
        with self.lock:
            for s in self.list:
                r = self.latest.get(s["id"])
                row = {"id": s["id"], "name": s["name"], "host": s["host"], "enabled": s["enabled"], "default": s is self.default(),
                       "state": "disabled" if not s["enabled"] else ("unknown" if r is None else ("online" if r["ok"] else "offline"))}
                if r and r["ok"]:
                    d = r["data"]
                    row.update(time=r["time"], ncpu=d["ncpu"], cpu=d["cpu"], iowait=d["iowait"], load=d["load"],
                               mem_total=d["mem_total"], mem_available=d["mem_available"],
                               disk_read=d["disk_read"], disk_write=d["disk_write"], net_rx=d["net_rx"], net_tx=d["net_tx"],
                               users=len(d["users"]), gpus=len(d["gpus"]),
                               free_cores=max(0, round(d["ncpu"] - d["load"][0])))
                elif r:
                    row.update(time=r["time"], error=r["error"])
                rows.append(row)
        return rows

    def details(self):
        """For administrators: everything, with history."""
        with self.lock:
            return [{"server": s, "latest": self.latest.get(s["id"]), "history": list(self.history.get(s["id"], []))}
                    for s in self.list]
