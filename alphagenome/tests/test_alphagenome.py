"""AlphaGenome control (alphagenome/server.py, remote.sh) against a fake GPU server: a fake `ssh` on PATH answers the
remote.sh words from a state file and opens the "tunnel" as a local TCP forwarder to a fake Atlas."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

HERE = Path(__file__).resolve().parent
APP = HERE.parent

FAKE_SSH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, socket, sys, threading, time
    d = os.environ["AGT"]; f = os.path.join(d, "remote.json")
    args = sys.argv[1:]
    st = json.load(open(f)) if os.path.exists(f) else {"on": False, "t": 0, "users": []}
    open(os.path.join(d, "calls.log"), "a").write(" ".join(args) + "\\n")
    if "-N" in args:
        lport = int(args[args.index("-L") + 1].split(":")[1]); rport = int(os.environ["FAKE_ATLAS_PORT"])
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", lport)); srv.listen(20)
        def pipe(a, b):
            try:
                while True:
                    x = a.recv(65536)
                    if not x: break
                    b.sendall(x)
            except OSError: pass
            for s in (a, b):
                try: s.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        while True:
            c, _ = srv.accept(); u = socket.create_connection(("127.0.0.1", rport))
            threading.Thread(target=pipe, args=(c, u), daemon=True).start()
            threading.Thread(target=pipe, args=(u, c), daemon=True).start()
    word = args[-1]
    if word == "status":
        on = st["on"]; ready = on and time.time() - st["t"] > 1
        print(json.dumps({"host": "fake-gpu", "port": 8080, "running": on, "ready": ready, "starting": on and not ready,
            "services": {k: ("active" if on else "inactive") for k in ("worker0", "worker1", "ui", "proxy")},
            "gpus": [{"index": 0, "name": "NVIDIA L4", "used": 15000 if on else 0, "total": 23034, "util": 3}],
            "users": st["users"], "startLog": ""}))
    elif word == "start":
        st.update(on=True, t=time.time()); json.dump(st, open(f, "w")); print('{"started": true}')
    elif word in ("stop", "stop-force"):
        if st["users"] and word == "stop":
            print('{"stopped": false, "reason": "in use by batch job (someone)"}'); sys.exit(3)
        st["on"] = False; json.dump(st, open(f, "w")); print('{"stopped": true}')
''')


class Atlas(BaseHTTPRequestHandler):
    """Echoes what reached it: the path and whether Omics Studio's cookie was passed on."""

    def do_GET(self):
        body = json.dumps({"path": self.path, "cookie": self.headers.get("Cookie")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ControlServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        ssh = self.dir / "bin" / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(0o755)
        self.atlas = ThreadingHTTPServer(("127.0.0.1", 0), Atlas)
        Thread(target=self.atlas.serve_forever, daemon=True).start()
        self.port, tunnel = free_port(), free_port()
        env = dict(os.environ, AGT=str(self.dir), FAKE_ATLAS_PORT=str(self.atlas.server_address[1]),
                   PATH=str(self.dir / "bin") + os.pathsep + os.environ.get("PATH", ""), OMICS_CANVAS_AUTH="")
        self.proc = subprocess.Popen([sys.executable, "-B", str(APP / "server.py"), "--port", str(self.port),
                                      "--tunnel-port", str(tunnel), "--remote", "tester@fake-gpu",
                                      "--key", str(self.dir / "key"), "--state", str(self.dir / "state"),
                                      "--idle-minutes", "0", "--name", "TESTGPU"],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.base = "http://127.0.0.1:{}".format(self.port)
        for _ in range(50):
            try:
                self.get("/alphagenome/health")
                break
            except OSError:
                time.sleep(0.1)

    def tearDown(self):
        os.killpg(self.proc.pid, 15)  # the server and its "tunnel"
        self.proc.wait(10)
        self.atlas.shutdown()
        self.atlas.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()

    def post(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def status(self):
        return json.loads(self.get("/alphagenome/studio-api/status")[1])

    def wait_for(self, check, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            s = self.status()
            if check(s):
                return s
            time.sleep(0.3)
        self.fail("timed out; last status {}".format(s))

    def test_start_serve_and_stop(self):
        s = self.wait_for(lambda s: s["checked"])
        self.assertEqual(s["phase"], "stopped")
        self.assertEqual(s["name"], "TESTGPU")
        # stopped: the Atlas's address leads to the control page, its API says how to start it
        req = urllib.request.Request(self.base + "/alphagenome/", headers={"Accept": "text/html"})
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.open(req, timeout=10)
        self.assertEqual(cm.exception.code, 302)
        self.assertTrue(cm.exception.headers["Location"].endswith("/alphagenome/studio/"))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/alphagenome/api/predict")
        self.assertEqual(cm.exception.code, 503)

        code, out = self.post("/alphagenome/studio-api/start")
        self.assertEqual(code, 200, out)
        s = self.wait_for(lambda s: s["phase"] == "ready" and s["tunnel"])
        self.assertIsNotNone(s["startedAt"])

        # through the tunnel: the path stays (basePath /alphagenome), Omics Studio's cookie does not go along
        _, body = self.get("/alphagenome/api/genes?chrom=chr1", headers={"Cookie": "omics_canvas_session=secret"})
        echoed = json.loads(body)
        self.assertEqual(echoed["path"], "/alphagenome/api/genes?chrom=chr1")
        self.assertIsNone(echoed["cookie"])
        self.assertTrue(self.status()["recentUsers"])

        code, out = self.post("/alphagenome/studio-api/stop")
        self.assertEqual(code, 200, out)
        s = self.wait_for(lambda s: s["phase"] == "stopped")
        self.assertFalse(s["tunnel"])
        self.assertIn("stopped", [e["what"] for e in s["events"]])

    def test_busy_model_is_not_stopped(self):
        (self.dir / "remote.json").write_text(json.dumps({"on": True, "t": 0, "users": [{"what": "batch job (x)", "count": 1}]}))
        self.wait_for(lambda s: s["phase"] == "ready")
        code, out = self.post("/alphagenome/studio-api/stop")
        self.assertEqual(code, 409)
        self.assertIn("batch job", out["error"])
        code, out = self.post("/alphagenome/studio-api/stop", {"force": True})  # not an administrator
        self.assertEqual(code, 409)
        self.assertTrue(json.loads((self.dir / "remote.json").read_text())["on"])

    def test_only_the_restricted_key(self):
        self.wait_for(lambda s: s["checked"])
        calls = (self.dir / "calls.log").read_text()
        self.assertIn("IdentitiesOnly=yes", calls)
        self.assertIn("IdentityAgent=none", calls)
        self.assertIn("-i {}".format(self.dir / "key"), calls)

    def test_static_files_stay_in_web(self):
        code, body = self.get("/alphagenome/studio/")
        self.assertEqual(code, 200)
        self.assertIn(b"AlphaGenome", body)
        conn = socket.create_connection(("127.0.0.1", self.port))
        conn.sendall(b"GET /alphagenome/studio/../server.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        reply = b""
        while True:
            data = conn.recv(65536)
            if not data:
                break
            reply += data
        conn.close()
        self.assertTrue(reply.startswith(b"HTTP/1.1 404"), reply[:80])


class NotSetUp(unittest.TestCase):
    def test_no_gpu_server(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable, "-B", str(APP / "server.py"), "--port", str(port), "--remote", "",
                                     "--tunnel-port", str(free_port()), "--state", tmp],
                                    env=dict(os.environ, OMICS_CANVAS_AUTH="", VIZHUB_ALPHAGENOME_SSH=""),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:{}/alphagenome/studio-api/status".format(port),
                                                    timeout=5) as r:
                            s = json.loads(r.read())
                        if s["checked"]:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                self.assertEqual(s["phase"], "unknown")
                self.assertIn("not set up", s["error"])
            finally:
                proc.terminate()
                proc.wait(10)


class RemoteScript(unittest.TestCase):
    def test_refuses_other_commands(self):
        for word in ("bash", "status; id", "rm -rf /", "stop-force x"):
            p = subprocess.run(["bash", str(APP / "remote.sh")], env=dict(os.environ, SSH_ORIGINAL_COMMAND=word),
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, word)
            self.assertIn("refused", p.stderr)


if __name__ == "__main__":
    unittest.main()
