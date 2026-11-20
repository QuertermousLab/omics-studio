#!/usr/bin/env python3
"""The Omics Studio gateway in Python (standard library only): one address for every module, with the routes of
gateway/apache.conf.in, for servers without Apache (THOR's apache2 package was removed on 2026-11-12).

  python3 gateway/gateway.py --listen 0.0.0.0 --port 8892 --hub 8893 --canvas 8000 --great 8894 --auth 8895 \
      --workbench 8896 --alphagenome 8897 [--jupyter 8891] [--log gateway/logs/access.log]

Routes (as apache.conf): slashless entry points redirect (/great -> /great/, /auth -> /auth/login); / and /hub/,
/spatial/, /single-cell/, /crispr/ -> hub; /genome-canvas/ -> Genome Canvas (prefix stripped, X-Forwarded-Prefix);
/auth/ /admin/ -> sign-in; /great/ -> GREAT; /workbench/ -> Omics Workbench; /alphagenome/ -> AlphaGenome; everything else -> JupyterLab (with
WebSocket upgrades), or 404 without it. Like mod_proxy it keeps Host, adds X-Forwarded-For / -Host / -Server (the
backends tell proxied requests from their own internal calls by X-Forwarded-For) and drops X-Remote-User. Bodies are
streamed both ways, so large downloads and uploads do not sit in memory.
"""

