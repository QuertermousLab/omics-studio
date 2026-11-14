"""Tests of workbench/download.py (python -m unittest discover workbench/tests).

Offline by default. Tests that reach GEO / ENA / NCBI run only with WB_NET_TESTS=1.
The end-to-end tests run generated scripts against a local HTTP server (wget / curl must be installed).
"""

import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import download  # noqa: E402

NET = os.environ.get("WB_NET_TESTS") == "1"

NOVOGENE = """Dear Customer,

Your sequencing data are ready for download.

HOST:sftp://usftp23.novogene.com  PORT:3022  Account:X000SC00000000-Z01-F001  Password:Xy12abCD

Linux / Mac users:
lftp -c 'set sftp:auto-confirm yes;set net:max-retries 20;open sftp://X000SC00000000-Z01-F001:[Xy12abCD@usftp23.novogene.com](mailto:Xy12abCD@usftp23.novogene.com); mirror --verbose --use-pget-n=8 -c'

Windows users: FileZilla https://urldefense.com/v3/__https://filezilla-project.org/download.php__;!!Nmw4Hv0!xyz$
Check the progress of your project: http://data-deliver.novogeneusa.com/login/X000SC00000000-Z01-F001
Best regards
"""

MEDGENOME = """Hi,

Please find the data access details below.

 * Host: *sftp://portal-us.medgenome.com*
 * Port: *2424*
 * Enter Data Access User ID: *P0000000_01012026*
 * Enter Data Access Password: *q1W2e3R4t5Y6*
 * Browser URL: *https://portal-us.medgenome.com/P0000000_01012026/*

To download one file:
wget --user=P0000000_01012026 --password='q1W2e3R4t5Y6' https://portal-us.medgenome.com/P0000000_01012026/FASTQ/09-1328_-_Replicate_1_R1.fastq.gz
To download the folder:
wget --user=P0000000_01012026 --password='q1W2e3R4t5Y6' -r -np -nH --cut-dirs=1 --reject "index.html*" https://portal-us.medgenome.com/P0000000_01012026/FASTQ/
"""

# the lab's buckets live in its settings (Admin → Workbench); these stand in for them
download.DEFAULT_BUCKETS.update({"example-delivery": {"region": "us-east-2", "profile": "default"},
                                 "partner-bucket": {"region": "us-east-1", "profile": "partner-bucket"}})
S3_EXAMPLE = "s3://example-delivery/WALKUP-00000/260101_SL-EXD_0000_A00000AAAA/1234_1000000000"

GEO_FIXTURE = {
    "accession": "GSE100118", "title": "Test series", "summary": "", "warnings": [],
    "samples": [
        {"gsm": "GSM2672119", "title": "C9.2 (RNA-Seq)", "characteristics": {"stage": "Blastocyst"},
         "runs": [{"run": "SRR5710433", "layout": "PAIRED", "bytes": 144271, "reads": 1266, "fastq": [
             {"url": "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/003/SRR5710433/SRR5710433_1.fastq.gz",
              "ftp_url": "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/003/SRR5710433/SRR5710433_1.fastq.gz",
              "name": "SRR5710433_1.fastq.gz", "bytes": 75524, "md5": "fc34b5486452908dec22f1da243113b3"},
             {"url": "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/003/SRR5710433/SRR5710433_2.fastq.gz",
              "ftp_url": "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/003/SRR5710433/SRR5710433_2.fastq.gz",
              "name": "SRR5710433_2.fastq.gz", "bytes": 68747, "md5": "ee5133d2fd77aa56dc0d83238cf34bc2"}]}],
         "supplementary": [{"name": "GSM2672119_counts.txt.gz",
                            "url": "https://ftp.ncbi.nlm.nih.gov/geo/samples/GSM2672nnn/GSM2672119/suppl/GSM2672119_counts.txt.gz",
                            "bytes": None}]},
        {"gsm": "GSM2672120", "title": "C9.3 / odd: name", "characteristics": {},
         "runs": [{"run": "SRR5710434", "layout": "SINGLE", "bytes": None, "fastq": [], "source": "SRA"},
                  {"run": "SRR5710435", "layout": "SINGLE", "bytes": 10, "fastq": [
                      {"url": "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/005/SRR5710435/SRR5710435.fastq.gz",
                       "ftp_url": "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR571/005/SRR5710435/SRR5710435.fastq.gz",
                       "name": "SRR5710435.fastq.gz", "bytes": 10, "md5": None}]}],
         "supplementary": []},
    ],
    "series_supplementary": [{"name": "GSE100118_rpkm.csv.gz",
                              "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE100nnn/GSE100118/suppl/GSE100118_rpkm.csv.gz",
                              "bytes": 3984588, "bytes_approx": True}],
}


