#!/usr/bin/env python3
"""Omics Workbench server: the analysis part of Omics Studio (downloads, bulk and single-cell pipelines, server load).

    python3 server.py --host 127.0.0.1 --port 8896

The gateway routes /workbench/ here. Pages (one page, hub/static/workbench.html, built with the hub's pages):
  /workbench/                 overview: your jobs and the servers' load
  /workbench/download/        FASTQ / processed data download (links, delivery e-mails, s3://, GEO / SRA)
  /workbench/bulk/            RNA-seq, ChIP-seq, CUT&RUN, CUT&Tag, ATAC-seq (Pipelines-Wrappers)
  /workbench/single-cell/     Cell Ranger (GEX, ATAC, multiome) and the Seurat analysis
  /workbench/jobs/<id>/       a job: state, live log, files
API under /workbench/api/ (JSON). Administrators (the sign-in's) also manage the servers, their SSH keys and the
e-mail settings (the admin page's Servers section calls this API).

Who runs what: a job runs in the account the person names on the chosen server, with that account's password (kept in
memory only, never written); administrators may leave the password out, and the job then runs with this service's
SSH key in the account set for the server (Admin → Servers). Sign-in as everywhere: ../auth/sessions.py.
Standard library only (Python 3.8+); the scripts it starts need the lab's tools on the chosen server.
"""

import argparse
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import fastq  # noqa: E402
import jobs as jobs_module  # noqa: E402
import pipelines  # noqa: E402
import remote  # noqa: E402
import servers as servers_module  # noqa: E402

try:
    import download  # noqa: E402
except ImportError:  # written separately; the download page says so
    download = None

REPO = APP_DIR.parent
PAGE = REPO / "hub" / "static" / "workbench.html"
PAGES = re.compile(r"/workbench/(?:(?:download|bulk|single-cell|jobs|servers)/(?:[a-z0-9]{12}/)?)?")
DEFAULT_SETTINGS = {
    "wrappers": "/nfs/baldar/quanyiz/app/Pipelines-Wrappers",
    "setup": "",                    # environment lines run before every job (e.g. conda activate ...)
    "mail": {"method": "sendmail", "from": "", "host": "", "port": 25, "starttls": False, "user": "", "password": "", "server": ""},
    "buckets": {},                  # extra s3 bucket -> {profile, region}
    "defaultServer": "",
}