import argparse
import http.client
import os
import re
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "proxy-connection"}
# requests left out of the access log: the lab dashboard (10.130.73.73) checks the site every few seconds
QUIET_AGENTS = {"LabDashboard"}
SLASHLESS = re.compile(r"^/(hub|spatial|single-cell|crispr|genome-canvas|great|admin|workbench|alphagenome)$")
CHUNK = 256 * 1024
TIMEOUT = 600  # ProxyTimeout 600


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OmicsStudioGateway/1.0"

    def route(self):
        """(port, upstream path, extra headers) or None (404)."""
        path = self.path.split("?", 1)[0]
        ports = self.server.ports
        if path == "/" or path.startswith(("/hub/", "/spatial/", "/single-cell/", "/crispr/")):
            return ports["hub"], self.path, {}
        if path.startswith("/genome-canvas/"):
            return ports["canvas"], self.path[len("/genome-canvas"):] or "/", {"X-Forwarded-Prefix": "/genome-canvas"}
        if path.startswith(("/auth/", "/admin/")):
            return ports["auth"], self.path, {}
        if path.startswith("/great/"):
            return ports["great"], self.path, {}
        if path.startswith("/workbench/"):
            return ports["workbench"], self.path, {}
        if path.startswith("/alphagenome/") and ports.get("alphagenome"):
            return ports["alphagenome"], self.path, {}
        if ports.get("jupyter"):
            return ports["jupyter"], self.path, {}
        return None

    def client_ip(self):
        return self.client_address[0]

    def handle_one(self):
        path = self.path.split("?", 1)[0]
        if SLASHLESS.match(path):
            return self.redirect(path + "/" + (self.path[len(path):] or ""))
        if path in ("/auth", "/auth/"):
            return self.redirect("/auth/login")
        target = self.route()
        if target is None:
            return self.simple(404, "Not found")
        port, upstream_path, extra = target
        headers = []
        forwarded = None
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP or lk in ("x-remote-user", "x-forwarded-prefix", "content-length"):
                continue
            if lk == "x-forwarded-for":
                forwarded = v
                continue
            headers.append((k, v))
        headers.append(("X-Forwarded-For", "{}, {}".format(forwarded, self.client_ip()) if forwarded else self.client_ip()))
        headers.append(("X-Forwarded-Host", self.headers.get("Host", "")))
        headers.append(("X-Forwarded-Server", self.server.server_name_))
        for k, v in extra.items():
            headers.append((k, v))
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.tunnel(port, upstream_path, headers)
        self.proxy(port, upstream_path, headers)

    # ---------------------------------------------------------------- plain HTTP

    def request_body(self):
        """A generator of the request body (Content-Length or chunked), or None."""
        length = self.headers.get("Content-Length")
        if length is not None:
            remaining = int(length)
            def fixed():
                nonlocal remaining
                while remaining > 0:
                    data = self.rfile.read(min(CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
            return fixed(), int(length)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            def chunked():
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return
                    yield self.rfile.read(size)
                    self.rfile.readline()
            return chunked(), None
        return None, 0

    def proxy(self, port, path, headers):
        body, length = self.request_body()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
        try:
            conn.putrequest(self.command, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            if body is not None and length is None:
                conn.putheader("Transfer-Encoding", "chunked")
            elif length:
                conn.putheader("Content-Length", str(length))
            conn.endheaders()
            if body is not None:
                for piece in body:
                    if length is None:
                        conn.send(b"%x\r\n%s\r\n" % (len(piece), piece))
                    else:
                        conn.send(piece)
                if length is None:
                    conn.send(b"0\r\n\r\n")
            res = conn.getresponse()
        except OSError as error:
            conn.close()
            return self.simple(502, "The service behind {} is not answering ({})".format(path.split("?")[0], error))
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
            self.log_request(res.status, known or "-")
            if no_body:
                return
            while True:
                data = res.read1(CHUNK) if hasattr(res, "read1") else res.read(CHUNK)
                if not data:
                    break
                if known is None:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                else:
                    self.wfile.write(data)
            if known is None:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        finally:
            conn.close()

    # ---------------------------------------------------------------- WebSocket (JupyterLab kernels and terminals)

    def tunnel(self, port, path, headers):
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=30)
        except OSError as error:
            return self.simple(502, "The service is not answering ({})".format(error))
        lines = ["{} {} HTTP/1.1".format(self.command, path)] + ["{}: {}".format(k, v) for k, v in headers]
        lines += ["Connection: Upgrade", "Upgrade: websocket", "", ""]
        upstream.sendall("\r\n".join(lines).encode("latin-1"))
        upstream.settimeout(None)
        client = self.connection
        self.log_request(101, "-")
        sockets = [client, upstream]
        try:
            while True:
                readable, _, broken = select.select(sockets, [], sockets, 3600)
                if broken or not readable:
                    break
                for s in readable:
                    data = s.recv(CHUNK)
                    if not data:
                        return
                    (upstream if s is client else client).sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True

    # ---------------------------------------------------------------- small answers and logging

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def simple(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        log = self.server.log
        if log is None:
            return
        agent = self.headers.get("User-Agent", "") if self.headers else ""
        if agent in QUIET_AGENTS:
            return  # monitors that ask every few seconds (as apache.conf.in: SetEnvIf ... quiet)
        line = '{} - - [{}] "{}" {} {} "{}" "{}"\n'.format(
            self.client_ip(), time.strftime("%d/%b/%Y:%H:%M:%S %z"), self.requestline, code, size,
            self.headers.get("Referer", "-") if self.headers else "-", self.headers.get("User-Agent", "-") if self.headers else "-")
        with self.server.log_lock:
            log.write(line)
            log.flush()

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        print("{} {}".format(self.client_ip(), fmt % args), file=sys.stderr, flush=True)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_one


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8892)
    ap.add_argument("--server-name", default=socket.gethostname())
    for name, port in (("hub", 8893), ("canvas", 8000), ("great", 8894), ("auth", 8895), ("workbench", 8896), ("alphagenome", 8897)):
        ap.add_argument("--" + name, type=int, default=port)
    ap.add_argument("--jupyter", type=int, default=0, help="JupyterLab's port (0: none)")
    ap.add_argument("--log", default="", help="access log (combined format)")
    a = ap.parse_args()
    server = Server((a.listen, a.port), Gateway)
    server.ports = {"hub": a.hub, "canvas": a.canvas, "great": a.great, "auth": a.auth, "workbench": a.workbench,
                    "alphagenome": a.alphagenome, "jupyter": a.jupyter}
    server.server_name_ = a.server_name
    server.log_lock = threading.Lock()
    server.log = None
    if a.log:
        os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
        server.log = open(a.log, "a", buffering=1)
    print("Omics Studio gateway on {}:{} -> hub {hub}, canvas {canvas}, great {great}, auth {auth}, workbench {workbench}, alphagenome {alphagenome}, jupyter {jupyter}".format(
        a.listen, a.port, **server.ports), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