def bash_syntax_ok(script):
    proc = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    return proc.returncode == 0, proc.stderr


def only(text):
    result = download.parse_input(text)
    assert len(result["sources"]) == 1, result
    return result["sources"][0], result["warnings"]


class ParseEmails(unittest.TestCase):
    def test_novogene(self):
        src, warnings = only(NOVOGENE)
        self.assertEqual(src["kind"], "remote")
        self.assertEqual(len(src["transports"]), 1)
        t = src["transports"][0]
        self.assertEqual((t["scheme"], t["host"], t["port"], t["user"]),
                         ("sftp", "usftp23.novogene.com", 3022, "X000SC00000000-Z01-F001"))
        self.assertTrue(t["has_password"])
        self.assertEqual(t["secret_ref"], "PASSWORD")
        self.assertEqual(src["secrets"], {"PASSWORD": "Xy12abCD"})
        self.assertEqual(src["secret_refs"], ["PASSWORD"])
        public = json.dumps(download.public_source(src)) + json.dumps(warnings)
        self.assertNotIn("Xy12abCD", public)
        self.assertTrue(any("data-deliver.novogeneusa.com" in w for w in warnings))
        self.assertNotIn("filezilla", json.dumps(src))

    def test_medgenome(self):
        src, warnings = only(MEDGENOME)
        self.assertEqual(src["kind"], "remote")
        schemes = [(t["scheme"], t["host"], t["port"], t["path"], t["user"]) for t in src["transports"]]
        self.assertEqual(schemes, [("sftp", "portal-us.medgenome.com", 2424, "", "P0000000_01012026"),
                                   ("https", "portal-us.medgenome.com", 443, "/P0000000_01012026/",
                                    "P0000000_01012026")])
        self.assertTrue(all(t["has_password"] and t["secret_ref"] == "PASSWORD" for t in src["transports"]))
        self.assertEqual(src["secrets"], {"PASSWORD": "q1W2e3R4t5Y6"})
        self.assertNotIn("q1W2e3R4t5Y6", json.dumps(download.public_source(src)) + json.dumps(warnings))

    def test_several_sources_in_one_paste(self):
        result = download.parse_input(NOVOGENE + "\n\n" + MEDGENOME + "\n" + S3_EXAMPLE + "\nGEO: GSE100118\n")
        kinds = [s["kind"] for s in result["sources"]]
        self.assertEqual(kinds, ["remote", "remote", "s3", "geo"])
        hosts = [s["transports"][0]["host"] for s in result["sources"][:2]]
        self.assertEqual(hosts, ["usftp23.novogene.com", "portal-us.medgenome.com"])
        self.assertEqual(result["sources"][0]["secrets"]["PASSWORD"], "Xy12abCD")
        self.assertEqual(result["sources"][1]["secrets"]["PASSWORD"], "q1W2e3R4t5Y6")
        self.assertEqual(len({s["id"] for s in result["sources"]}), 4)

    def test_ids_are_stable(self):
        a = download.parse_input(MEDGENOME)["sources"][0]["id"]
        b = download.parse_input(MEDGENOME)["sources"][0]["id"]
        self.assertEqual(a, b)

    def test_generic_labels(self):
        src, _ = only("**Server:** ftp.example.org\nProtocol: FTP\nUsername = bob\nPwd: `p@ss:w0rd!`\n")
        t = src["transports"][0]
        self.assertEqual((t["scheme"], t["host"], t["port"], t["user"]), ("ftp", "ftp.example.org", 21, "bob"))
        self.assertEqual(src["secrets"]["PASSWORD"], "p@ss:w0rd!")

    def test_chinese_labels(self):
        src, _ = only("主机：sftp.example.cn\n端口：2222\n用户名：alice\n密码：Secr3t\n")
        t = src["transports"][0]
        self.assertEqual((t["scheme"], t["host"], t["port"], t["user"]), ("sftp", "sftp.example.cn", 2222, "alice"))
        self.assertEqual(src["secrets"]["PASSWORD"], "Secr3t")

    def test_sshpass_sftp_command(self):
        src, _ = only("sshpass -p 'x1y2' sftp -P 2200 bob@data.example.org:/outgoing/run1")
        t = src["transports"][0]
        self.assertEqual((t["scheme"], t["host"], t["port"], t["user"], t["path"]),
                         ("sftp", "data.example.org", 2200, "bob", "/outgoing/run1"))
        self.assertEqual(src["secrets"]["PASSWORD"], "x1y2")

    def test_curl_user(self):
        src, _ = only("curl -u ann:pw9 -O https://files.example.com/run/a_R1.fastq.gz")
        t = src["transports"][0]
        self.assertEqual((t["scheme"], t["user"], t["path"]), ("https", "ann", "/run/a_R1.fastq.gz"))
        self.assertTrue(src["is_file"])
        self.assertEqual(src["secrets"]["PASSWORD"], "pw9")

    def test_url_with_credentials(self):
        src, _ = only("sftp://carol:p%40ss@seq.example.edu:2222/deliveries/2026/")
        t = src["transports"][0]
        self.assertEqual((t["host"], t["port"], t["user"], t["path"]),
                         ("seq.example.edu", 2222, "carol", "/deliveries/2026/"))
        self.assertEqual(src["secrets"]["PASSWORD"], "p@ss")
        self.assertNotIn("p@ss", json.dumps(download.public_source(src)))

    def test_plain_urls(self):
        result = download.parse_input("ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1000/suppl/GSE1000_RAW.tar\n"
                                      "https://example.org/data/run1/")
        a, b = result["sources"]
        self.assertEqual((a["kind"], a["is_file"]), ("url", True))
        self.assertEqual((b["kind"], b["is_file"], b["transports"][0]["path"]), ("url", False, "/data/run1/"))
        self.assertFalse(a["secrets"])

    def test_accessions(self):
        result = download.parse_input("GSE12345 GSM555 SRP000001 SRR1234567, ERR7654321 DRR000123 PRJNA172563 "
                                      "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE99nnn/GSE99999/suppl/")
        acc = [(s["kind"], s.get("accession")) for s in result["sources"] if s["kind"] in ("geo", "sra")]
        self.assertEqual(acc, [("geo", "GSE12345"), ("geo", "GSM555"), ("sra", "SRP000001"), ("sra", "SRR1234567"),
                               ("sra", "ERR7654321"), ("sra", "DRR000123"), ("sra", "PRJNA172563")])

    def test_nothing(self):
        result = download.parse_input("hello, nothing here")
        self.assertEqual(result["sources"], [])
        self.assertTrue(result["warnings"])

    def test_invalid_host_skipped(self):
        result = download.parse_input("https://exa$(id)mple.com/x.fastq.gz")
        self.assertFalse(any(s["kind"] in ("url", "remote") and "$" in s["transports"][0]["host"]
                             for s in result["sources"]))


