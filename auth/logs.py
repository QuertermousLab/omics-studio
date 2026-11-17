"""The logs the admin page can clear (Admin → Logs): what each holds, its size, and what clearing does.

Everything here belongs to this checkout and this account. Logs of jobs that are still queued or running are never
touched. The system journal (systemd, where the services' own output goes) belongs to root: it is shown with the
command that clears it. The servers' load history lives in Omics Workbench's memory: the page clears it through
Workbench's own API (/workbench/api/servers/history).
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DAY = 86400
STAMP = re.compile(rb"\[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]")
ACTIVE = {"queued", "starting", "running", "cancelling", "waiting"}


class Logs:
    def __init__(self, store, repo=REPO):
        self.store = store
        self.repo = Path(repo)
        self.gateway_dir = self.repo / "gateway" / "logs"
        self.access = self.gateway_dir / "access.log"
        self.hub_state = self.repo / "state"
        self.runs = self.repo / "state" / "workbench" / "runs"
        self.wb_jobs = self.repo / "state" / "workbench" / "jobs"

    # ---------------------------------------------------------------- what there is

    def inventory(self):
        items = []
        size, oldest = _size(self.access), _first_stamp(self.access)
        items.append({"id": "access", "name": "Gateway access log", "path": _rel(self.access, self.repo),
                      "detail": "Every request to the site: address, time, page, status, browser.",
                      "size": size, "oldest": oldest, "actions": ["keep7", "clear"] if size else []})
        others = [p for p in sorted(self.gateway_dir.glob("*")) if p.is_file() and p != self.access]
        if others:
            items.append({"id": "gateway-old", "name": "Old gateway logs", "path": ", ".join(_rel(p, self.repo) for p in others),
                          "detail": "Written by Apache before it was removed (2026-11-12); nothing writes them now.",
                          "size": sum(_size(p) for p in others), "oldest": None, "actions": ["delete"]})
        rows, first = self.store.log_stats()
        items.append({"id": "signin", "name": "Sign-in log", "path": "state/auth/auth.sqlite3 (table log)",
                      "detail": "Every sign-in attempt; failed ones also count towards lockouts. Kept 180 days.",
                      "entries": rows, "oldest": first, "actions": ["keep30", "clear"] if rows else []})
        logs = self.finished_conversion_logs()
        items.append({"id": "conversions", "name": "Conversion logs", "path": "state/jobs/<id>.log",
                      "detail": "What each finished conversion of Omics Canvas printed (queued and running ones are kept).",
                      "size": sum(_size(p) for p in logs), "entries": len(logs),
                      "oldest": min((_mtime(p) for p in logs), default=None), "actions": ["clear"] if logs else []})
        runs = self.finished_runs()
        items.append({"id": "downloads", "name": "Download job folders", "path": "state/workbench/runs/<id>/",
                      "detail": "Script and log of finished downloads into shared folders (the downloaded files stay).",
                      "size": sum(_tree_size(p) for p in runs), "entries": len(runs),
                      "oldest": min((_mtime(p) for p in runs), default=None), "actions": ["clear"] if runs else []})
        items.append(self.journal())
        return items

    def finished_conversion_logs(self):
        try:
            jobs = json.loads((self.hub_state / "jobs.json").read_text()).get("jobs", [])
        except (OSError, ValueError):
            jobs = []
        active = {j.get("id") for j in jobs if j.get("state") in ACTIVE}
        return [p for p in sorted((self.hub_state / "jobs").glob("*.log")) if p.stem not in active]

    def finished_runs(self):
        out = []
        for d in sorted(self.runs.glob("*")) if self.runs.is_dir() else []:
            if not d.is_dir() or d.is_symlink():
                continue
            try:
                state = json.loads((self.wb_jobs / (d.name + ".json")).read_text()).get("state")
            except (OSError, ValueError):
                state = None  # its record was removed from the list: nothing follows it any more
            if state not in ACTIVE:
                out.append(d)
        return out

    @staticmethod
    def journal():
        item = {"id": "journal", "name": "System journal", "path": "/var/log/journal (systemd)",
                "detail": "The services' own messages (and everything else on THOR); systemd keeps it within its size "
                          "limit. It belongs to root: clear it on THOR with the command below.",
                "command": "sudo journalctl --vacuum-time=14d", "actions": []}
        try:
            out = subprocess.run(["journalctl", "--user", "--disk-usage"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"take up ([\d.]+)([KMGT]?)", out)
            if m:
                item["size"] = int(float(m.group(1)) * 1024 ** " KMGT".index(m.group(2) or " "))
        except (OSError, subprocess.SubprocessError):
            pass
        return item

    # ---------------------------------------------------------------- clearing

    def clean(self, item, action):
        if item == "access" and action == "clear":
            return {"freed": _truncate(self.access)}
        if item == "access" and action == "keep7":
            return {"freed": _keep_recent(self.access, time.time() - 7 * DAY)}
        if item == "gateway-old" and action == "delete":
            freed = 0
            for p in self.gateway_dir.glob("*"):
                if p.is_file() and not p.is_symlink() and p != self.access:
                    freed += _size(p)
                    p.unlink()
            return {"freed": freed}
        if item == "signin" and action in ("clear", "keep30"):
            return {"removed": self.store.clear_log(time.time() - 30 * DAY if action == "keep30" else None)}
        if item == "conversions" and action == "clear":
            logs = self.finished_conversion_logs()
            freed = sum(_size(p) for p in logs)
            for p in logs:
                p.unlink()
            return {"freed": freed, "removed": len(logs)}
        if item == "downloads" and action == "clear":
            runs = self.finished_runs()
            freed = sum(_tree_size(p) for p in runs)
            for d in runs:
                shutil.rmtree(str(d))
            return {"freed": freed, "removed": len(runs)}
        raise KeyError("Not found")


def _rel(path, repo):
    try:
        return str(Path(path).relative_to(repo))
    except ValueError:
        return str(path)


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _tree_size(folder):
    return sum(_size(p) for p in Path(folder).rglob("*") if p.is_file() and not p.is_symlink())


def _stamp(line):
    m = STAMP.search(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1).decode()[:20], "%d/%b/%Y:%H:%M:%S"))
    except ValueError:
        return None


def _first_stamp(path):
    try:
        with open(str(path), "rb") as f:
            return _stamp(f.readline())
    except OSError:
        return None


def _truncate(path):
    """Empty a log in place: the gateway keeps it open for appending and goes on writing at its new end."""
    size = _size(path)
    if size:
        with open(str(path), "r+b") as f:
            f.truncate(0)
    return size


def _keep_recent(path, since):
    """Keep the lines from `since` on, in place (the gateway keeps appending to the same file)."""
    size = _size(path)
    if not size:
        return 0
    with open(str(path), "r+b") as f:
        offset = 0
        for line in iter(f.readline, b""):
            t = _stamp(line)
            if t is not None and t >= since:
                break
            offset += len(line)
        if offset == 0:
            return 0
        f.seek(offset)
        rest = f.read()
        f.seek(0)
        f.truncate(0)
        f.write(rest)
    return size - len(rest)