def load_rsa():
    """The sign-in's RSA helper (auth/rsa.py): passwords typed into the pages are encrypted with this server's key."""
    spec = importlib.util.spec_from_file_location("omics_canvas_rsa", str(REPO / "auth" / "rsa.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.KeyPair()


def decrypt_passwords(value, key):
    """Every "password" field sent as "rsa:<base64>" (the pages always do) becomes the plain password."""
    import base64
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "password" and isinstance(v, str) and v.startswith("rsa:"):
                try:
                    v = key.decrypt(base64.b64decode(v[4:])).decode("utf-8")
                except ValueError:
                    raise ValueError("The password could not be decrypted: reload the page and try again")
            else:
                v = decrypt_passwords(v, key)
            out[k] = v
        return out
    if isinstance(value, list):
        return [decrypt_passwords(v, key) for v in value]
    return value


SHARED_ROOTS = ("/nfs/",)  # where the lab's shared (777) folders are


def shared_folder(path):
    """Whether `path` is in the lab's shared space: under /nfs, its nearest existing folder writable by everyone (the
    shared folders are 777; homes are not) and reached through no symbolic link."""
    p = os.path.normpath(path or "")
    if not p.startswith(SHARED_ROOTS) or p != (path or "").rstrip("/"):
        return False
    q = p
    while q != "/" and not os.path.lexists(q):
        q = os.path.dirname(q)
    try:
        info = os.lstat(q)
    except OSError:
        return False
    return os.path.realpath(q) == q and stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_IWOTH)


def load_gate():
    folder = os.environ.get("OMICS_CANVAS_AUTH", "").strip()
    if not folder:
        return None
    path = REPO / "auth" / "sessions.py"
    spec = importlib.util.spec_from_file_location("omics_canvas_sessions", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Gate(folder), module


class Settings:
    def __init__(self, state_dir):
        self.file = Path(state_dir) / "settings.json"
        self.lock = threading.Lock()
        self.values = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            saved = json.loads(self.file.read_text())
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(self.values.get(k), dict):
                    self.values[k].update(v)
                else:
                    self.values[k] = v
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.values.get(key, default)

    def public(self):
        """For administrators: everything but the SMTP password."""
        out = json.loads(json.dumps(self.values))
        out["mail"]["password"] = "" if not self.values["mail"].get("password") else "(set)"
        return out

    def update(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if k == "mail" and isinstance(v, dict):
                    if v.get("password") == "(set)":
                        v = dict(v, password=self.values["mail"].get("password", ""))
                    self.values["mail"].update({x: v[x] for x in DEFAULT_SETTINGS["mail"] if x in v})
                elif k == "buckets" and isinstance(v, dict):
                    self.values[k] = {str(b): {"profile": str(c.get("profile") or "default"), "region": str(c.get("region") or "")}
                                      for b, c in v.items() if isinstance(c, dict)}
                else:
                    self.values[k] = str(v or "")
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=2))
            os.chmod(tmp, 0o600)  # the SMTP password
            tmp.replace(self.file)
        return self.public()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OmicsWorkbench/1.0"

    def log_message(self, fmt, *args):
        if "/api/jobs/" in (self.path or "") and " 200 " in (fmt % args):
            return  # the job pages poll every few seconds
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    @property
    def app(self):
        return self.server.app

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_body(self, body, content_type, status=200):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload, status=200):
        self.send_body(json.dumps(payload, separators=(",", ":"), default=str), "application/json; charset=utf-8", status)

    def read_json(self, maximum=4 * 2**20):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > maximum:
            raise ValueError("Request too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object")
        return payload

    def identity(self):
        gate = self.app.gate
        if gate is None:
            return {"user": remote.SERVICE_USER, "method": "local", "admin": True}
        return gate[0].identity(self.headers, self.client_address)

    def do_HEAD(self):
        self.route("GET")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        parsed = urlparse(self.path)
        path, query = unquote(parsed.path), {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/workbench/api/health":
                return self.send_json({"ok": True})
            if path == "/workbench":
                self.send_response(302)
                self.send_header("Location", "/workbench/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            who = self.identity()
            if who is None:
                if method == "GET" and not path.startswith("/workbench/api/"):
                    self.send_response(302)
                    self.send_header("Location", self.app.gate[1].login_location(self.path))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self.send_json({"error": "Sign in first"}, 401)
            if method in ("POST", "DELETE"):
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                    raise PermissionError("Cross-site requests are not allowed")
            if method == "GET" and PAGES.fullmatch(path):
                try:
                    return self.send_body(PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                except OSError:
                    return self.send_body("Build the pages first: ./vizhubctl build", "text/plain; charset=utf-8", 503)
            if not path.startswith("/workbench/api/"):
                return self.send_json({"error": "Not found"}, 404)
            result = self.app.api(method, path[len("/workbench/api/"):], query, self.read_json if method == "POST" else dict, who)
            if isinstance(result, tuple):  # (bytes, content type)
                return self.send_body(result[0], result[1])
            self.send_json(result)
        except PermissionError as error:
            self.send_json({"error": str(error)}, 403)
        except KeyError as error:
            self.send_json({"error": str(error).strip("'\"")}, 404)
        except (ValueError, FileNotFoundError, remote.RemoteError) as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001 - the page shows it
            import traceback
            traceback.print_exc()
            self.send_json({"error": "{}: {}".format(type(error).__name__, error)}, 500)


class App:
    def __init__(self, state_dir, public_url, interval):
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.gate = load_gate()
        self.settings = Settings(self.state)
        self.servers = servers_module.Servers(self.state, interval)
        self.jobs = jobs_module.Jobs(self.state, self.servers, self.settings, public_url)
        self.sources = {}        # (user, source id) -> (time, source with secrets)
        self.inspections = {}    # reference path -> {"state", "result" | "error"}
        self.lock = threading.Lock()
        self.key = load_rsa()

    def start(self):
        self.servers.start()
        self.jobs.start()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def require_admin(who):
        if not who.get("admin"):
            raise PermissionError("Only administrators can do this")

    def target(self, payload, who):
        """The server and account a request names. Without a password only administrators (the service's key)."""
        server = self.servers.get(payload.get("server") or self.servers.default()["id"])
        account = (payload.get("account") or "").strip()
        password = payload.get("password") or ""
        if account and not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", account):
            raise ValueError("Not an account name: {}".format(account))
        if not password and not who.get("admin"):
            raise PermissionError("Give your account and password on {} (only administrators run without one)".format(server["name"]))
        if password and not account:
            raise ValueError("Give the account name for the password")
        return remote.Target(server, account or None, password or None), account, password

    def wrappers(self):
        return self.settings.get("wrappers") or DEFAULT_SETTINGS["wrappers"]

    # ---------------------------------------------------------------- API

    def api(self, method, route, query, body, who):
        b = decrypt_passwords(body(), self.key) if method == "POST" else {}
        parts = route.strip("/").split("/")
        head = parts[0]
        if route == "key":
            return self.key.public()
        if route == "me":
            return {"user": who["user"], "admin": bool(who.get("admin")), "method": who.get("method"),
                    "serviceUser": remote.SERVICE_USER, "wrappers": self.wrappers(), "setup": self.settings.get("setup") or "",
                    "download": download is not None}
        if head == "servers":
            return self.api_servers(method, parts[1:], b, who)
        if route == "settings":
            self.require_admin(who)
            return self.settings.update(b) if method == "POST" else self.settings.public()
        if route == "mail-test" and method == "POST":
            self.require_admin(who)
            jobs_module.send_mail(self.settings.get("mail"), self.servers, b.get("to"), "[Omics Workbench] test",
                                  "This is a test message from Omics Workbench ({}).".format(self.jobs.public_url or "THOR"))
            return {"ok": True}
        if route == "browse":
            return self.browse(query.get("path") or "/nfs", query.get("files") == "1")
        if route == "scan" and method == "POST":
            return self.scan(b, who)
        if route == "check" and method == "POST":
            return self.check(b, who)
        if head == "bulk" and method == "POST":
            return self.bulk(parts[1] if len(parts) > 1 else "", b, who)
        if head == "singlecell" and method == "POST":
            return self.singlecell(parts[1] if len(parts) > 1 else "", b, who)
        if head == "download" and method == "POST":
            return self.download(parts[1] if len(parts) > 1 else "", b, who)
        if head == "reference":
            return self.reference(b if method == "POST" else query, who)
        if head == "jobs":
            return self.api_jobs(method, parts[1:], query, b, who)
        raise KeyError("Not found")

    def api_servers(self, method, rest, b, who):
        if not rest:
            if method == "POST":
                self.require_admin(who)
                return {"servers": self.servers.save(b.get("servers") or [])}
            self.servers.viewed = time.time()
            return {"servers": self.servers.summary(), "interval": self.servers.interval}
        if rest == ["details"]:
            self.require_admin(who)
            return {"servers": self.servers.details(), "interval": self.servers.interval, "serviceUser": remote.SERVICE_USER}
        server = self.servers.get(rest[0])
        action = rest[1] if len(rest) > 1 else ""
        if action == "key" and method == "POST":
            self.require_admin(who)
            if not b.get("password"):
                raise ValueError("Give the account's password (used once, not kept)")
            ok = remote.install_key(server, (b.get("account") or server["user"]).strip(), b["password"])
            threading.Thread(target=self.servers.collect, args=(server,), daemon=True).start()
            return {"ok": ok}
        if action == "test" and method == "POST":
            target, _, _ = self.target(dict(b, server=server["id"]), who)
            code, out, err = target.run("echo \"$(whoami)@$(hostname)\"; nproc", timeout=30)
            if code != 0:
                raise remote.RemoteError((err or out).strip()[-300:])
            who_host, ncpu = (out.strip().splitlines() + ["", ""])[:2]
            return {"ok": True, "as": who_host, "ncpu": ncpu}
        if action == "refresh" and method == "POST":
            self.require_admin(who)
            return self.servers.collect(server)
        raise KeyError("Not found")

    def browse(self, path, files):
        """Folders (and files) under path, as this server sees them (the /nfs paths are the same on every server)."""
        p = Path(path if path.startswith("/") else "/" + path)
        try:
            p = p.resolve()
            entries = []
            with os.scandir(p) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        is_dir = e.is_dir()
                        if not is_dir and not files:
                            continue
                        size = None if is_dir else e.stat().st_size
                    except OSError:
                        continue
                    entries.append({"name": e.name, "dir": is_dir, "size": size})
        except PermissionError:
            raise PermissionError("Cannot read {}".format(p))
        except FileNotFoundError:
            raise FileNotFoundError("No folder {}".format(p))
        except NotADirectoryError:
            raise ValueError("{} is a file".format(p))
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "entries": entries[:3000],
                "writable": os.access(p, os.W_OK)}

    def scan(self, b, who):
        target, _, _ = self.target(b, who)
        folder = (b.get("folder") or "").strip()
        if not folder.startswith("/"):
            raise ValueError("Give the folder's full path")
        kind = b.get("kind") or "bulk"
        if kind == "outs":
            return {"runs": self.scan_outs(target, folder)}
        files = fastq.scan(target, folder)
        for extra in b.get("folders") or []:  # multiome: GEX and ATAC in separate folders
            extra = (extra or "").strip()
            if extra.startswith("/") and extra.rstrip("/") != folder.rstrip("/"):
                seen = {f["path"] for f in files}
                files += [f for f in fastq.scan(target, extra) if f["path"] not in seen]
        if kind == "10x":
            samples = fastq.group_10x(files)
            return {"files": len(files), "samples": samples, "pairs": fastq.pair_multiome(samples)}
        samples = fastq.group_bulk(files)
        for s in samples:
            s["group"] = fastq.guess_group(s["name"])
            s["is_control"] = fastq.is_control(s["name"])
        controls = fastq.find_controls(samples)
        for s in samples:
            s["control"] = controls.get(s["name"], "")
        groups = [s["group"] for s in samples]
        return {"files": len(files), "samples": samples, "comparisons": pipelines.comparisons_all(groups),
                "paired": all(s["paired"] for s in samples) if samples else True}

    @staticmethod
    def scan_outs(target, folder):
        """Cell Ranger output folders under folder (for the Seurat analysis of existing runs)."""
        import shlex
        code, out, err = target.run(
            "find -L {} -maxdepth 4 -type d -name outs 2>/dev/null | head -n 500 | while read d; do "
            "k=; [ -e \"$d/atac_fragments.tsv.gz\" ] && k=multiome; [ -z \"$k\" ] && [ -e \"$d/fragments.tsv.gz\" ] && k=atac; "
            "[ -z \"$k\" ] && [ -e \"$d/filtered_feature_bc_matrix.h5\" ] && k=gex; [ -n \"$k\" ] && printf '%s\\t%s\\n' \"$k\" \"$d\"; done".format(
                shlex.quote(folder)), timeout=90)
        runs = []
        for line in out.splitlines():
            kind, _, path = line.partition("\t")
            if path:
                name = Path(path).parent.name
                runs.append({"kind": kind, "outs": path, "name": fastq.safe_name(name), "aggr": name.startswith("aggr")})
        return runs

    def check(self, b, who):
        p = b.get("params") or {}
        if b.get("kind") == "download" and not b.get("password") and not who.get("admin"):
            # as the download itself will run: this service's account, into a shared folder only
            output = (p.get("output") or "").rstrip("/")
            if not shared_folder(output):
                raise PermissionError("{} is not a shared folder (one under /nfs that everyone can write to, reached "
                                      "through no link): give your THOR account and password to download there".format(output or "The folder"))
            target, shared = remote.Target(self.servers.default()), True
        else:
            (target, _, _), shared = self.target(b, who), False
        if b.get("kind") == "singlecell":
            need, tools = pipelines.singlecell_requirements(p, self.wrappers())
            inputs = sorted({d for s in p.get("samples") or [] if s.get("include", True) for d in s.get("dirs", [])} |
                            {d for pr in p.get("pairs") or [] if pr.get("include", True) for lib in (pr.get("gex"), pr.get("atac")) if lib for d in lib.get("dirs", [])} |
                            {r["outs"] for r in p.get("existing") or []})
        elif b.get("kind") == "download":
            need, tools, inputs = [], ["wget", "curl", "lftp", "rclone", "sshpass", "aws"], []
        else:
            need, tools = pipelines.bulk_requirements(p, self.wrappers())
            inputs = sorted({f for s in p.get("samples") or [] if s.get("include", True) for f in (s.get("r1") or []) + (s.get("r2") or [])})
        output = (p.get("output") or "").strip() or None
        results = pipelines.check_paths(target, need, tools, inputs[:400], output, shared=shared)
        return {"server": target.server["name"], "as": target.describe(), "results": results,
                "ok": all(r["ok"] for r in results if r["what"] != "program" or r["path"] not in ("fastqc",))}

    def start_job(self, b, who, kind, title, folder, body, shared=False, **extra):
        if not folder or not folder.startswith("/"):
            raise ValueError("Give the output folder's full path")
        if shared:
            # a download into a shared folder: this service's account on this machine (see download())
            server, account, password = self.servers.default(), "", ""
        else:
            target, account, password = self.target(b, who)
            server = target.server
        return self.jobs.public(self.jobs.create(
            who, kind, title, server["id"], account, password, folder, body,
            setup=(self.settings.get("setup") or "") + "\n" + (b.get("setup") or ""), email=b.get("email") or "",
            shared=shared, **extra))

    def bulk(self, action, b, who):
        p = b.get("params") or {}
        p["output"] = (p.get("output") or "").rstrip("/")
        body = pipelines.bulk_script(p, self.wrappers())
        if action == "preview":
            return {"script": body}
        if action == "run":
            names = {"rnaseq": "RNA-seq", "chipseq": "ChIP-seq", "cutrun": "CUT&RUN", "cuttag": "CUT&Tag", "atacseq": "ATAC-seq"}
            n = len([s for s in p["samples"] if s.get("include", True)])
            title = b.get("title") or "{}: {} samples → {}".format(names.get(p["assay"], p["assay"]), n, Path(p["output"]).name)
            return self.start_job(b, who, "bulk", title, p["output"], body, params=p)
        raise KeyError("Not found")

    def singlecell(self, action, b, who):
        p = b.get("params") or {}
        p["output"] = (p.get("output") or "").rstrip("/")
        if action == "continue":
            # the full Seurat analysis after reviewing the thresholds of a QC job
            parent = self.jobs.get(b.get("job") or "")
            if not self.jobs.allowed(parent, who):
                raise PermissionError("Not your job")
            p = json.loads(json.dumps(parent["params"]))
            s = p.setdefault("seurat", {})
            rows = b.get("thresholds") or []
            if not rows:
                raise ValueError("No thresholds")
            columns = list(rows[0].keys())
            tsv = "\t".join(columns) + "\n" + "".join("\t".join("" if r.get(c) in (None, "") else str(r[c]) for c in columns) + "\n" for r in rows)
            if not re.fullmatch(r"[\w.\-\t\n ]*", tsv):
                raise ValueError("Thresholds may hold numbers and sample names only")
            out = s.get("output") or "{}/seurat".format(p["output"])
            s.update(stage="full", review=False, thresholds_file="{}/thresholds_reviewed.tsv".format(out))
            p["skip_count"] = True
            p["existing"] = parent_runs(parent)
            p["aggr"] = False
            body = ("printf '%s' {} > {}\n".format(pipelines.q(tsv), pipelines.q(s["thresholds_file"])) +
                    pipelines.singlecell_script(p, self.wrappers()))
            return self.start_job(dict(b, server=parent["server"], account=parent["account"]), who, "singlecell",
                                  "Seurat analysis (reviewed QC): {}".format(Path(p["output"]).name), p["output"], body,
                                  params=p, parent=parent["id"])
        body = pipelines.singlecell_script(p, self.wrappers())
        if action == "preview":
            return {"script": body}
        if action == "run":
            labels = {"gex": "Cell Ranger GEX", "atac": "Cell Ranger ATAC", "multiome": "Cell Ranger ARC"}
            title = b.get("title") or ("Seurat analysis" if p.get("skip_count") else labels[p["mode"]]) + " → " + Path(p["output"]).name
            return self.start_job(b, who, "singlecell", title, p["output"], body, params=p)
        raise KeyError("Not found")

    # ---------------------------------------------------------------- downloads

    def remember_source(self, who, source):
        with self.lock:
            cutoff = time.time() - 86400
            self.sources = {k: v for k, v in self.sources.items() if v[0] > cutoff}
            self.sources[(who["user"], source["id"])] = (time.time(), source)

    def source(self, who, sid):
        hit = self.sources.get((who["user"], sid))
        if not hit:
            raise KeyError("Paste the source again (it was not found or has expired)")
        return hit[1]

    def download(self, action, b, who):
        if download is None:
            raise ValueError("The download module is not installed yet")
        if hasattr(download, "DEFAULT_BUCKETS"):
            for bucket, conf in (self.settings.get("buckets") or {}).items():
                download.DEFAULT_BUCKETS.setdefault(bucket, conf)
        if action == "parse":
            result = download.parse_input(b.get("text") or "")
            out = []
            for s in result.get("sources", []):
                self.remember_source(who, s)
                out.append({k: v for k, v in s.items() if k != "secrets"})
            return {"sources": out, "warnings": result.get("warnings", [])}
        if action == "geo":
            lookup = download.geo_lookup((b.get("accession") or "").strip())
            hit = self.sources.get((who["user"], b.get("source") or ""))
            if hit:
                hit[1]["lookup"] = lookup  # plan() uses it instead of looking it up again
            return lookup
        source = self.source(who, b.get("source") or "")
        secrets_ = source.get("secrets") or {}
        if action == "list":
            listing = download.list_remote(source, secrets_)
            source["listing"] = listing  # the exact preview, and which transport worked
            return listing
        options = dict(b.get("options") or {})
        if source.get("listing") and "entries" not in options:
            options["entries"] = source["listing"].get("entries")
            options["transport"] = source["listing"].get("transport_index")
        plan = download.plan(source, secrets_, b.get("selection"), (b.get("dest") or "").rstrip("/"), options)
        if action == "plan":
            return {k: v for k, v in plan.items() if k != "secrets"}
        if action == "run":
            dest = (b.get("dest") or "").rstrip("/")
            # no account given: the lab's shared folders under /nfs are writable by everyone, so the download runs in
            # this service's account (files writable by everyone too, as the folders); a home folder needs your account
            shared = not b.get("password") and not who.get("admin")
            if shared and not shared_folder(dest):
                raise PermissionError("{} is not a shared folder (one under /nfs that everyone can write to, reached "
                                      "through no link): give your THOR account and password to download there".format(dest))
            env = {"WB_SECRET_" + k if not k.startswith("WB_SECRET_") else k: v for k, v in (plan.get("secrets") or {}).items()}
            title = b.get("title") or "Download {} → {}".format(source.get("label") or source.get("kind"), plan.get("root") or b.get("dest"))
            params = {"source": {k: v for k, v in source.items() if k not in ("secrets", "listing", "lookup")}, "dest": b.get("dest"),
                      "options": b.get("options") or {}, "root": plan.get("root"), "files": len(plan.get("files") or [])}
            body = "umask 0000  # the shared folders are writable by everyone: so are the files\n" + plan["script"] if shared else plan["script"]
            return self.start_job(b, who, "download", title, dest, body, shared=shared,
                                  secrets_env=env, params=params, outputs=[plan.get("root")] if plan.get("root") else [])
        raise KeyError("Not found")

    # ---------------------------------------------------------------- label reference columns

    def reference(self, b, who):
        path = (b.get("path") or "").strip()
        if not path.startswith("/") or not path.lower().endswith((".rds", ".h5ad", ".h5seurat")):
            raise ValueError("Give the full path of a reference .rds (Seurat) file")
        with self.lock:
            entry = self.inspections.get(path)
            if entry and entry["state"] == "done" and entry.get("mtime") == _mtime(path):
                return entry
            if entry and entry["state"] == "running":
                return entry
            if b.get("start") in (True, "1", 1):
                entry = self.inspections[path] = {"state": "running", "started": time.time()}
                threading.Thread(target=self._inspect, args=(path,), daemon=True).start()
                return entry
        return entry or {"state": "none"}

    def _inspect(self, path):
        import subprocess
        try:
            proc = subprocess.run(["Rscript", str(pipelines.SC_SCRIPT), "--inspect-reference", path],
                                  capture_output=True, text=True, timeout=1800)
            line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), None)
            if proc.returncode != 0 or not line:
                raise RuntimeError((proc.stderr or proc.stdout).strip()[-400:] or "Rscript failed")
            self.inspections[path] = {"state": "done", "result": json.loads(line), "mtime": _mtime(path)}
        except Exception as error:  # noqa: BLE001 - shown on the page
            self.inspections[path] = {"state": "failed", "error": str(error)}

    # ---------------------------------------------------------------- jobs

    def api_jobs(self, method, rest, query, b, who):
        if not rest or rest == [""]:
            everyone = query.get("all") == "1"
            return {"jobs": [self.jobs.public(j) for j in self.jobs.visible(who, everyone)]}
        job = self.jobs.get(rest[0])
        if not self.jobs.allowed(job, who):
            raise PermissionError("Not your job")
        action = rest[1] if len(rest) > 1 else ""
        if not action:
            if method == "DELETE":
                self.jobs.remove(job)
                return {"ok": True}
            return {"job": self.jobs.public(job), "passwordKnown": job["auth"] == "key" or job["id"] in self.jobs.passwords}
        if action == "log":
            text, offset, size = self.jobs.log(job, int(query.get("offset") or 0))
            return {"text": text, "offset": offset, "size": size, "state": job["state"], "step": job.get("step"),
                    "progress": job.get("progress")}
        if action == "cancel" and method == "POST":
            self.jobs.cancel(job, b.get("password"))
            return {"job": self.jobs.public(job)}
        if action == "password" and method == "POST":
            target = self.jobs.target(job, b.get("password"))
            code, out, err = target.run("true", timeout=30)
            self.jobs.passwords[job["id"]] = b.get("password")
            self.jobs.poll(job)
            return {"job": self.jobs.public(job)}
        if action == "file":
            name = query.get("name") or ""
            data = self.jobs.read_file(job, name, limit=20 * 2**20)
            types = {".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json", ".html": "text/html; charset=utf-8"}
            ctype = types.get(Path(name).suffix.lower(), "text/plain; charset=utf-8")
            if ctype.startswith("text/html"):
                ctype = "text/plain; charset=utf-8"  # never render job output as a page of this site
            return data, ctype
        if action == "poll" and method == "POST":
            self.jobs.poll(job)
            return {"job": self.jobs.public(job)}
        raise KeyError("Not found")


def parent_runs(job):
    p = job["params"]
    if p.get("skip_count"):
        return p.get("existing") or []
    items = p.get("pairs") if p.get("mode") == "multiome" else p.get("samples")
    return [{"name": s["name"], "outs": "{}/{}/outs".format(p["output"], s["name"])} for s in items or [] if s.get("include", True)]


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description="Omics Workbench server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8896)
    ap.add_argument("--state", default=str(REPO / "state" / "workbench"))
    ap.add_argument("--public-url", default=os.environ.get("VIZHUB_PUBLIC_URL", ""))
    ap.add_argument("--interval", type=int, default=20, help="seconds between server load samples")
    a = ap.parse_args()
    app = App(a.state, a.public_url, a.interval)
    app.start()
    server = Server((a.host, a.port), Handler)
    server.app = app
    print("Omics Workbench on http://{}:{}/workbench/ (state {}, sign-in {})".format(
        a.host, a.port, a.state, "on" if app.gate else "off"), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
