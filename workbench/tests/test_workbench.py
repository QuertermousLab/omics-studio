"""Tests of Omics Workbench without servers: FASTQ grouping, job scripts (bash -n), permissions, passwords, a local job.

    python3 -m unittest discover workbench/tests
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import fastq  # noqa: E402
import jobs  # noqa: E402
import pipelines  # noqa: E402
import remote  # noqa: E402
import server  # noqa: E402


def bash_ok(text):
    proc = subprocess.run(["bash", "-n"], input=text.encode(), capture_output=True)
    return proc.returncode == 0, proc.stderr.decode()


def files(*names):
    return [{"path": n, "size": 100} for n in names]


class Grouping(unittest.TestCase):
    def test_lanes_flowcells_and_names(self):
        samples = fastq.group_bulk(files(
            "/d/raw/WT_1/WT_1_CKDL230_H7K_L1_1.fq.gz", "/d/raw/WT_1/WT_1_CKDL230_H7K_L1_2.fq.gz",
            "/d/raw/WT_1/WT_1_CKDL230_H8Z_L3_1.fq.gz", "/d/raw/WT_1/WT_1_CKDL230_H8Z_L3_2.fq.gz",
            "/d/f/KO-rep2_S3_L001_R1_001.fastq.gz", "/d/f/KO-rep2_S3_L001_R2_001.fastq.gz",
            "/d/f/KO-rep2_S3_L002_R1_001.fastq.gz", "/d/f/KO-rep2_S3_L002_R2_001.fastq.gz",
            "/d/f/KO-rep2_S3_L001_I1_001.fastq.gz"))
        by = {s["name"]: s for s in samples}
        self.assertEqual(set(by), {"WT_1", "KO-rep2"})
        self.assertEqual(by["WT_1"]["lanes"], 2)
        self.assertTrue(by["KO-rep2"]["paired"])
        self.assertEqual(len(by["KO-rep2"]["r1"]), 2)  # the index read is left out
        self.assertEqual(fastq.guess_group("KO-rep2"), "KO")
        self.assertEqual(fastq.guess_group("WT_1"), "WT")

    def test_single_end_and_controls(self):
        samples = fastq.group_bulk(files("/c/H3K27ac_rep1.fastq.gz", "/c/Input_rep1.fastq.gz", "/c/H3K27ac_rep2.fastq.gz", "/c/Input_rep2.fastq.gz"))
        self.assertFalse(any(s["paired"] for s in samples))
        controls = fastq.find_controls(samples)
        self.assertEqual(controls, {"H3K27ac_rep1": "Input_rep1", "H3K27ac_rep2": "Input_rep2"})
        self.assertTrue(fastq.is_control("IgG_A"))
        self.assertFalse(fastq.is_control("Inputless_H3K4me3"))

    def test_multiome_runs_indexes_and_merged_copies(self):
        # IGVF13 layout: 10x index and flowcell in the prefix, a second sequencing round, a merged/ copy
        r = "/d/IGVF13/"
        libs = fastq.group_10x(files(
            r + "ATAC/Mult_1/Mult_1-SI_3A_F8_22W32GLT4_S1_L006_R1_001.fastq.gz", r + "ATAC/Mult_1/Mult_1-SI_3A_F8_22W32GLT4_S1_L006_R3_001.fastq.gz",
            r + "ATAC/Sequence_Round2/Mult_1/Mult_1-SI_3A_F8_22J7FKLT4_S1_L004_R1_001.fastq.gz",
            r + "ATAC/merged/Mult_1_S1_L001_R1_001.fastq.gz", r + "ATAC/merged/Mult_1_S1_L001_R3_001.fastq.gz",
            r + "RNA/Mult_1_RNA/Mult_1_RNA-SI_TT_E1_22V7L7LT4_S1_L001_R1_001.fastq.gz",
            r + "RNA/Sequence_Round2/Mult_1_RNA/Mult_1_RNA-SI_TT_E1_22JC23LT4_S1_L001_R2_001.fastq.gz"))
        by = {(l["key"], l["merged"]): l for l in libs}
        self.assertEqual(by[("Mult_1", False)]["runs"], 2)
        self.assertEqual(by[("Mult_1_RNA", False)]["runs"], 2)
        self.assertIn(("Mult_1", True), by)
        pairs = fastq.pair_multiome(libs)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["atac"], by[("Mult_1", False)]["id"])  # the raw runs, not the merged copy
        self.assertEqual(pairs[0]["name"], "Mult_1")
        p = {"mode": "multiome", "output": "/o", "genome": "hg38",
             "pairs": [{"name": "Mult_1", "gex": by[("Mult_1_RNA", False)], "atac": by[("Mult_1", False)]}]}
        text = pipelines.singlecell_script(p, "/w")
        self.assertIn("Mult_1-SI_3A_F8_22J7FKLT4,Chromatin Accessibility", text)
        self.assertIn("Mult_1_RNA-SI_TT_E1_22V7L7LT4,Gene Expression", text)

    def test_10x_and_multiome_pairs(self):
        libs = fastq.group_10x(files("/x/PBMC_GEX_S1_L001_R1_001.fastq.gz", "/x/PBMC_GEX_S1_L001_R2_001.fastq.gz",
                                     "/y/PBMC_ATAC_S2_L001_R1_001.fastq.gz", "/y/PBMC_ATAC_S2_L001_R3_001.fastq.gz"))
        self.assertEqual([l["kind"] for l in libs], ["gex", "atac"])
        self.assertEqual(fastq.pair_multiome(libs), [{"gex": "x1", "atac": "x2", "name": "PBMC"}])


def bulk(assay, **extra):
    p = {"assay": assay, "output": "/tmp/wb out", "genome": "hg38", "threads": 4, "paired": True,
         "samples": [{"name": "A_1", "r1": ["/in/a b_R1.fq.gz"], "r2": ["/in/a b_R2.fq.gz"], "group": "A", "control": "IN"},
                     {"name": "B_1", "r1": ["/in/b_R1.fq.gz", "/in/b2_R1.fq.gz"], "r2": ["/in/b_R2.fq.gz", "/in/b2_R2.fq.gz"], "group": "B", "control": "IN"},
                     {"name": "IN", "r1": ["/in/i_R1.fq.gz"], "r2": ["/in/i_R2.fq.gz"], "group": "C", "is_control": True}]}
    p.update(extra)
    return pipelines.bulk_script(p, "/wrappers")


class Scripts(unittest.TestCase):
    def test_bulk_scripts_parse(self):
        for assay in ("rnaseq", "chipseq", "cutrun", "cuttag", "atacseq"):
            text = bulk(assay, peaks={"call": True, "broad": assay == "cuttag"}, comparisons=[["B", "A"]] if assay == "rnaseq" else [])
            ok, err = bash_ok(text)
            self.assertTrue(ok, "{}: {}".format(assay, err))
        rna = bulk("rnaseq", comparisons=[["B", "A"]])
        self.assertIn("-c comparisons.txt", rna)
        self.assertIn("'/in/a b_R1.fq.gz'", rna)  # quoted
        chip = bulk("chipseq", peaks={"call": True})
        self.assertIn("-c IN_filtered.bam", chip)
        self.assertNotIn("-n IN ", chip.split("macs2 callpeak", 1)[1])  # controls are not peak-called
        atac = bulk("atacseq", macs2="--nomodel -q 0.01")
        self.assertIn("-M '--nomodel -q 0.01'", atac)

    def test_bad_names_and_comparisons(self):
        with self.assertRaises(ValueError):
            bulk("rnaseq", samples=[{"name": "a;rm -rf ~", "r1": ["x"], "r2": ["y"], "group": "A"}])
        with self.assertRaises(ValueError):
            bulk("rnaseq", comparisons=[["A", "Z"]])

    def test_singlecell_scripts_parse(self):
        gex = {"mode": "gex", "output": "/o", "genome": "hg38", "aggr": True,
               "samples": [{"name": "s1", "prefix": "S1", "dirs": ["/f"]}, {"name": "s2", "prefix": "S2", "dirs": ["/f", "/g"]}],
               "seurat": {"run": True, "review": True, "formats": ["rds", "h5ad"], "ref": "/r.rds"}}
        multi = {"mode": "multiome", "output": "/o", "genome": "mm10",
                 "pairs": [{"name": "m1", "gex": {"prefix": "G", "dirs": ["/g"]}, "atac": {"prefix": "A", "dirs": ["/a"]}}],
                 "seurat": {"run": True, "formats": ["h5mu"]}}
        for p in (gex, multi):
            text = pipelines.singlecell_script(p, "/wrappers")
            ok, err = bash_ok(text)
            self.assertTrue(ok, err)
        self.assertIn("--stage qc", pipelines.singlecell_script(gex, "/w"))
        self.assertIn("Chromatin Accessibility", pipelines.singlecell_script(multi, "/w"))

    def test_wrapper_parses(self):
        self.assertTrue(bash_ok(jobs.WRAPPER)[0])


class Permissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("OMICS_CANVAS_AUTH", None)
        self.app = server.App(self.tmp.name, "", 60)

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_only_for_admins(self):
        here = self.app.servers.default()["id"]  # the lab's list is in state/ (not in git): this machine
        with self.assertRaises(PermissionError):
            self.app.target({"server": here}, {"user": "guest", "admin": False})
        target, account, password = self.app.target({"server": here, "account": "someone", "password": "pw"}, {"user": "x", "admin": False})
        self.assertFalse(target.local)  # a password always goes through SSH, never this account
        argv = target.ssh_argv("true")
        self.assertIn("PubkeyAuthentication=no", argv)  # a wrong password never falls back to the key
        self.assertNotIn("pw", " ".join(argv))
        with self.assertRaises(ValueError):
            self.app.target({"server": here, "account": "bad name", "password": "x"}, {"user": "x", "admin": True})

    def test_encrypted_password(self):
        cipher = "rsa:" + base64.b64encode(self.app.key.encrypt("s3cret é".encode())).decode()
        out = server.decrypt_passwords({"a": [{"password": cipher}], "password": cipher, "other": "rsa:x"}, self.app.key)
        self.assertEqual(out["password"], "s3cret é")
        self.assertEqual(out["a"][0]["password"], "s3cret é")
        self.assertEqual(out["other"], "rsa:x")

    def test_other_accounts_cannot_read_this_accounts_files(self):
        d = Path(self.tmp.name) / "out" / ".omics-workbench" / "abcdefabcdef"
        d.mkdir(parents=True)
        secret = Path(self.tmp.name) / "secret"
        secret.write_text("private")
        os.chmod(secret, 0o600)
        (d / "log").symlink_to(secret)
        (d / "run.sh").write_text("")
        job = {"id": "abcdefabcdef", "auth": "password", "account": "someone", "job_dir": str(d), "folder": str(d.parent.parent)}
        self.assertIsNone(self.app.jobs.open_here(job, d / "log"))  # a link
        self.assertIsNone(self.app.jobs.open_here(job, secret))  # this account's file
        with self.assertRaises(PermissionError):
            self.app.jobs.read_file(job, "secret")
        mine = dict(job, auth="key", account="")
        self.assertEqual(self.app.jobs.open_here(mine, secret).read(), b"private")  # the service's own jobs

    def test_control_sockets_differ_by_password(self):
        s = {"id": "x", "name": "X", "host": "10.0.0.1", "port": 22, "user": "u"}
        self.assertNotEqual(remote.Target(s, "u", "a").control_path(), remote.Target(s, "u", "b").control_path())
        self.assertNotEqual(remote.Target(s, "u").control_path(), remote.Target(s, "u", "a").control_path())


class LocalJob(unittest.TestCase):
    """A real job on this machine, in this account: start, follow the log, finish, stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("OMICS_CANVAS_AUTH", None)
        self.app = server.App(self.tmp.name, "", 60)
        self.me = {"user": remote.SERVICE_USER, "admin": True}
        self.server = self.app.servers.default()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, job, states, seconds=30):
        for _ in range(seconds * 2):
            if job["state"] in ("running", "cancelling"):
                self.app.jobs.poll(job)
            if job["state"] in states:
                return
            time.sleep(0.5)
        self.fail("job stayed {} ({})".format(job["state"], job.get("error")))

    def test_run_and_cancel(self):
        if not self.app.servers.default()["host"] in remote.LOCAL:
            self.skipTest("not on a configured server")
        out = Path(self.tmp.name) / "out"
        job = self.app.jobs.create(self.me, "test", "echo", self.server, "", "", str(out),
                                   "echo \"##WB step counting\"\necho \"secret=$WB_SECRET_X\"\nfor i in 1 2 3; do echo line $i; done\n",
                                   secrets_env={"WB_SECRET_X": "hidden", "WB_LOGIN_ENV": "0"})
        self.wait(job, {"done", "failed"})
        self.assertEqual(job["state"], "done", job.get("error"))
        text, offset, size = self.app.jobs.log(job, 0)
        self.assertIn("line 3", text)
        self.assertIn("secret=hidden", text)
        self.assertFalse((Path(job["job_dir"]) / "secrets.env").exists())
        self.assertEqual(job["step"], "counting")
        slow = self.app.jobs.create(self.me, "test", "sleep", self.server, "", "", str(out), "sleep 120\n")
        self.wait(slow, {"running"})
        time.sleep(1)
        self.app.jobs.cancel(slow)
        self.wait(slow, {"cancelled", "failed"})
        self.assertEqual(slow["state"], "cancelled")