class ParseS3(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(download, "aws_profiles", return_value={"default"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_known_bucket(self):
        src, warnings = only(S3_EXAMPLE)
        self.assertEqual((src["kind"], src["bucket"], src["key"], src["profile"], src["region"]),
                         ("s3", "example-delivery", "WALKUP-00000/260101_SL-EXD_0000_A00000AAAA/1234_1000000000",
                          "default", "us-east-2"))
        self.assertFalse(src["is_file"])
        self.assertEqual(warnings, [])

    def test_missing_profile_is_a_warning(self):
        src, warnings = only("s3://partner-bucket/project/run7/")
        self.assertEqual((src["profile"], src["region"]), ("partner-bucket", "us-east-1"))
        self.assertTrue(src["profile_missing"])
        self.assertTrue(any("partner-bucket" in w and "credentials" in w for w in warnings))

    def test_unknown_bucket(self):
        src, warnings = only("s3://some-other-bucket/a/b.fastq.gz")
        self.assertEqual((src["profile"], src["region"], src["is_file"]), ("default", None, True))
        self.assertTrue(any("some-other-bucket" in w for w in warnings))

    def test_buckets_extendable(self):
        with mock.patch.dict(download.DEFAULT_BUCKETS, {"lab-bucket": {"profile": "lab", "region": "us-west-2"}}):
            src, _ = only("s3://lab-bucket/x/")
            self.assertEqual((src["profile"], src["region"]), ("lab", "us-west-2"))
        download.configure({"buckets": {"cfg-bucket": {"profile": "p2", "region": "eu-west-1"}}})
        try:
            self.assertEqual(download.bucket_settings("cfg-bucket")["region"], "eu-west-1")
        finally:
            download.DEFAULT_BUCKETS.pop("cfg-bucket", None)

    def test_destination_levels(self):
        src, _ = only(S3_EXAMPLE)
        root = lambda **o: download.destination_preview(src, "/data/x", None, o)["root"]  # noqa: E731
        self.assertEqual(root(), "/data/x/1234_1000000000")
        self.assertEqual(root(keep_levels=2), "/data/x/260101_SL-EXD_0000_A00000AAAA/1234_1000000000")
        self.assertEqual(root(strip_levels=1), "/data/x/260101_SL-EXD_0000_A00000AAAA/1234_1000000000")
        self.assertEqual(root(strip_levels=0), "/data/x/WALKUP-00000/260101_SL-EXD_0000_A00000AAAA/1234_1000000000")
        self.assertEqual(root(strip_levels=3), "/data/x")
        self.assertEqual(root(strip_levels=9), "/data/x")
        self.assertEqual(root(subfolder="broad/run1"), "/data/x/broad/run1/1234_1000000000")
        with self.assertRaises(ValueError):
            root(subfolder="../etc")
        with self.assertRaises(ValueError):
            download.destination_preview(src, "relative/dir", None, {})

    def test_plan_s3(self):
        src, _ = only(S3_EXAMPLE)
        entries = [{"path": "a", "bytes": None, "dir": True}, {"path": "a/x_R1.fastq.gz", "bytes": 10, "dir": False},
                   {"path": "md5sum.txt", "bytes": 5, "dir": False}]
        p = download.plan(src, {}, None, "/data/x", {"entries": entries})
        self.assertEqual(p["root"], "/data/x/1234_1000000000")
        self.assertEqual([f["dest"] for f in p["files"]], ["/data/x/1234_1000000000/a/x_R1.fastq.gz",
                                                           "/data/x/1234_1000000000/md5sum.txt"])
        s = p["script"]
        self.assertIn("rclone copy :s3,env_auth:example-delivery/WALKUP-00000/260101_SL-EXD_0000_A00000AAAA/"
                      "1234_1000000000 \"$WB_ROOT\"", s)
        self.assertIn("--s3-no-check-bucket --fast-list --transfers 16 --multi-thread-streams 16", s)
        self.assertIn("export AWS_PROFILE=default", s)
        self.assertIn("WB_REGION=us-east-2", s)
        self.assertIn("aws s3 sync", s)
        self.assertEqual(p["secrets"], {})
        self.assertTrue(bash_syntax_ok(s)[0], bash_syntax_ok(s)[1])
        sel = download.plan(src, {}, ["a"], "/data/x", {"method": "rclone", "entries": entries})
        self.assertIn("--filter-from", sel["script"])
        self.assertNotIn("aws s3 sync", sel["script"].split("WB_DONE=0")[1])
        self.assertEqual([f["dest"] for f in sel["files"]], ["/data/x/1234_1000000000/a/x_R1.fastq.gz"])
        self.assertTrue(bash_syntax_ok(sel["script"])[0])

    def test_plan_s3_unknown_region(self):
        src, _ = only("s3://some-other-bucket/run/")
        p = download.plan(src, {}, None, "/data/y", {})
        self.assertIn("get-bucket-location --bucket some-other-bucket", p["script"])
        self.assertTrue(bash_syntax_ok(p["script"])[0])


class PlanRemote(unittest.TestCase):
    def test_novogene_plan(self):
        src, _ = only(NOVOGENE)
        p = download.plan(src, src["secrets"], None, "/nfs/lab/deliveries", {})
        self.assertEqual(p["root"], "/nfs/lab/deliveries/X000SC00000000-Z01-F001")
        self.assertEqual(p["secrets"], {"PASSWORD": "Xy12abCD"})
        s = p["script"]
        self.assertNotIn("Xy12abCD", s)
        self.assertIn("wb_probe usftp23.novogene.com 3022", s)
        self.assertIn("open --env-password -u", s)
        self.assertIn("mirror -c --parallel=4 --use-pget-n=8", s)
        self.assertIn("wb_sftp 3022 PASSWORD X000SC00000000-Z01-F001@usftp23.novogene.com", s)
        self.assertIn("wb_scp_0", s)
        self.assertIn(': "${WB_SECRET_PASSWORD:?', s)
        ok, err = bash_syntax_ok(s)
        self.assertTrue(ok, err)

    def test_medgenome_plan_and_preview(self):
        src, _ = only(MEDGENOME)
        entries = [{"path": "FASTQ", "bytes": None, "dir": True},
                   {"path": "FASTQ/09-1328_-_Replicate_1_R1.fastq.gz", "bytes": 100, "dir": False},
                   {"path": "FASTQ/09-1328_-_Replicate_1_R2.fastq.gz", "bytes": 100, "dir": False},
                   {"path": "MD5.txt", "bytes": 10, "dir": False}]
        p = download.plan(src, src["secrets"], None, "/data/d", {"entries": entries})
        self.assertEqual(p["root"], "/data/d/P0000000_01012026")
        self.assertEqual([f["dest"] for f in p["files"]],
                         ["/data/d/P0000000_01012026/FASTQ/09-1328_-_Replicate_1_R1.fastq.gz",
                          "/data/d/P0000000_01012026/FASTQ/09-1328_-_Replicate_1_R2.fastq.gz",
                          "/data/d/P0000000_01012026/MD5.txt"])
        s = p["script"]
        self.assertNotIn("q1W2e3R4t5Y6", s)
        self.assertIn("wb_probe portal-us.medgenome.com 2424", s)
        self.assertIn("wb_probe portal-us.medgenome.com 443", s)
        self.assertIn("--cut-dirs=1", s)
        self.assertIn("https://portal-us.medgenome.com/P0000000_01012026/", s)
        self.assertLess(s.index("wb_try_0; then"), s.index("wb_try_1; then"))
        self.assertTrue(bash_syntax_ok(s)[0])
        # https first when chosen (the listing came from it)
        p2 = download.plan(src, src["secrets"], ["FASTQ"], "/data/d", {"transport": 1, "strip_levels": 1,
                                                                        "entries": entries})
        self.assertEqual(p2["root"], "/data/d")
        self.assertEqual(len(p2["files"]), 2)
        self.assertLess(p2["script"].index("wb_try_1; then"), p2["script"].index("wb_try_0; then"))
        self.assertTrue(bash_syntax_ok(p2["script"])[0])

    def test_methods(self):
        src, _ = only(NOVOGENE)
        for method in ("lftp", "sftp", "scp"):
            p = download.plan(src, src["secrets"], None, "/data/d", {"method": method})
            body = p["script"].split("wb_try_0() {")[1]
            self.assertIn("Trying", body)
            self.assertTrue(bash_syntax_ok(p["script"])[0])
        with self.assertRaises(ValueError):
            download.plan(src, src["secrets"], None, "/data/d", {"method": "wget"})
        with self.assertRaises(ValueError):
            download.plan(src, src["secrets"], None, "/data/d", {"method": "nonsense"})

    def test_missing_secret(self):
        src, _ = only(NOVOGENE)
        src = download.public_source(src)
        with self.assertRaises(ValueError):
            download.plan(src, {}, None, "/data/d", {})

    def test_quoting_of_odd_paths(self):
        src, _ = only("ftp://ftp.example.org/pub/run 1/")  # the URL stops at the space
        self.assertEqual(src["transports"][0]["path"], "/pub/run")
        entries = [{"path": "it's $(id) `x`.fastq.gz", "bytes": 1, "dir": False}]
        p = download.plan(src, {}, None, "/data/a b", {"entries": entries})
        self.assertEqual(p["files"][0]["dest"], "/data/a b/run/it's $(id) `x`.fastq.gz")
        self.assertTrue(bash_syntax_ok(p["script"])[0])
        self.assertIn("'/data/a b/run'", p["script"])
        with self.assertRaises(ValueError):
            download.plan(src, {}, None, "/data/x\ny", {})

    def test_single_file_url(self):
        src, _ = only("ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1000/suppl/filelist.txt")
        p = download.plan(src, {}, None, "/data/f", {})
        self.assertEqual(p["root"], "/data/f")
        self.assertEqual(p["files"][0]["dest"], "/data/f/filelist.txt")
        p = download.plan(src, {}, None, "/data/f", {"keep_levels": 1})
        self.assertEqual(p["files"][0]["dest"], "/data/f/suppl/filelist.txt")
        self.assertIn("pget -c -n 8", p["script"])
        self.assertTrue(bash_syntax_ok(p["script"])[0])

    def test_list_remote_unreachable_redacts(self):
        src, _ = only("sftp://dan:VerySecret1@127.0.0.1:1/x/")
        out = download.list_remote(src, src["secrets"], timeout=5)
        self.assertEqual(out["entries"], [])
        self.assertTrue(out["errors"])
        self.assertNotIn("VerySecret1", json.dumps(out))


class PlanGeo(unittest.TestCase):
    def setUp(self):
        self.src = download.parse_input("GSE100118")["sources"][0]

    def test_all_run_names(self):
        p = download.plan(self.src, {}, None, "/data/geo", {"lookup": GEO_FIXTURE})
        dests = [f["dest"] for f in p["files"]]
        self.assertEqual(p["root"], "/data/geo/GSE100118")
        self.assertEqual(dests, ["/data/geo/GSE100118/fastq/SRR5710433_1.fastq.gz",
                                 "/data/geo/GSE100118/fastq/SRR5710433_2.fastq.gz",
                                 "/data/geo/GSE100118/fastq/SRR5710434.fastq.gz",
                                 "/data/geo/GSE100118/fastq/SRR5710435.fastq.gz",
                                 "/data/geo/GSE100118/supplementary/GSE100118_rpkm.csv.gz"])
        s = p["script"]
        self.assertIn("fc34b5486452908dec22f1da243113b3", s)
        self.assertIn("wb_sra_dump SRR5710434 '' /data/geo/GSE100118/fastq/SRR5710434.fastq.gz", s)
        self.assertIn("printf '%s\\n' SRR5710434 >> \"$WB_TMP/sra_runs\"", s)
        # approximate GEO sizes are not used as an exact size check
        self.assertNotIn("3984588", s)
        self.assertTrue(bash_syntax_ok(s)[0], bash_syntax_ok(s)[1])

    def test_gsm_title_names_and_selection(self):
        sel = [{"type": "fastq", "run": "SRR5710433"}, {"type": "fastq", "run": "SRR5710434"},
               {"type": "fastq", "run": "SRR5710435"},
               {"type": "supplementary", "url": GEO_FIXTURE["samples"][0]["supplementary"][0]["url"]}]
        p = download.plan(self.src, {}, sel, "/data/geo", {"lookup": GEO_FIXTURE, "rename": "gsm_title",
                                                           "strip_levels": 1})
        self.assertEqual([f["dest"] for f in p["files"]],
                         ["/data/geo/fastq/GSM2672119_C9.2_RNA-Seq_R1.fastq.gz",
                          "/data/geo/fastq/GSM2672119_C9.2_RNA-Seq_R2.fastq.gz",
                          "/data/geo/supplementary/GSM2672119_counts.txt.gz",
                          "/data/geo/fastq/GSM2672120_C9.3_odd_name_SRR5710434_R1.fastq.gz",
                          "/data/geo/fastq/GSM2672120_C9.3_odd_name_SRR5710435_R1.fastq.gz"])
        self.assertTrue(bash_syntax_ok(p["script"])[0])

    def test_methods(self):
        p = download.plan(self.src, {}, ["SRR5710433"], "/data/geo", {"lookup": GEO_FIXTURE, "method": "curl"})
        self.assertEqual(len(p["files"]), 2)
        self.assertTrue(bash_syntax_ok(p["script"])[0])
        with self.assertRaises(ValueError):
            download.plan(self.src, {}, None, "/data/geo", {"lookup": GEO_FIXTURE, "method": "lftp"})
        with self.assertRaises(ValueError):
            download.plan(self.src, {}, [{"type": "fastq", "run": "SRR000000"}], "/data/geo",
                          {"lookup": GEO_FIXTURE})


class _Handler(http.server.SimpleHTTPRequestHandler):
    auth = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.auth and self.headers.get("Authorization") != "Basic " + self.auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()

    do_HEAD = do_GET


@unittest.skipUnless(shutil.which("wget") and shutil.which("curl"), "wget and curl are needed")
class EndToEndLocalHttp(unittest.TestCase):
    """Generated scripts run for real against a local web server (with basic auth)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-dl-test-")
        self.served = os.path.join(self.tmp, "served")
        run = os.path.join(self.served, "delivery", "run 1")
        os.makedirs(run)
        self.files = {"run 1/a_R1.fastq.gz": b"@r1\nACGT\n+\nIIII\n" * 50,
                      "run 1/a_R2.fastq.gz": b"@r2\nTGCA\n+\nIIII\n" * 50}
        lines = []
        for rel, data in self.files.items():
            with open(os.path.join(self.served, "delivery", rel), "wb") as handle:
                handle.write(data)
            lines.append("%s  %s" % (hashlib.md5(data).hexdigest(), rel.split("/")[-1]))
        with open(os.path.join(run, "MD5.txt"), "w") as handle:
            handle.write("\n".join(lines) + "\n")
        handler = functools.partial(_Handler, directory=self.served)
        _Handler.auth = base64.b64encode(b"eve:pa ss\"w").decode()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        _Handler.auth = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_plan(self, p):
        job = os.path.join(self.tmp, "job")
        os.makedirs(job, exist_ok=True)
        env = dict(os.environ, WB_JOB_DIR=job, WB_RETRY_DELAY="1")
        env.update({download.SECRET_ENV_PREFIX + k: v for k, v in p["secrets"].items()})
        return subprocess.run(["bash", "-Eeuo", "pipefail", "-c", p["script"]], env=env, cwd=job,
                              capture_output=True, text=True, timeout=300)

    def source(self):
        text = ("Server: 127.0.0.1\nUser: eve\nPassword: *pa ss\"w*\n"
                "Browser: http://127.0.0.1:%d/delivery/\n" % self.port)
        src, _ = only(text.replace("pa ss\"w", "PLACEHOLDER"))
        src["secrets"]["PASSWORD"] = "pa ss\"w"   # a password with a space cannot be pasted as a label value
        return src

    def test_listing_download_and_md5(self):
        src = self.source()
        http_index = next(i for i, t in enumerate(src["transports"]) if t["scheme"] == "http")
        listing = download.list_remote({"kind": "remote", "transports": [src["transports"][http_index]]},
                                       src["secrets"], timeout=20)
        self.assertEqual(listing["errors"], [], listing)
        paths = sorted(e["path"] for e in listing["entries"] if not e["dir"])
        self.assertEqual(paths, ["run 1/MD5.txt", "run 1/a_R1.fastq.gz", "run 1/a_R2.fastq.gz"])
        dest = os.path.join(self.tmp, "dest")
        p = download.plan(src, src["secrets"], None, dest, {"transport": http_index, "entries": listing["entries"],
                                                            "method": "wget"})
        self.assertNotIn('pa ss"w', p["script"])
        proc = self.run_plan(p)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("md5: 2 ok, 0 mismatched", proc.stdout)
        for f in p["files"]:
            self.assertTrue(os.path.isfile(f["dest"]), f["dest"])
        with open(os.path.join(p["root"], "run 1", "a_R1.fastq.gz"), "rb") as handle:
            self.assertEqual(handle.read(), self.files["run 1/a_R1.fastq.gz"])
        # curl, file by file from the listing, resumes / skips complete files
        p2 = download.plan(src, src["secrets"], ["run 1/a_R2.fastq.gz"], os.path.join(self.tmp, "dest2"),
                           {"transport": http_index, "entries": listing["entries"], "method": "curl"})
        proc = self.run_plan(p2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(p2["files"]), 1)
        self.assertTrue(os.path.isfile(p2["files"][0]["dest"]))

    def test_md5_mismatch_fails(self):
        with open(os.path.join(self.served, "delivery", "run 1", "a_R1.fastq.gz"), "ab") as handle:
            handle.write(b"corrupt")
        src = self.source()
        http_index = next(i for i, t in enumerate(src["transports"]) if t["scheme"] == "http")
        p = download.plan(src, src["secrets"], None, os.path.join(self.tmp, "dest"),
                          {"transport": http_index, "method": "wget"})
        proc = self.run_plan(p)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("md5 MISMATCH", proc.stdout)

    def test_wrong_password_fails(self):
        src = self.source()
        src["secrets"]["PASSWORD"] = "wrong"
        http_index = next(i for i, t in enumerate(src["transports"]) if t["scheme"] == "http")
        p = download.plan(src, src["secrets"], None, os.path.join(self.tmp, "dest"),
                          {"transport": http_index, "method": "wget"})
        p["script"] = p["script"].replace("wb_retry 4 ", "wb_retry 1 ")
        proc = self.run_plan(p)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("every transport and tool failed", proc.stdout + proc.stderr)


@unittest.skipUnless(NET, "set WB_NET_TESTS=1 to reach GEO / ENA / NCBI")
class Network(unittest.TestCase):
    def test_geo_sample(self):
        info = download.geo_lookup("GSM2672119")
        self.assertEqual(info["accession"], "GSM2672119")
        runs = info["samples"][0]["runs"]
        self.assertEqual(runs[0]["run"], "SRR5710433")
        self.assertEqual([f["md5"] for f in runs[0]["fastq"]],
                         ["fc34b5486452908dec22f1da243113b3", "ee5133d2fd77aa56dc0d83238cf34bc2"])

    def test_geo_series(self):
        info = download.geo_lookup("GSE100118")
        self.assertTrue(info["title"])
        self.assertGreater(len(info["samples"]), 10)
        self.assertTrue(any(s["runs"] for s in info["samples"]))
        self.assertTrue(info["series_supplementary"])
        self.assertTrue(info["series_supplementary"][0]["url"].startswith("https://ftp.ncbi.nlm.nih.gov/geo/"))

    def test_sra_run(self):
        info = download.geo_lookup("SRR390728")
        run = info["samples"][0]["runs"][0]
        self.assertEqual(run["layout"], "PAIRED")
        self.assertEqual(len(run["fastq"]), 2)

    def test_list_public_ftp(self):
        src, _ = only("ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1000/")
        out = download.list_remote(src, {}, timeout=60)
        paths = {e["path"] for e in out["entries"]}
        self.assertIn("suppl/GSE1000_RAW.tar", paths, out)


if __name__ == "__main__":
    unittest.main()
