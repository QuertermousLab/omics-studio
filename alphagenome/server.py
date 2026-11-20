#!/usr/bin/env python3
"""AlphaGenome in Omics Studio: start the model on its GPU server when someone needs it, stop it to free the GPUs.

AlphaGenome (the lab's local AlphaGenome deployment: two GPU workers, a proxy and the AlphaGenome Atlas pages) runs on
a GPU server (FREYA) as that account's systemd user services. This server, on the Omics Studio machine:
  - starts and stops it there through a restricted SSH key (state/alphagenome/freya_ed25519) that can only run
    alphagenome/remote.sh (status, start, stop, stop-force, restart-web) and forward to the proxy's port;
  - keeps an SSH tunnel to the proxy while it runs and serves the Atlas at /alphagenome/ through it;
  - stops it after VIZHUB_ALPHAGENOME_IDLE_MINUTES without use when it was started from the page and nothing else
    (batch SNP predictions, other programs) uses the model.

  /alphagenome/studio/            the control page (status, GPU memory, Start / Stop)
  /alphagenome/studio-api/status  GET; /start, /stop POST ({"force": true}: administrators)
  /alphagenome/health             open (vizhubctl status)
  /alphagenome/…                  AlphaGenome Atlas (its pages and API), while it runs; otherwise the control page

Standard library only. Sign-in: OMICS_CANVAS_AUTH as the other backends (../auth/sessions.py).
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

APP_DIR = Path(__file__).resolve().parent
REPO = APP_DIR.parent
WEB = APP_DIR / "web"
PREFIX = "/alphagenome"
CHUNK = 256 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade"}
# Requests that mean someone is working with the model (the pages also poll health and the track list every 15 s:
# an open but forgotten tab does not keep the GPUs busy)
ACTIVITY = re.compile(r"^/alphagenome/(?:$|(?:api|ui-api)/(?:predict|predict_tracks|genes|motifs|variant-search|"
                      r"gene-search)|ui-api/custom-tracks/[^/]+/values)")
OTHERS_RECENT = 10 * 60  # someone else used it in the last 10 minutes: only an administrator may stop it
TYPES = {".html": "text/html; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


def load_sign_in():
    """(Gate, module) of Omics Studio sign-in when OMICS_CANVAS_AUTH names its state folder, else (None, None)."""
    folder = os.environ.get("OMICS_CANVAS_AUTH", "").strip()
    module_path = REPO / "auth" / "sessions.py"
    if not folder or not module_path.is_file():
        return None, None
    import importlib.util
    spec = importlib.util.spec_from_file_location("omics_canvas_sessions", str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Gate(folder), module


# ---------------------------------------------------------------- the GPU server

class Remote:
    """SSH to the GPU server with the restricted key: commands of remote.sh, and the tunnel to the proxy."""

    def __init__(self, target, key, remote_port, local_port, ssh_port=22):
        self.target, self.key, self.ssh_port = target, str(key), int(ssh_port)
        self.remote_port, self.local_port = int(remote_port), int(local_port)
        self.tunnel_proc = None
        self.tunnel_started = 0.0

    def base(self):
        # only this key: never an agent forwarded to this machine, never the account's own keys
        return ["ssh", "-p", str(self.ssh_port), "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR", "-T"]

    @staticmethod
    def env():
        env = dict(os.environ)
        env.pop("SSH_AUTH_SOCK", None)
        return env

    def run(self, word, timeout=40):
        """(exit code, parsed JSON or None, stderr) of remote.sh <word>."""
        if not self.target:
            return 1, None, "AlphaGenome is not set up on this server (VIZHUB_ALPHAGENOME_SSH in deploy/site.env)"
        try:
            p = subprocess.run(self.base() + [self.target, word], capture_output=True, text=True, timeout=timeout,
                               env=self.env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, None, "no answer from the GPU server in {} s".format(timeout)
        except OSError as exc:
            return 127, None, str(exc)
        out = None
        for line in reversed(p.stdout.strip().splitlines()):
            try:
                out = json.loads(line)
                break
            except ValueError:
                continue
        return p.returncode, out, p.stderr.strip()[-800:]

    def tunnel_alive(self):
        if self.tunnel_proc is None or self.tunnel_proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", self.local_port), timeout=2).close()
            return True
        except OSError:
            return False

    def open_tunnel(self):
        if not self.target:
            return False
        if self.tunnel_alive():
            return True
        if self.tunnel_proc is not None and self.tunnel_proc.poll() is None:
            if time.time() - self.tunnel_started < 15:
                return False  # still connecting
            self.close_tunnel()
        argv = self.base() + ["-N", "-o", "ExitOnForwardFailure=yes",
                              "-L", "127.0.0.1:{}:127.0.0.1:{}".format(self.local_port, self.remote_port), self.target]
        self.tunnel_proc = subprocess.Popen(argv, env=self.env(), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.tunnel_started = time.time()
        for _ in range(30):
            if self.tunnel_proc.poll() is not None:
                err = (self.tunnel_proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                log("tunnel failed:", err[-300:])
                return False
            if self.tunnel_alive():
                log("tunnel open: 127.0.0.1:{} -> {}:{}".format(self.local_port, self.target.split("@")[-1],
                                                               self.remote_port))
                return True
            time.sleep(0.5)
        return False

    def close_tunnel(self):
        p, self.tunnel_proc = self.tunnel_proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            log("tunnel closed")


class Controller:
    """What the page shows and does: the GPU server's state, who started and who uses AlphaGenome, idle stop."""

    def __init__(self, remote, state_dir, idle_minutes, name):
        self.remote, self.name = remote, name
        self.idle = max(0, int(idle_minutes)) * 60
        self.state_file = Path(state_dir) / "control.json"
        self.lock = threading.Lock()
        self.remote_state = None   # last status of remote.sh
        self.checked = 0.0
        self.error = ""
        self.action = None         # "starting" / "stopping" while a request is on its way
        self.watched = 0.0         # someone has the control page open: check more often
        self.memory = self.load()

    # ---- the little that survives a restart of this server
    def load(self):
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("users", {})
        data.setdefault("events", [])
        return data

    def save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.memory, indent=1))
        os.chmod(str(tmp), 0o600)
        tmp.replace(self.state_file)

    def event(self, what, user):
        self.memory["events"] = (self.memory["events"] + [{"t": time.time(), "what": what, "user": user}])[-50:]
        log(what, "by", user)

    def used(self, user):
        now = time.time()
        with self.lock:
            last = self.memory["users"].get(user, 0)
            self.memory["users"][user] = now
            self.memory["lastUsed"] = now
            if now - last > 60:
                self.save()

    # ---- the GPU server
    def refresh(self):
        code, out, err = self.remote.run("status", timeout=30)
        with self.lock:
            self.checked = time.time()
            if code == 0 and isinstance(out, dict):
                self.remote_state, self.error = out, ""
            else:
                self.remote_state = None
                if "Permission denied" in (err or ""):
                    err = ("{} does not accept Omics Studio's key yet: its line has to be added to ~/.ssh/authorized_keys "
                           "there (alphagenome/README.md, Setting it up)".format(self.name))
                self.error = err or "the GPU server did not answer (exit {})".format(code)
        return out

    def phase(self):
        s = self.remote_state
        if self.action:
            return self.action
        if s is None:
            return "unknown"
        if s.get("ready"):
            return "ready"
        if s.get("starting") or (s.get("running") and not s.get("ready")):
            return "starting"
        return "stopped"

    def status(self, identity):
        self.watched = time.time()
        with self.lock:
            now = time.time()
            users = {u: t for u, t in self.memory["users"].items() if now - t < 3600}
            phase = self.phase()
            s = self.remote_state or {}
            last = self.memory.get("lastUsed") or self.memory.get("startedAt") or 0
            fromPage = bool(self.memory.get("startedBy"))
            me = (identity or {}).get("user") or "you"
            others = sorted(u for u, t in users.items() if u != me and now - t < OTHERS_RECENT)
            return {
                "name": self.name, "phase": phase, "error": self.error, "checked": self.checked,
                "host": s.get("host"), "services": s.get("services"), "gpus": s.get("gpus") or [],
                "modelUsers": s.get("users") or [], "startLog": s.get("startLog") if phase != "ready" else "",
                "tunnel": self.remote.tunnel_alive(),
                "startedBy": self.memory.get("startedBy"), "startedAt": self.memory.get("startedAt"),
                "fromPage": fromPage, "lastUsed": last or None,
                "idleMinutes": self.idle // 60,
                "idleStopAt": (last + self.idle) if (fromPage and self.idle and phase == "ready" and last) else None,
                "recentUsers": [{"user": u, "t": t} for u, t in sorted(users.items(), key=lambda x: -x[1])],
                "othersActive": others,
                "admin": bool((identity or {}).get("admin")),
                "events": self.memory["events"][-8:],
            }

    def start(self, identity):
        user = (identity or {}).get("user") or "someone"
        with self.lock:
            if self.action:
                return {"ok": False, "error": "AlphaGenome is already {}".format(self.action)}
            self.action = "starting"
        try:
            code, out, err = self.remote.run("start", timeout=40)
            if code != 0:
                raise RuntimeError(err or "start failed (exit {})".format(code))
            with self.lock:
                if (out or {}).get("started"):
                    self.memory.update({"startedBy": user, "startedAt": time.time(), "lastUsed": time.time()})
                    self.event("started", user)
                    self.save()
            return {"ok": True, "result": out}
        finally:
            with self.lock:
                self.action = None
            self.refresh()

    def stop(self, identity, force=False, reason="stopped"):
        user = "Omics Studio" if identity is None else (identity.get("user") or "someone")
        admin = bool((identity or {}).get("admin"))
        now = time.time()
        with self.lock:
            others = [u for u, t in self.memory["users"].items() if u != user and now - t < OTHERS_RECENT]
            if identity is not None and others and not admin:
                return {"ok": False, "error": "{} used AlphaGenome in the last {} minutes; ask them, or an "
                                              "administrator, to stop it".format(", ".join(sorted(others)),
                                                                                 OTHERS_RECENT // 60)}
            if force and not admin:
                return {"ok": False, "error": "Only administrators can stop it while other programs use the model"}
            if self.action:
                return {"ok": False, "error": "AlphaGenome is already {}".format(self.action)}
            self.action = "stopping"
        try:
            code, out, err = self.remote.run("stop-force" if force else "stop", timeout=120)
            if code == 3:
                return {"ok": False, "busy": True, "error": "Not stopped: {}".format((out or {}).get("reason", "in use"))}
            if code != 0:
                return {"ok": False, "error": err or "stop failed (exit {})".format(code)}
            self.remote.close_tunnel()
            with self.lock:
                for k in ("startedBy", "startedAt", "lastUsed"):
                    self.memory.pop(k, None)
                self.memory["users"] = {}
                self.event(reason + (" (forced)" if force else ""), user)
                self.save()
            return {"ok": True}
        finally:
            with self.lock:
                self.action = None
            self.refresh()

    # ---- the loop: state, tunnel, idle stop
    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as exc:  # keep the loop alive whatever happens
                log("loop:", repr(exc))
            time.sleep(5)

    def tick(self):
        now = time.time()
        phase = self.phase()
        busy = phase in ("starting", "stopping") or now - self.watched < 120
        every = 5 if busy else (30 if phase == "ready" else 120)
        if now - self.checked >= every and not self.action:
            self.refresh()
            phase = self.phase()
        s = self.remote_state or {}
        if s.get("services", {}).get("proxy") == "active":
            self.remote.open_tunnel()
        elif self.remote_state is not None and not self.action:
            self.remote.close_tunnel()
        # idle stop: only what was started from the page, and only when nothing else uses the model
        with self.lock:
            last = self.memory.get("lastUsed") or self.memory.get("startedAt") or 0
            from_page = bool(self.memory.get("startedBy"))
        if from_page and self.idle and phase == "ready" and last and now - last > self.idle and not s.get("users"):
            log("idle for {} min: stopping".format(int((now - last) // 60)))
            result = self.stop(None, reason="stopped after {} idle minutes".format(self.idle // 60))
            if not result.get("ok"):
                log("idle stop refused:", result.get("error"))
                with self.lock:  # try again after another idle period, not every 5 s
                    self.memory["lastUsed"] = now
                    self.save()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OmicsStudioAlphaGenome/1.0"
    identity = None

    def log_message(self, fmt, *args):
        pass  # the gateway's access log has every request

    def send_bytes(self, data, status=200, ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, payload, status=200):
        self.send_bytes(json.dumps(payload).encode(), status)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def signed_in(self, path):
        gate, module = self.server.sign_in
        if gate is None or path == PREFIX + "/health":
            return True
        self.identity = gate.identity(self.headers, self.client_address[0])
        if self.identity:
            return True
        if module.wants_html(self.command, self.headers):
            self.redirect(module.login_location(self.path))
        else:
            if self.command in ("POST", "PUT", "DELETE"):
                self.close_connection = True
            self.send_json({"error": "Sign in to Omics Studio first", "signIn": "/auth/login"}, 401)
        return False

    def do_GET(self):
        self.handle_any()

    def do_HEAD(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def do_PUT(self):
        self.handle_any()

    def do_DELETE(self):
        self.handle_any()

    def handle_any(self):
        path = urlsplit(self.path).path
        if path == PREFIX:
            return self.redirect(PREFIX + "/")
        if not path.startswith(PREFIX + "/"):
            return self.send_json({"error": "Not found"}, 404)
        if not self.signed_in(path):
            return
        ctl = self.server.controller
        if path == PREFIX + "/health":
            return self.send_json({"ok": True, "phase": ctl.phase()})
        if path.startswith(PREFIX + "/studio-api/"):
            return self.api(path[len(PREFIX + "/studio-api/"):])
        if path == PREFIX + "/studio":
            return self.redirect(PREFIX + "/studio/")
        if path.startswith(PREFIX + "/studio/"):
            return self.static(path[len(PREFIX + "/studio/"):] or "index.html")
        # AlphaGenome Atlas: through the tunnel while it runs; otherwise the control page
        if not ctl.remote.tunnel_alive() or ctl.phase() != "ready":
            if self.command in ("GET", "HEAD") and (path == PREFIX + "/" or "text/html" in self.headers.get("Accept", "")):
                return self.redirect(PREFIX + "/studio/")
            return self.send_json({"error": "AlphaGenome is not running: start it at {}/studio/".format(PREFIX)}, 503)
        if ACTIVITY.match(path):
            ctl.used((self.identity or {}).get("user") or "someone")
        self.proxy()

    def api(self, route):
        ctl = self.server.controller
        if route == "status" and self.command in ("GET", "HEAD"):
            return self.send_json(ctl.status(self.identity))
        if self.command != "POST":
            return self.send_json({"error": "Not found"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0) or b"{}")
        except ValueError:
            body = {}
        if route == "start":
            try:
                result = ctl.start(self.identity)
            except RuntimeError as exc:
                result = {"ok": False, "error": str(exc)}
            return self.send_json(result, 200 if result.get("ok") else 409)
        if route == "stop":
            result = ctl.stop(self.identity, force=bool(body.get("force")))
            return self.send_json(result, 200 if result.get("ok") else 409)
        return self.send_json({"error": "Not found"}, 404)

    def static(self, name):
        target = (WEB / name).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self.send_json({"error": "Not found"}, 404)
        self.send_bytes(target.read_bytes(), 200, TYPES.get(target.suffix, "application/octet-stream"))

    def proxy(self):
        """Forward to the AlphaGenome proxy through the tunnel (the path stays: the Atlas has basePath /alphagenome)."""
        port = self.server.controller.remote.local_port
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in self.headers.items():
                # Omics Studio's session cookie and sign-in headers stay here: AlphaGenome has no use for them
                if k.lower() in HOP or k.lower() in ("content-length", "cookie", "authorization", "x-remote-user",
                                                      "accept-encoding"):
                    continue
                conn.putheader(k, v)
            # uncompressed through the tunnel: the Atlas would answer zstd, which not every browser reads
            conn.putheader("Accept-Encoding", "identity")
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            res = conn.getresponse()
        except OSError as exc:
            conn.close()
            return self.send_json({"error": "AlphaGenome is not answering ({})".format(exc)}, 502)
        try:
            self.send_response_only(res.status, res.reason)
            self.send_header("Date", self.date_time_string())
            known = res.getheader("Content-Length")
            for k, v in res.getheaders():
                if k.lower() in HOP or k.lower() in ("content-length", "date", "server"):
                    continue
                self.send_header(k, v)
            no_body = self.command == "HEAD" or res.status in (204, 304) or 100 <= res.status < 200
            if known is not None:
                self.send_header("Content-Length", known)
            elif not no_body:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if no_body:
                return
            while True:
                data = res.read1(CHUNK) if hasattr(res, "read1") else res.read(CHUNK)
                if not data:
                    break
                self.wfile.write(data if known is not None else b"%x\r\n%s\r\n" % (len(data), data))
            if known is None:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        finally:
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8897)
    ap.add_argument("--remote", default=os.environ.get("VIZHUB_ALPHAGENOME_SSH", ""),
                    help="user@host of the GPU server (deploy/site.env VIZHUB_ALPHAGENOME_SSH)")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--name", default="FREYA", help="the GPU server's name on the page")
    ap.add_argument("--key", default=str(REPO / "state" / "alphagenome" / "freya_ed25519"))
    ap.add_argument("--remote-port", type=int, default=8080, help="AlphaGenome's proxy port on the GPU server")
    ap.add_argument("--tunnel-port", type=int, default=8898, help="this machine's end of the tunnel (127.0.0.1)")
    ap.add_argument("--idle-minutes", type=int, default=60, help="stop after this long without use (0: never)")
    ap.add_argument("--state", default=str(REPO / "state" / "alphagenome"))
    a = ap.parse_args()
    Path(a.state).mkdir(parents=True, exist_ok=True)
    os.chmod(a.state, 0o700)
    remote = Remote(a.remote, a.key, a.remote_port, a.tunnel_port, a.ssh_port)
    controller = Controller(remote, a.state, a.idle_minutes, a.name)
    server = Server((a.host, a.port), Handler)
    server.sign_in = load_sign_in()
    server.controller = controller
    threading.Thread(target=controller.loop, daemon=True).start()
    log("AlphaGenome control on {}:{} -> {} (proxy port {}, tunnel 127.0.0.1:{}, idle stop {} min, sign-in {})".format(
        a.host, a.port, a.remote.split("@")[-1] or "(no GPU server set up)", a.remote_port, a.tunnel_port, a.idle_minutes,
        "on" if server.sign_in[0] else "off"))
    try:
        server.serve_forever()
    finally:
        remote.close_tunnel()


if __name__ == "__main__":
    main()