class SharedDownloads(unittest.TestCase):
    """A download into a shared (777) folder runs in this service's account without a password; a home folder needs one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("OMICS_CANVAS_AUTH", None)
        root = Path(self.tmp.name)
        self.saved = (server.SHARED_ROOTS, jobs.PRIVATE_RUNS, server.download)
        server.SHARED_ROOTS = (str(root / "nfs") + "/",)
        jobs.PRIVATE_RUNS = root / "private" / "runs"
        self.lab, self.home = root / "nfs" / "lab", root / "nfs" / "home"
        self.lab.mkdir(parents=True)
        self.home.mkdir()
        os.chmod(self.lab, 0o777)
        os.chmod(self.home, 0o755)
        (root / "nfs" / "link").symlink_to(self.lab)
        self.app = server.App(self.tmp.name, "", 60)
        self.guest = {"user": "guest", "admin": False}

    def tearDown(self):
        server.SHARED_ROOTS, jobs.PRIVATE_RUNS, server.download = self.saved
        self.tmp.cleanup()

    def test_which_folders_are_shared(self):
        nfs = Path(self.tmp.name) / "nfs"
        self.assertTrue(server.shared_folder(str(self.lab)))
        self.assertTrue(server.shared_folder(str(self.lab / "new" / "deeper")))  # its nearest existing folder is shared
        self.assertFalse(server.shared_folder(str(self.home / "x")))  # not writable by everyone (a home)
        self.assertFalse(server.shared_folder(str(nfs / "link" / "x")))  # through a link
        self.assertFalse(server.shared_folder(str(self.lab) + "/../home/x"))
        self.assertFalse(server.shared_folder("/tmp/x"))  # outside /nfs
        self.assertFalse(server.shared_folder(""))

    def test_download_without_an_account(self):
        if not self.app.servers.default()["host"] in remote.LOCAL:
            self.skipTest("not on a configured server")
        dest = self.lab / "run1"
        script = "mkdir -p {0} && echo data > {0}/f.txt\n".format(dest)
        server.download = type("FakeDownload", (), {"plan": staticmethod(lambda *a: {"script": script, "root": str(dest), "files": []})})
        self.app.remember_source(self.guest, {"id": "s1", "kind": "https", "label": "example"})
        with self.assertRaises(PermissionError):
            self.app.download("run", {"source": "s1", "dest": str(self.home / "x")}, self.guest)
        with self.assertRaises(PermissionError):
            self.app.check({"kind": "download", "params": {"output": str(self.home / "x")}}, self.guest)
        self.assertTrue(self.app.check({"kind": "download", "params": {"output": str(dest)}}, self.guest)["results"])
        self.assertEqual(os.stat(str(dest)).st_mode & 0o777, 0o777)  # the check made the folder: shared too
        job = self.app.jobs.get(self.app.download("run", {"source": "s1", "dest": str(dest)}, self.guest)["id"])
        self.assertTrue(job["shared"])
        self.assertEqual(job["owner"], "guest")
        self.assertTrue(job["job_dir"].startswith(str(jobs.PRIVATE_RUNS)))  # not in the folder anyone can change
        self.assertEqual(os.stat(str(jobs.PRIVATE_RUNS)).st_mode & 0o777, 0o700)
        for _ in range(60):
            if job["state"] == "running":
                self.app.jobs.poll(job)
            if job["state"] in ("done", "failed"):
                break
            time.sleep(0.5)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(os.stat(str(dest / "f.txt")).st_mode & 0o777, 0o666)  # writable by everyone, as the folder
        self.assertFalse((dest / ".omics-workbench").exists())
        with self.assertRaises(PermissionError):
            self.app.jobs.read_file(job, "f.txt")


class Gateway(unittest.TestCase):
    def test_routes_and_forwarded_for(self):
        import http.server
        import threading
        import urllib.request
        seen = []

        class Backend(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.server.name, self.path, self.headers.get("X-Forwarded-For"), self.headers.get("X-Forwarded-Prefix"),
                             self.headers.get("X-Remote-User")))
                body = json.dumps({"ok": True}).encode() * 2000
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        backends = {}
        for name in ("hub", "canvas", "workbench", "auth", "great"):
            b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
            b.name = name
            threading.Thread(target=b.serve_forever, daemon=True).start()
            backends[name] = b
        sys.path.insert(0, str(APP.parent / "gateway"))
        import gateway as gw
        g = gw.Server(("127.0.0.1", 0), gw.Gateway)
        g.ports = {k: v.server_address[1] for k, v in backends.items()}
        g.ports["jupyter"] = 0
        g.server_name_, g.log, g.log_lock = "test", None, threading.Lock()
        threading.Thread(target=g.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:{}".format(g.server_address[1])
        for path in ("/", "/workbench/api/x", "/genome-canvas/api/y?z=1", "/auth/login"):
            req = urllib.request.Request(base + path, headers={"X-Remote-User": "evil", "X-Forwarded-Prefix": "/evil"})
            data = urllib.request.urlopen(req).read()
            self.assertEqual(len(data), len(json.dumps({"ok": True})) * 2000)
        names = [s[0] for s in seen]
        self.assertEqual(names, ["hub", "workbench", "canvas", "auth"])
        self.assertEqual(seen[2][1], "/api/y?z=1")
        self.assertEqual(seen[2][3], "/genome-canvas")
        self.assertIsNone(seen[0][3])
        self.assertTrue(all(s[2] == "127.0.0.1" and s[4] is None for s in seen))
        r = urllib.request.urlopen(urllib.request.Request(base + "/great"), timeout=5)
        self.assertTrue(r.geturl().endswith("/great/"))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/lab")  # no JupyterLab configured
        # the access log: the lab dashboard's checks are served but not written down
        import io
        g.log = io.StringIO()
        urllib.request.urlopen(urllib.request.Request(base + "/", headers={"User-Agent": "LabDashboard"})).read()
        urllib.request.urlopen(urllib.request.Request(base + "/", headers={"User-Agent": "Mozilla/5.0"})).read()
        time.sleep(0.2)
        lines = g.log.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"Mozilla/5.0"', lines[0])
        for b in backends.values():
            b.shutdown()
        g.shutdown()


if __name__ == "__main__":
    unittest.main()
