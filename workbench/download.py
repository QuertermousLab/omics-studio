"""Omics Workbench: download sequencing data (FASTQ and processed files).

Standard library only (Python 3.8+). The HTTP server (``workbench/server.py``) calls four functions:

``parse_input(text)``
    Reads whatever the user pasted and returns the sources it describes:

    * a direct address (``ftp://``, ``ftps://``, ``sftp://``, ``http(s)://``), with or without ``user:password@``;
    * a whole delivery e-mail copied from a sequencing provider (Novogene, MedGenome, ...): labelled fields
      (Host / Server, Port, Account / User / User ID / Login, Password / Pass / Pwd, also in Chinese), URLs with
      credentials, ``lftp`` / ``wget`` / ``curl`` / ``sftp`` / ``scp`` / ``sshpass`` command lines.  Markdown and
      mail-client damage is repaired first (``[pw@host](mailto:pw@host)``, ``<mailto:...>``, ``*bold*``), links
      wrapped by urldefense are ignored unless they point at the delivery host, and web pages that are not data
      (progress / login pages) are skipped with a warning.  One e-mail gives ONE source whose ``transports`` are the
      alternative ways to reach the same delivery (e.g. sftp host:port, then the https browser URL), tried in order;
    * ``s3://bucket/key`` URIs, read with the AWS profile / region of :data:`DEFAULT_BUCKETS` (the ``aws_download``
      shell helper's table; the server extends it from its settings) or, for unknown buckets, the ``default``
      profile and the bucket's own region (``aws s3api get-bucket-location``);
    * GEO accessions (GSE / GSM) and SRA / ENA accessions (SRP, SRX, SRR, ERR, DRR, PRJNA, ...).

    Passwords are only ever in ``source["secrets"]`` (``{"PASSWORD": ...}``; the server strips that field before
    anything goes to the browser); transports carry ``has_password`` and the name of their secret.

``geo_lookup(accession)``
    Series / sample metadata from GEO (``acc.cgi?form=text``), runs and FASTQ files (size, md5) from the ENA portal
    API, NCBI E-utilities ``runinfo`` when ENA does not have a run yet, and supplementary files (GEO https folders).

``list_remote(source, secrets)``
    What a source contains (recursive listing for the preview), trying its transports and tools in order:
    rclone / aws for s3, lftp / OpenSSH sftp (through ``sshpass -e``) / ftplib for (s)ftp, urllib index pages
    for http(s).

``plan(source, secrets, selection, dest, options)``
    The final paths (the preview the page shows) and the bash body of the download job.  The body runs under
    ``bash -Eeuo pipefail`` in the job wrapper (``jobs.py``), which exports the secrets as ``WB_SECRET_<NAME>``.
    It probes every transport first (``/dev/tcp`` with ``timeout``), then tries tools in order with retries and
    back-off, resuming where it can (lftp ``mirror -c`` / ``pget -c``, ``sftp get -a``, ``wget -c``,
    ``curl -C -``, rclone), verifies md5 sums (ENA's, or ``md5sum.txt`` / ``MD5.txt`` / ``*.md5`` files found in
    a delivery) and exits non-zero when anything failed.  Passwords never appear on a command line or in the
    script: lftp reads ``LFTP_PASSWORD`` (``open --env-password``), sshpass ``SSHPASS`` (``-e``), wget a config
    on its standard input, curl ``-K -``.

Destination layout (``plan`` and :func:`destination_preview` agree exactly with what the script writes)::

    <dest>/[<subfolder>/]<kept part of the source folder>/<paths inside the delivery>

The source folder is the transport path (or the s3 key, or the account name for an sftp home folder); by default
only its last component (the delivered folder) is kept.  ``options["strip_levels"]`` removes that many leading
components instead (as ``wget --cut-dirs``), ``options["keep_levels"]`` keeps that many trailing ones (as the
``levels`` argument of ``aws_download``).  GEO / SRA downloads go to ``<root>/fastq/`` and
``<root>/supplementary/`` with ``<root> = <dest>/[<subfolder>/]<accession>``.
"""

import base64
import ftplib
import hashlib
import html
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["DEFAULT_BUCKETS", "parse_input", "geo_lookup", "list_remote", "plan", "destination_preview",
           "public_source", "configure", "aws_profiles", "bucket_settings", "resolve_bucket_region"]

USER_AGENT = "omics-workbench/1.0"
SECRET_ENV_PREFIX = "WB_SECRET_"

#: s3 bucket -> AWS profile and region (as in the ``aws_download`` shell helper).  The server adds entries from
#: its settings (``DEFAULT_BUCKETS.setdefault(bucket, {"profile": ..., "region": ...})``); unknown buckets use the
#: ``default`` profile and the region AWS reports for the bucket.
DEFAULT_BUCKETS = {}  # the lab's buckets are set in Admin → Workbench (state/workbench/settings.json, not in git)

DEFAULT_PORTS = {"sftp": 22, "ftp": 21, "ftps": 21, "http": 80, "https": 443, "s3": 443}

#: tools tried in order for each scheme ("auto"); ``options["method"]`` picks one of them
TOOLS = {
    "sftp": ["lftp", "sftp", "scp"],
    "ftp": ["lftp", "wget", "curl"],
    "ftps": ["lftp", "curl"],
    "http": ["wget", "lftp", "curl"],
    "https": ["wget", "lftp", "curl"],
    "s3": ["rclone", "aws"],
}
METHODS = ("auto", "lftp", "sftp", "scp", "wget", "curl", "rclone", "aws", "fasterq-dump")

ENA_FIELDS = ("run_accession,experiment_accession,sample_accession,secondary_sample_accession,sample_alias,"
              "experiment_alias,sample_title,experiment_title,study_title,study_accession,secondary_study_accession,"
              "library_layout,library_strategy,read_count,base_count,fastq_ftp,fastq_bytes,fastq_md5,scientific_name")
ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
GEO_ACC = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

DATA_EXTENSIONS = (
    ".fastq", ".fq", ".fastq.gz", ".fq.gz", ".gz", ".bz2", ".xz", ".zst", ".tar", ".tgz", ".zip", ".7z", ".bam",
    ".bai", ".cram", ".crai", ".sam", ".vcf", ".bcf", ".tbi", ".csi", ".bed", ".bedpe", ".bw", ".bigwig", ".bb",
    ".h5", ".hdf5", ".h5ad", ".h5seurat", ".loom", ".rds", ".rda", ".rdata", ".mtx", ".tsv", ".csv", ".txt",
    ".md5", ".json", ".fa", ".fasta", ".fna", ".gtf", ".gff", ".gff3", ".sra", ".xlsx", ".xls", ".pdf", ".html",
    ".bcl", ".cbcl", ".fast5", ".pod5", ".parquet", ".zarr", ".npz", ".bedgraph", ".wig", ".sha256",
)

ACCESSION_RE = re.compile(r"(?<![A-Za-z0-9_])(GSE\d{1,8}|GSM\d{1,9}|(?:SRP|ERP|DRP|SRX|ERX|DRX|SRR|ERR|DRR|SRS|ERS|DRS)"
                          r"\d{5,10}|PRJ(?:NA|EB|DB)\d{1,10})(?![A-Za-z0-9_])")
URL_RE = re.compile(r"(?i)(?<![\w.+-])(s3|sftp|ftps|ftp|https?)://[^\s<>\"'`*\[\]{}|\\^]+")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}$|"
                     r"^\d{1,3}(?:\.\d{1,3}){3}$|^localhost$")
URLDEFENSE_V3 = re.compile(r"https?://urldefense(?:\.proofpoint)?\.com/v3/__(?P<u>.+?)__;[^\s<>\"')\]]*", re.I)
URLDEFENSE_V2 = re.compile(r"https?://urldefense\.proofpoint\.com/v2/url\?u=(?P<u>[^&\s]+)[^\s<>\"')\]]*", re.I)
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{1,254}$")
SAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._/+-]")

_LABELS = [
    ("protocol", r"protocol|transfer\s+protocol|协议"),
    ("host", r"(?:s?ftp\s+)?(?:host\s*name|host|server\s+address|server|ip\s+address|address)|主机(?:名|地址)?|服务器(?:地址)?|地址"),
    ("port", r"port(?:\s+number)?|端口(?:号)?"),
    ("user", r"(?:data\s+access\s+)?user\s*(?:name|id)?|account(?:\s*(?:name|id))?|log\s*in(?:\s*(?:name|id))?|"
             r"账号|帐号|账户|帐户|用户名|用户"),
    ("password", r"(?:data\s+access\s+)?(?:pass\s*word|passwd|pass|pwd)|密码|口令"),
]
LABEL_RE = re.compile(r"(?<![\w.\-/])(?:" + "|".join("(?P<%s>%s)" % (k, v) for k, v in _LABELS) + r")"
                      r"[*_`]*[ \t]*(?:\([^)\n]{0,20}\))?[ \t]*[:=：][*_`]*[ \t]*(?P<nl>\n[ \t]*)?(?P<value>\S+)", re.I)
COMMANDS = {"wget", "curl", "lftp", "sshpass", "sftp", "scp", "ssh", "rclone", "aws"}


class DownloadError(Exception):
    """A listing or lookup failed (the message is safe to show: secrets are redacted)."""


# --------------------------------------------------------------------------------------------- settings / AWS

def configure(settings=None):
    """Merge ``settings["buckets"]`` (or ``s3_buckets``) {bucket: {"profile", "region"}} into DEFAULT_BUCKETS."""
    settings = settings or {}
    for key in ("buckets", "s3_buckets"):
        for bucket, conf in (settings.get(key) or {}).items():
            DEFAULT_BUCKETS[bucket] = dict(DEFAULT_BUCKETS.get(bucket, {}), **(conf or {}))
    return DEFAULT_BUCKETS


def aws_profiles():
    """Names of the profiles in ~/.aws/credentials and ~/.aws/config (section names only; values are never read)."""
    names = set()
    home = os.path.expanduser("~")
    files = [(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.join(home, ".aws", "credentials"), False),
             (os.environ.get("AWS_CONFIG_FILE") or os.path.join(home, ".aws", "config"), True)]
    for path, is_config in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    m = re.match(r"^\s*\[\s*([^\]]+?)\s*\]\s*$", line)
                    if m:
                        name = m.group(1)
                        if is_config and name.startswith("profile "):
                            name = name[8:].strip()
                        names.add(name)
        except OSError:
            continue
    return names


def bucket_settings(bucket):
    """{"profile", "region" (None when unknown), "known"} for a bucket (DEFAULT_BUCKETS first)."""
    conf = DEFAULT_BUCKETS.get(bucket) or {}
    return {"profile": conf.get("profile") or "default", "region": conf.get("region") or None,
            "known": bucket in DEFAULT_BUCKETS}


_REGION_CACHE = {}


def resolve_bucket_region(bucket, profile="default", timeout=20):
    """The bucket's region: DEFAULT_BUCKETS, ``aws s3api get-bucket-location``, then S3's x-amz-bucket-region header."""
    region = bucket_settings(bucket)["region"] or _REGION_CACHE.get(bucket)
    if region:
        return region
    if shutil.which("aws"):
        env = dict(os.environ, AWS_PROFILE=profile or "default")
        try:
            proc = subprocess.run(["aws", "s3api", "get-bucket-location", "--bucket", bucket, "--query",
                                   "LocationConstraint", "--output", "text"], env=env, capture_output=True,
                                  text=True, timeout=timeout)
            out = proc.stdout.strip()
            if proc.returncode == 0 and out:
                region = {"None": "us-east-1", "null": "us-east-1", "EU": "eu-west-1"}.get(out, out)
        except (OSError, subprocess.SubprocessError):
            pass
    if not region:
        try:
            req = urllib.request.Request("https://%s.s3.amazonaws.com/" % bucket, method="HEAD",
                                         headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    region = resp.headers.get("x-amz-bucket-region")
            except urllib.error.HTTPError as error:
                region = error.headers.get("x-amz-bucket-region")
        except (OSError, ValueError):
            region = None
    if region:
        _REGION_CACHE[bucket] = region
    return region


# --------------------------------------------------------------------------------------------- small helpers

def _sid(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:10]


def public_source(source):
    """A copy of a source without its secrets (what may go to the browser)."""
    return {k: v for k, v in source.items() if k != "secrets"}


def _redact(text, secrets):
    text = str(text)
    for value in (secrets or {}).values():
        if value:
            text = text.replace(str(value), "***")
            text = text.replace(urllib.parse.quote(str(value), safe=""), "***")
    return text


def _unwrap(value):
    """Remove symmetric wrappers (*x*, **x**, `x`, 'x', "x", [x], (x), <x>) around a pasted value."""
    pairs = {"*": "*", "_": "_", "`": "`", "'": "'", '"': '"', "[": "]", "(": ")", "<": ">", "“": "”", "‘": "’"}
    changed = True
    while changed and len(value) >= 2:
        changed = False
        first = value[0]
        if first in pairs and value.endswith(pairs[first]):
            value = value[1:-1]
            changed = True
    return value


def _clean_value(value, kind):
    value = _unwrap(value)
    if kind != "password":
        value = value.strip("*`'\"")
        value = value.rstrip(",;.)]>").lstrip("([<")
    return value


def _default_port(scheme):
    return DEFAULT_PORTS.get(scheme, 0)


def _looks_like_file(path):
    if not path or path.endswith("/"):
        return False
    name = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if name.startswith("."):
        return False
    return any(name.endswith(ext) for ext in DATA_EXTENSIONS) or bool(re.search(r"\.[a-z][a-z0-9]{0,5}$", name))


def _components(path):
    return [c for c in (path or "").split("/") if c and c != "."]


def _safe_name(text, limit=80):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._-")
    return text[:limit].rstrip("._-") or "sample"


def _check_text(value, what):
    if value is None:
        return value
    if any(ch in str(value) for ch in ("\n", "\r", "\0", "\t")):
        raise ValueError("%s contains a line break, tab or NUL character" % what)
    return value


def _esc_glob(path):
    """Backslash-escape everything but plain characters (sftp batch files, scp remote paths)."""
    return SAFE_PATH_CHARS.sub(lambda m: "\\" + m.group(0), path)


def _lftp_quote(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tcp_ok(host, port, timeout=8):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------------------------- parse_input

def _normalise_text(text):
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in ("\u00a0", "\u2007", "\u202f", "\u3000"):
        text = text.replace(ch, " ")
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"):
        text = text.replace(ch, "")
    return text


def _unwrap_protected(text):
    """Cut urldefense-wrapped links out of the text; return (text, [inner URLs])."""
    wrapped = []

    def v3(m):
        wrapped.append(m.group("u"))
        return " "

    def v2(m):
        inner = m.group("u").replace("_", "/")
        inner = re.sub(r"-([0-9A-Fa-f]{2})", lambda h: chr(int(h.group(1), 16)), inner)
        wrapped.append(inner)
        return " "

    text = URLDEFENSE_V3.sub(v3, text)
    text = URLDEFENSE_V2.sub(v2, text)
    return text, wrapped


def _demangle(text):
    """Undo what mail clients and markdown renderers do to addresses inside commands."""
    def md_link(m):
        label, target = m.group(1), m.group(2)
        if target.lower().startswith("mailto:"):
            return label
        if re.match(r"(?i)^(s3|sftp|ftps?|https?)://", label.strip()) or label.strip() == target:
            return label
        if re.match(r"(?i)^(s3|sftp|ftps?|https?)://", target):
            return target
        return label
    text = re.sub(r"\[([^\]\n]{0,500})\]\(([^)\s]{0,2000})\)", md_link, text)
    text = re.sub(r"<mailto:[^>\s]*>", "", text, flags=re.I)
    text = re.sub(r"(?i)<((?:s3|sftp|ftps?|https?)://[^>\s]+)>", r"\1", text)
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        text = text.replace(a, b)
    return text


def _find_urls(text):
    out = []
    for m in URL_RE.finditer(text):
        url = m.group(0)
        while url and url[-1] in ".,;:!?)'\"":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        if len(url) > len(m.group(1)) + 3:
            out.append({"start": m.start(), "end": m.start() + len(url), "url": url})
    return out


def _parse_url(url):
    """dict(scheme, host, port, path, user, password, url) or None."""
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if scheme == "s3":
        bucket = parts.netloc
        return {"scheme": "s3", "host": bucket, "port": None, "path": parts.path.lstrip("/"), "user": None,
                "password": None, "url": url}
    path = urllib.parse.unquote(parts.path or "")
    if parts.query and scheme in ("http", "https"):
        path = path  # queries are kept in "url" only
    user = urllib.parse.unquote(parts.username) if parts.username else None
    password = urllib.parse.unquote(parts.password) if parts.password is not None else None
    return {"scheme": scheme, "host": host.lower(), "port": port or _default_port(scheme), "path": path,
            "user": user, "password": password, "url": url, "query": parts.query}


def _line_spans(text):
    spans, pos = [], 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return spans


def _parse_command_line(line):
    """Credentials / targets in a shell command line (wget, curl, lftp, sshpass, sftp, scp, ssh)."""
    found = {"user": None, "password": None, "port": None, "targets": [], "cut_dirs": None, "tools": set()}
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        tokens = line.split()
    if not any(os.path.basename(t) in COMMANDS for t in tokens):
        return None
    tool = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        base = os.path.basename(tok)
        if base in COMMANDS:
            tool = base
            found["tools"].add(base)
            i += 1
            continue
        if tool == "wget":
            m = re.match(r"^--(?:http-|ftp-)?(user|password|passwd)=(.*)$", tok)
            if m:
                found["user" if m.group(1) == "user" else "password"] = m.group(2)
            elif tok in ("--user", "--http-user", "--ftp-user") and nxt is not None:
                found["user"] = nxt
                i += 1
            elif tok in ("--password", "--http-password", "--ftp-password", "--http-passwd") and nxt is not None:
                found["password"] = nxt
                i += 1
            m = re.match(r"^--cut-dirs=(\d+)$", tok)
            if m:
                found["cut_dirs"] = int(m.group(1))
        elif tool == "curl":
            value = None
            if tok in ("-u", "--user") and nxt is not None:
                value = nxt
                i += 1
            elif tok.startswith("--user="):
                value = tok[7:]
            elif tok.startswith("-u") and len(tok) > 2:
                value = tok[2:]
            if value:
                user, _, password = value.partition(":")
                found["user"] = user or found["user"]
                if _:
                    found["password"] = password
        elif tool == "lftp":
            if tok == "-u" and nxt is not None:
                user, _, password = nxt.partition(",")
                found["user"] = user or found["user"]
                if _ and password:
                    found["password"] = password
                i += 1
            elif tok == "-p" and nxt and nxt.isdigit():
                found["port"] = int(nxt)
                i += 1
            elif tok in ("-c", "-e") and nxt:
                for part in re.split(r";", nxt):
                    sub = _parse_command_line("lftp " + part.strip())
                    if sub:
                        for key in ("user", "password", "port"):
                            found[key] = sub[key] or found[key]
                i += 1
        elif tool == "sshpass":
            if tok == "-p" and nxt is not None:
                found["password"] = nxt
                i += 1
            elif tok.startswith("-p") and len(tok) > 2:
                found["password"] = tok[2:]
        elif tool in ("sftp", "scp", "ssh"):
            if tok in ("-P", "-p") and nxt and nxt.isdigit() and not (tool == "scp" and tok == "-p"):
                found["port"] = int(nxt)
                i += 1
            elif tok == "-o" and nxt:
                m = re.match(r"(?i)^port\s*=?\s*(\d+)$", nxt)
                if m:
                    found["port"] = int(m.group(1))
                i += 1
            elif re.match(r"(?i)^-oport=(\d+)$", tok):
                found["port"] = int(re.match(r"(?i)^-oport=(\d+)$", tok).group(1))
            elif "://" not in tok and not tok.startswith("-"):
                m = re.match(r"^([^@\s/:]+)@([A-Za-z0-9.-]+)(?::(.*))?$", tok)
                if m and HOST_RE.match(m.group(2)):
                    found["targets"].append({"user": m.group(1), "host": m.group(2).lower(), "path": m.group(3) or ""})
        i += 1
    return found


def _scheme_guess(port, protocol, text_lower):
    if protocol:
        p = protocol.lower()
        for scheme in ("sftp", "ftps", "ftp", "https", "http"):
            if scheme in p:
                return scheme
    if port == 21:
        return "ftp"
    if port == 990:
        return "ftps"
    if port == 443:
        return "https"
    if port == 80:
        return "http"
    if "sftp" in text_lower or "ssh" in text_lower:
        return "sftp"
    if re.search(r"\bftp\b", text_lower) and not port:
        return "ftp"
    return "sftp"


class _Record:
    """One logical delivery: transports (alternatives) and its credentials."""

    def __init__(self):
        self.transports = []   # dicts: scheme host port paths user password origin
        self.pos = None        # where the delivery is first mentioned (sources keep the order of the paste)

    def host_set(self):
        return {t["host"] for t in self.transports}

    def add(self, t):
        for have in self.transports:
            same_port = (have["port"] == t["port"] or not t.get("explicit_port") or not have.get("explicit_port"))
            if have["scheme"] == t["scheme"] and have["host"] == t["host"] and same_port and \
                    (not have["user"] or not t["user"] or have["user"] == t["user"]):
                if t.get("explicit_port") and not have.get("explicit_port"):
                    have["port"], have["explicit_port"] = t["port"], True
                have["user"] = have["user"] or t["user"]
                have["password"] = have["password"] or t["password"]
                have["paths"].extend(t["paths"])
                return have
        self.transports.append(t)
        return t


def _common_folder(paths, scheme):
    """The delivery root of several mentioned paths: the path itself when single, else their common folder."""
    paths = [p for p in paths if p is not None]
    if not paths:
        return ""
    uniq = list(dict.fromkeys(paths))
    if len(uniq) == 1:
        return uniq[0]
    folders = []
    for p in uniq:
        if _looks_like_file(p):
            p = p.rsplit("/", 1)[0] + "/"
        folders.append(_components(p))
    common = folders[0]
    for comps in folders[1:]:
        n = 0
        while n < len(common) and n < len(comps) and common[n] == comps[n]:
            n += 1
        common = common[:n]
    absolute = any(p.startswith("/") for p in uniq)
    if not common:
        return "/" if absolute else ""
    return ("/" if absolute else "") + "/".join(common) + "/"


def parse_input(text):
    """Sources described by a paste (addresses, delivery e-mails, s3 URIs, accessions). See the module docstring."""
    warnings = []
    text = _normalise_text(text or "")
    text, wrapped = _unwrap_protected(text)
    text = _demangle(text)
    urls = _find_urls(text)
    masked = list(text)
    for u in urls:
        for k in range(u["start"], u["end"]):
            masked[k] = "\x01"
    masked = "".join(masked)
    lower = text.lower()

    # labelled fields -> records
    fields = []
    for m in LABEL_RE.finditer(masked):
        kind = next(k for k, _ in _LABELS if m.group(k))
        vstart, vend = m.start("value"), m.end("value")
        raw = text[vstart:vend]
        url_hit = next((u for u in urls if u["start"] <= vstart < u["end"] or vstart <= u["start"] < vend), None)
        if url_hit is not None:
            if kind != "host":
                continue
            fields.append((m.start(), kind, url_hit["url"], True))
            continue
        value = _clean_value(raw, kind)
        if not value or (m.group("nl") and LABEL_RE.match(value)):
            continue
        fields.append((m.start(), kind, value, False))
    records, cur = [], None
    for _pos, kind, value, is_url in fields:
        if kind == "port" and not value.isdigit():
            continue
        if kind == "host" and not is_url:
            hostpart = value.split("/")[0]
            hostname, _, port = hostpart.partition(":")
            if not HOST_RE.match(hostname) or (port and not port.isdigit()):
                continue
        if cur is None or (kind in cur and cur[kind] != value):
            cur = {"_pos": _pos}
            records.append(cur)
        cur.setdefault(kind, value)
        cur.setdefault("_url_" + kind, is_url)

    recs = []
    hostless = []
    for fr in records:
        if "host" not in fr:
            if fr.get("user") or fr.get("password"):
                hostless.append(fr)
            continue
        rec = _Record()
        rec.pos = fr.get("_pos", 0)
        port = int(fr["port"]) if fr.get("port", "").isdigit() else None
        if fr.get("_url_host"):
            u = _parse_url(fr["host"]) or {}
            scheme = u.get("scheme") or "sftp"
            host = u.get("host")
            path = u.get("path") or ""
            explicit = port is not None or (u.get("port") and u.get("port") != _default_port(scheme))
            port = port or u.get("port") or _default_port(scheme)
            user = fr.get("user") or u.get("user")
            password = fr.get("password") or u.get("password")
        else:
            hostpart = fr["host"].split("/", 1)
            hostname, _, hport = hostpart[0].partition(":")
            path = ("/" + hostpart[1]) if len(hostpart) > 1 else ""
            if hport.isdigit():
                port = port or int(hport)
            scheme = _scheme_guess(port, fr.get("protocol"), lower)
            host = hostname.lower()
            explicit = port is not None
            port = port or _default_port(scheme)
            user, password = fr.get("user"), fr.get("password")
        if not host:
            continue
        rec.add({"scheme": scheme, "host": host, "port": port, "explicit_port": bool(explicit),
                 "paths": [path], "user": user, "password": password, "origin": "fields"})
        recs.append(rec)

    # command lines: credentials for the URLs on the same line; ssh-style targets
    line_creds = []
    for start, end, line in _line_spans(text):
        found = _parse_command_line(line) if re.search(r"\b(" + "|".join(COMMANDS) + r")\b", line) else None
        if not found:
            continue
        line_creds.append((start, end, found))
        for tgt in found["targets"]:
            t = {"scheme": "sftp", "host": tgt["host"], "port": found["port"] or 22,
                 "explicit_port": bool(found["port"]), "paths": [tgt["path"]], "user": tgt["user"],
                 "password": found["password"], "origin": "command"}
            rec = next((r for r in recs if tgt["host"] in r.host_set()), None)
            if rec is None:
                rec = _Record()
                rec.pos = start
                recs.append(rec)
            rec.add(t)
        if found["cut_dirs"] is not None:
            warnings.append("The provider's wget command uses --cut-dirs=%d; here the delivered folder is kept by default (\"strip levels\" changes that)"
                            % found["cut_dirs"])

    sources = []
    url_sources = []
    s3_seen = set()
    ignored = []
    email_like = bool(recs) or bool(hostless)
    for u in urls:
        info = _parse_url(u["url"])
        if not info:
            continue
        if info["scheme"] != "s3" and not HOST_RE.match(info["host"]):
            warnings.append("Skipped an address with an invalid host name: %s" % info["host"][:80])
            continue
        if info["scheme"] == "s3":
            if not BUCKET_RE.match(info["host"]):
                warnings.append("Skipped an s3 URI with an invalid bucket name: %s" % info["host"][:80])
                continue
            key = (info["host"], info["path"])
            if key not in s3_seen and info["host"]:
                s3_seen.add(key)
                sources.append(dict(_s3_source(info["host"], info["path"], warnings), _pos=u["start"]))
            continue
        for start, end, found in line_creds:
            if start <= u["start"] < end:
                info["user"] = info["user"] or found["user"]
                info["password"] = info["password"] if info["password"] is not None else found["password"]
                if found["port"] and info["port"] == _default_port(info["scheme"]):
                    info["port"] = found["port"]
        t = {"scheme": info["scheme"], "host": info["host"], "port": info["port"],
             "explicit_port": info["port"] != _default_port(info["scheme"]), "paths": [info["path"]],
             "user": info["user"], "password": info["password"], "origin": "url", "query": info.get("query")}
        if info["scheme"] in ("http", "https") and info.get("query") and not _looks_like_file(info["path"]):
            ignored.append(info["host"])
            continue
        rec = next((r for r in recs if info["host"] in r.host_set()), None)
        if rec is None and info["user"]:
            rec = next((r for r in recs if info["user"] in {x["user"] for x in r.transports}), None)
        if rec is not None:
            if not t["user"]:
                same = next((x for x in rec.transports if x["host"] == info["host"] and x["user"]), None) or \
                    next((x for x in rec.transports if x["user"]), None)
                if same:
                    t["user"], t["password"] = same["user"], same["password"]
            rec.add(t)
            continue
        if info["user"] or info["password"]:
            rec = _Record()
            rec.pos = u["start"]
            rec.add(t)
            recs.append(rec)
            continue
        is_data = _looks_like_file(info["path"]) or info["scheme"] in ("ftp", "ftps", "sftp")
        if email_like and not is_data:
            ignored.append(info["host"])
            continue
        t["_pos"] = u["start"]
        url_sources.append(t)
    for inner in wrapped:
        info = _parse_url(inner)
        rec = next((r for r in recs if info and info["host"] in r.host_set()), None) if info else None
        if rec is not None and info["scheme"] != "s3":
            rec.add({"scheme": info["scheme"], "host": info["host"], "port": info["port"], "explicit_port": False,
                     "paths": [info["path"]], "user": info["user"], "password": info["password"],
                     "origin": "wrapped"})
        elif info:
            ignored.append(info["host"] + " (urldefense link)")

    # credentials without a host: give them to the only plain URL, if there is one
    for fr in hostless:
        if len(url_sources) == 1 and not url_sources[0]["user"]:
            url_sources[0]["user"] = fr.get("user")
            url_sources[0]["password"] = fr.get("password")
            rec = _Record()
            rec.pos = url_sources[0]["_pos"]
            rec.add(url_sources.pop(0))
            recs.append(rec)
        elif not recs:
            warnings.append("A user name / password was found but no server address to use it with")
        else:
            rec = recs[-1]
            for t in rec.transports:
                if not t["user"] and not t["password"]:
                    t["user"], t["password"] = fr.get("user"), fr.get("password")
    if ignored:
        warnings.append("Ignored links that are not data locations: " + ", ".join(sorted(set(ignored))))

    for rec in recs:
        src = _remote_source(rec)
        if src:
            src["_pos"] = rec.pos or 0
            sources.append(src)
    for t in url_sources:
        rec = _single(t)
        sources.append(dict(_remote_source(rec), _pos=t["_pos"]))

    # accessions (outside URLs)
    seen = set()
    for m in ACCESSION_RE.finditer(masked):
        acc = m.group(1)
        if acc in seen:
            continue
        seen.add(acc)
        kind = "geo" if acc.startswith("GS") else "sra"
        sources.append({"_pos": m.start(), "id": _sid(kind, acc), "kind": kind,
                        "label": ("GEO " if kind == "geo" else "SRA / ENA ") + acc, "accession": acc,
                        "transports": [], "secret_refs": [], "secrets": {}})
    if not sources:
        warnings.append("Nothing to download was recognised: paste an ftp / sftp / https address, s3:// URI, a "
                        "delivery e-mail or a GEO / SRA accession")
    sources.sort(key=lambda s: s.pop("_pos", 0))
    # one id per source
    ids = set()
    for s in sources:
        base = s["id"]
        n = 2
        while s["id"] in ids:
            s["id"] = "%s%d" % (base[:8], n)
            n += 1
        ids.add(s["id"])
    return {"sources": sources, "warnings": warnings}


def _single(t):
    rec = _Record()
    rec.add(t)
    return rec


def _remote_source(rec):
    if not rec.transports:
        return None
    secrets = {}
    out = []
    for t in rec.transports:
        ref = None
        if t.get("password"):
            ref = next((k for k, v in secrets.items() if v == t["password"]), None)
            if ref is None:
                ref = "PASSWORD" if not secrets else "PASSWORD_%d" % (len(secrets) + 1)
                secrets[ref] = t["password"]
        path = _common_folder(t["paths"], t["scheme"])
        if path == "/" and t["scheme"] in ("sftp",):
            path = ""
        item = {"scheme": t["scheme"], "host": t["host"], "port": t["port"], "path": path,
                "user": t.get("user") or None, "has_password": bool(t.get("password")), "secret_ref": ref}
        if t.get("query") and _looks_like_file(path):
            item["query"] = t["query"]
        out.append(item)
    first = out[0]
    kind = "remote" if any(t["user"] or t["has_password"] for t in out) else "url"
    who = (first["user"] + " @ ") if first["user"] else ""
    via = ", ".join("%s:%s" % (t["scheme"], t["port"]) if t["port"] != _default_port(t["scheme"]) else t["scheme"]
                    for t in out)
    if kind == "url":
        label = "%s://%s%s" % (first["scheme"], first["host"] + (":%d" % first["port"] if first["port"] !=
                                                                    _default_port(first["scheme"]) else ""),
                               first["path"] or "/")
    else:
        label = "%s%s (%s)" % (who, first["host"], via)
    src = {"id": _sid(kind, first["scheme"], first["host"], first["port"], first["path"], first["user"]),
           "kind": kind, "label": label, "transports": out, "secret_refs": sorted(secrets), "secrets": secrets,
           "is_file": _looks_like_file(first["path"])}
    return src


def _s3_source(bucket, key, warnings):
    conf = bucket_settings(bucket)
    profile, region = conf["profile"], conf["region"]
    profiles = aws_profiles()
    if profile not in profiles:
        warnings.append("AWS profile '%s' (for bucket %s) is not in ~/.aws/credentials; add it before downloading"
                        % (profile, bucket))
    if not conf["known"]:
        warnings.append("Bucket %s is not in the bucket table: using profile '%s' and the bucket's own region"
                        % (bucket, profile))
    key = key or ""
    return {"id": _sid("s3", bucket, key), "kind": "s3",
            "label": "s3://%s/%s (profile %s, %s)" % (bucket, key, profile, region or "region looked up"),
            "bucket": bucket, "key": key, "profile": profile, "region": region,
            "transports": [{"scheme": "s3", "host": bucket, "port": 443, "path": key, "user": profile,
                            "has_password": False, "secret_ref": None}],
            "secret_refs": [], "secrets": {}, "is_file": _looks_like_file(key),
            "profile_missing": profile not in profiles}


# --------------------------------------------------------------------------------------------- HTTP helpers

_LAST_NCBI = [0.0]


def _http_get(url, timeout=30, data=None, headers=None, retries=2):
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    if "ncbi.nlm.nih.gov" in url:  # at most ~3 requests a second without an API key
        wait = 0.4 - (time.time() - _LAST_NCBI[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_NCBI[0] = time.time()
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as error:
            last = error
            if error.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, socket.timeout, ConnectionError) as error:
            last = error
        time.sleep(1.5 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------------------------- geo_lookup

def _soft_records(text):
    """GEO SOFT text -> list of (entity, accession, {key: [values]})."""
    records, cur = [], None
    for line in text.splitlines():
        if line.startswith("^"):
            entity, _, acc = line[1:].partition(" = ")
            cur = (entity.strip().upper(), acc.strip(), {})
            records.append(cur)
        elif line.startswith("!") and cur is not None:
            key, _, value = line[1:].partition(" = ")
            cur[2].setdefault(key.strip(), []).append(value.strip())
    return records


def _geo_soft(acc, targ, timeout):
    url = "%s?%s" % (GEO_ACC, urllib.parse.urlencode({"acc": acc, "targ": targ, "form": "text", "view": "brief"}))
    text = _http_get(url, timeout=timeout).decode("utf-8", "replace")
    return _soft_records(text)


def _https_ftp(url):
    if url.startswith("ftp://ftp.ncbi.nlm.nih.gov/"):
        return "https://" + url[6:]
    return url


def _ena_runs_tsv(text):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    head = lines[0].split("\t")
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]]


def _ena_filereport(accession, timeout):
    q = urllib.parse.urlencode({"accession": accession, "result": "read_run", "fields": ENA_FIELDS,
                                "format": "tsv", "download": "false", "limit": 0})
    try:
        return _ena_runs_tsv(_http_get(ENA_FILEREPORT + "?" + q, timeout=timeout).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        if error.code in (204, 400, 404):
            return []
        raise


def _ena_search_experiments(experiments, timeout):
    rows = []
    experiments = sorted(set(experiments))
    for i in range(0, len(experiments), 100):
        chunk = experiments[i:i + 100]
        query = " OR ".join('experiment_accession="%s"' % e for e in chunk)
        data = {"result": "read_run", "query": query, "fields": ENA_FIELDS, "format": "tsv", "limit": 0}
        try:
            rows += _ena_runs_tsv(_http_get(ENA_SEARCH, timeout=timeout, data=data,
                                            headers={"Content-Type": "application/x-www-form-urlencoded"})
                                  .decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            if error.code not in (204, 400, 404):
                raise
    return rows


def _ncbi_runinfo(term, timeout):
    """Runs from NCBI SRA (E-utilities runinfo) for a term such as 'SRP123' or 'SRX1 OR SRX2'."""
    q = urllib.parse.urlencode({"db": "sra", "term": term, "usehistory": "y", "retmode": "json", "retmax": 0})
    info = json.loads(_http_get(EUTILS + "esearch.fcgi?" + q, timeout=timeout).decode())
    res = info.get("esearchresult") or {}
    if not int(res.get("count") or 0):
        return []
    q = urllib.parse.urlencode({"db": "sra", "query_key": res.get("querykey"), "WebEnv": res.get("webenv"),
                                "rettype": "runinfo", "retmode": "text"})
    text = _http_get(EUTILS + "efetch.fcgi?" + q, timeout=timeout).decode("utf-8", "replace")
    import csv
    import io
    return [r for r in csv.DictReader(io.StringIO(text)) if r.get("Run") and r.get("Run") != "Run"]


def _ints(text):
    out = []
    for part in (text or "").split(";"):
        try:
            out.append(int(part))
        except ValueError:
            out.append(None)
    return out


def _ena_run(row):
    urls = [u for u in (row.get("fastq_ftp") or "").split(";") if u]
    sizes = _ints(row.get("fastq_bytes"))
    md5s = (row.get("fastq_md5") or "").split(";")
    fastq = []
    for i, u in enumerate(urls):
        path = u.split("/", 1)[1] if "/" in u else u
        host = u.split("/", 1)[0]
        fastq.append({"url": "https://%s/%s" % (host, path), "ftp_url": "ftp://%s/%s" % (host, path),
                      "name": u.rsplit("/", 1)[-1], "bytes": sizes[i] if i < len(sizes) else None,
                      "md5": md5s[i] if i < len(md5s) and re.match(r"^[0-9a-f]{32}$", md5s[i]) else None})
    def num(key):
        try:
            return int(row.get(key) or "")
        except ValueError:
            return None
    total = sum(f["bytes"] for f in fastq if f["bytes"]) if fastq else None
    return {"run": row.get("run_accession"), "experiment": row.get("experiment_accession"),
            "layout": (row.get("library_layout") or "").upper() or None, "strategy": row.get("library_strategy"),
            "bytes": total, "reads": num("read_count"), "bases": num("base_count"), "fastq": fastq, "source": "ENA"}


def _ncbi_run(row):
    def num(key):
        try:
            return int(float(row.get(key) or ""))
        except ValueError:
            return None
    size_mb = num("size_MB")
    return {"run": row.get("Run"), "experiment": row.get("Experiment"),
            "layout": (row.get("LibraryLayout") or "").upper() or None, "strategy": row.get("LibraryStrategy"),
            "bytes": size_mb * 1000000 if size_mb is not None else None, "reads": num("spots"),
            "bases": num("bases"), "fastq": [], "source": "SRA",
            "note": "Not in ENA yet: converted from SRA with prefetch + fasterq-dump"}


def _gsm_of(row):
    for key in ("experiment_alias", "sample_alias", "SampleName", "LibraryName"):
        m = re.match(r"^(GSM\d+)", row.get(key) or "")
        if m:
            return m.group(1)
    return None


def _http_index(url, timeout, auth=None):
    """Parse an Apache / nginx index page -> [(absolute URL, is_dir, approx bytes)]."""
    body = _open_url(url, timeout, auth)[0].decode("utf-8", "replace")
    return _parse_index(body, url)


def _approx_bytes(token):
    m = re.match(r"^(\d+(?:\.\d+)?)([KMGTP]?)i?B?$", token, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def _parse_index(body, page_url):
    out = []
    base = urllib.parse.urlsplit(page_url)
    anchors = list(re.finditer(r"<a\s[^>]*href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", body, re.I | re.S))
    for i, m in enumerate(anchors):
        href = html.unescape(m.group(1))
        if href.startswith("?") or href.startswith("mailto:") or href in ("../", "./", "/"):
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.netloc != base.netloc or parts.query or not parts.path.startswith(base.path) or \
                len(parts.path) <= len(base.path):
            continue
        tail_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(body)
        tail = re.sub(r"<[^>]+>", " ", body[m.end():tail_end])
        tail = html.unescape(tail).split("\n")[0] if "<tr" not in body[m.end():tail_end].lower() else html.unescape(tail)
        size = None
        for token in reversed(tail.split()):
            size = _approx_bytes(token)
            if size is not None:
                break
            if token == "-":
                break
        out.append((absolute, parts.path.endswith("/"), size))
    return out


def _geo_suppl_listing(folder_url, timeout):
    """{file name: approx bytes} for a GEO suppl folder (one request); {} on any problem."""
    try:
        return {urllib.parse.unquote(u.rsplit("/", 1)[-1]): size
                for u, is_dir, size in _http_index(folder_url, timeout) if not is_dir}
    except Exception:  # noqa: BLE001 - sizes are optional
        return {}


def geo_lookup(accession, timeout=30):
    """Metadata, runs (FASTQ URLs, sizes, md5) and supplementary files of a GEO / SRA accession."""
    acc = (accession or "").strip().upper()
    m = ACCESSION_RE.match(acc)
    if not m or m.group(1) != acc:
        raise ValueError("Not a GEO (GSE / GSM) or SRA / ENA accession: %r" % accession)
    out = {"accession": acc, "title": "", "summary": "", "samples": [], "series_supplementary": [], "warnings": []}
    samples = {}
    order = []

    def sample_entry(gsm, title=""):
        if gsm not in samples:
            samples[gsm] = {"gsm": gsm, "title": title, "characteristics": {}, "organism": None, "source": None,
                            "experiments": [], "runs": [], "supplementary": []}
            order.append(gsm)
        return samples[gsm]

    def add_soft_sample(fields):
        gsm = (fields.get("Sample_geo_accession") or [""])[0]
        if not gsm:
            return None
        s = sample_entry(gsm, (fields.get("Sample_title") or [""])[0])
        for line in fields.get("Sample_characteristics_ch1", []) + fields.get("Sample_characteristics_ch2", []):
            key, sep, value = line.partition(":")
            key, value = (key.strip(), value.strip()) if sep else ("characteristics", line.strip())
            s["characteristics"][key] = (s["characteristics"][key] + "; " + value) if key in s["characteristics"] \
                else value
        s["organism"] = (fields.get("Sample_organism_ch1") or [None])[0]
        s["source"] = (fields.get("Sample_source_name_ch1") or [None])[0]
        s["library_strategy"] = (fields.get("Sample_library_strategy") or [None])[0]
        for rel in fields.get("Sample_relation", []):
            mm = re.search(r"\b([SED]RX\d+)", rel)
            if rel.startswith("SRA") and mm:
                s["experiments"].append(mm.group(1))
        for key, values in fields.items():
            if key.startswith("Sample_supplementary_file"):
                for v in values:
                    if v and v.upper() != "NONE":
                        s["supplementary"].append({"name": urllib.parse.unquote(v.rsplit("/", 1)[-1]),
                                                   "url": _https_ftp(v), "ftp_url": v, "bytes": None})
        return s

    ena_rows = []
    if acc.startswith("GSE"):
        recs = _geo_soft(acc, "self", timeout)
        series = next((r for r in recs if r[0] == "SERIES"), None)
        if series is None:
            raise DownloadError("GEO has no series %s" % acc)
        f = series[2]
        out["title"] = (f.get("Series_title") or [""])[0]
        out["summary"] = "\n".join(f.get("Series_summary", []))
        out["type"] = "; ".join(f.get("Series_type", []))
        sra_ids, bioprojects, subseries = [], [], []
        for rel in f.get("Series_relation", []):
            mm = re.search(r"\b([SED]RP\d+)", rel)
            if rel.startswith("SRA") and mm:
                sra_ids.append(mm.group(1))
            mm = re.search(r"\b(PRJ[A-Z]{2}\d+)", rel)
            if rel.startswith("BioProject") and mm:
                bioprojects.append(mm.group(1))
            mm = re.search(r"\b(GSE\d+)", rel)
            if rel.startswith("SuperSeries of") and mm:
                subseries.append(mm.group(1))
        if subseries:
            out["subseries"] = subseries
        folder = None
        for v in f.get("Series_supplementary_file", []):
            if v and v.upper() != "NONE":
                folder = folder or _https_ftp(v).rsplit("/", 1)[0] + "/"
                out["series_supplementary"].append({"name": urllib.parse.unquote(v.rsplit("/", 1)[-1]),
                                                    "url": _https_ftp(v), "ftp_url": v, "bytes": None})
        if folder:
            sizes = _geo_suppl_listing(folder, timeout)
            for item in out["series_supplementary"]:
                item["bytes"] = sizes.get(item["name"])
                item["bytes_approx"] = item["bytes"] is not None
        for _e, _a, fields in _geo_soft(acc, "gsm", timeout):
            if _e == "SAMPLE":
                add_soft_sample(fields)
        for sid in sra_ids or bioprojects:
            ena_rows += _ena_filereport(sid, timeout)
        if not ena_rows:
            exps = [e for s in samples.values() for e in s["experiments"]]
            if exps:
                ena_rows = _ena_search_experiments(exps, timeout)
    elif acc.startswith("GSM"):
        recs = _geo_soft(acc, "self", timeout)
        rec = next((r for r in recs if r[0] == "SAMPLE"), None)
        if rec is None:
            raise DownloadError("GEO has no sample %s" % acc)
        s = add_soft_sample(rec[2])
        out["title"] = s["title"] if s else ""
        out["summary"] = "; ".join(rec[2].get("Sample_description", []))
        out["series"] = rec[2].get("Sample_series_id", [])
        if s and s["experiments"]:
            ena_rows = _ena_search_experiments(s["experiments"], timeout)
    else:
        ena_rows = _ena_filereport(acc, timeout)

    # runs -> samples
    by_exp = {e: gsm for gsm, s in samples.items() for e in s["experiments"]}
    seen_runs = set()
    for row in ena_rows:
        run = _ena_run(row)
        if not run["run"] or run["run"] in seen_runs:
            continue
        seen_runs.add(run["run"])
        gsm = by_exp.get(run["experiment"]) or _gsm_of(row)
        if acc.startswith("GS") and gsm not in samples and samples:
            continue  # a run of another series sharing the project
        key = gsm or row.get("secondary_sample_accession") or row.get("sample_accession") or run["run"]
        s = sample_entry(key, row.get("sample_title") or row.get("sample_alias") or row.get("experiment_title") or "")
        if gsm is None:
            s["gsm"] = key
        s["organism"] = s["organism"] or row.get("scientific_name")
        s["runs"].append(run)
        if not out["title"]:
            out["title"] = row.get("study_title") or ""
    # experiments ENA does not have (yet): NCBI runinfo
    missing = sorted({e for s in samples.values() for e in s["experiments"] if not s["runs"]})
    if acc[:3] in ("SRP", "ERP", "DRP", "SRR", "ERR", "DRR", "SRX", "ERX", "DRX", "PRJ", "SRS", "ERS", "DRS") \
            and not ena_rows:
        missing = [acc]
    if missing:
        try:
            ncbi = []
            for i in range(0, len(missing), 100):
                ncbi += _ncbi_runinfo(" OR ".join(x + "[Accession]" if not x.startswith("PRJ") else x
                                                  for x in missing[i:i + 100]), timeout)
            for row in ncbi:
                run = _ncbi_run(row)
                if run["run"] in seen_runs:
                    continue
                seen_runs.add(run["run"])
                gsm = by_exp.get(run["experiment"]) or _gsm_of(row)
                key = gsm or row.get("BioSample") or row.get("Sample") or run["run"]
                s = sample_entry(key, row.get("SampleName") or "")
                s["runs"].append(run)
            if ncbi:
                out["warnings"].append("%d run(s) are not in ENA yet; they will be converted from SRA with "
                                       "prefetch + fasterq-dump" % len(ncbi))
        except Exception as error:  # noqa: BLE001
            out["warnings"].append("NCBI SRA lookup failed: %s" % error)
    if acc.startswith("GS") and samples and not any(s["runs"] for s in samples.values()):
        if not any(s["experiments"] for s in samples.values()):
            out["warnings"].append("No raw reads are linked to %s in SRA (processed files only)" % acc)
        else:
            out["warnings"].append("SRA runs of %s were not found in ENA or NCBI" % acc)
    out["samples"] = [samples[g] for g in order]
    return out


# --------------------------------------------------------------------------------------------- list_remote

def _open_url(url, timeout, auth=None, method="GET"):
    """urllib GET/HEAD with optional basic auth (sent only to the original host). Returns (body, headers, url)."""
    host = urllib.parse.urlsplit(url).hostname

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None and urllib.parse.urlsplit(newurl).hostname != host:
                new.headers.pop("Authorization", None)
                new.unredirected_hdrs.pop("Authorization", None)
            return new

    opener = urllib.request.build_opener(Redirect)
    headers = {"User-Agent": USER_AGENT}
    if auth and auth[0]:
        token = base64.b64encode(("%s:%s" % (auth[0], auth[1] or "")).encode()).decode()
        headers["Authorization"] = "Basic " + token
    req = urllib.request.Request(url, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as resp:
        return (resp.read() if method == "GET" else b""), resp.headers, resp.geturl()


def _run(cmd, env=None, timeout=40, stdin=None):
    try:
        proc = subprocess.run(cmd, input=stdin, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DownloadError("%s timed out after %ss" % (cmd[0], timeout))
    except OSError as error:
        raise DownloadError("%s could not run: %s" % (cmd[0], error))
    return proc


def _lftp_open_line(t, has_password):
    url = "%s://%s" % (t["scheme"], t["host"])
    parts = ["open"]
    if has_password:
        parts.append("--env-password")
    if t.get("user"):
        parts += ["-u", _lftp_quote(t["user"] if has_password else t["user"] + ",")]
    if t.get("port") and t["port"] != _default_port(t["scheme"]):
        parts += ["-p", str(int(t["port"]))]
    parts.append(_lftp_quote(url))
    return " ".join(parts)


def _lftp_settings(listing=False):
    if listing:
        return ["set cmd:fail-exit yes", "set net:max-retries 2", "set net:timeout 15",
                "set net:reconnect-interval-base 5", "set net:reconnect-interval-max 10",
                "set sftp:auto-confirm yes", "set ssl:verify-certificate yes"]
    return ["set cmd:fail-exit yes", "set net:max-retries 8", "set net:timeout 30",
            "set net:reconnect-interval-base 15", "set net:reconnect-interval-multiplier 2",
            "set net:reconnect-interval-max 300", "set sftp:auto-confirm yes", "set xfer:clobber on",
            "set mirror:use-pget-n 8", "set pget:default-n 8"]


LS_LINE = re.compile(r"^([dl-])[rwxsStT-]{9}\S*\s+\S+\s+(\d+)\s+\S+\s+\S+\s+(.*)$")


def _list_lftp(t, password, timeout, max_entries):
    if not shutil.which("lftp"):
        raise DownloadError("lftp is not installed")
    lines = _lftp_settings(listing=True) + [_lftp_open_line(t, bool(password))]
    target = t.get("path") or "."
    lines.append("find -l " + _lftp_quote(target))
    prefix = target.rstrip("/") + "/"
    env = dict(os.environ)
    if password:
        env["LFTP_PASSWORD"] = password
    proc = _run(["lftp"], env=env, timeout=timeout, stdin="\n".join(lines) + "\n")
    entries = []
    for line in proc.stdout.splitlines():
        m = LS_LINE.match(line)
        if not m:
            continue
        name = m.group(3)
        if m.group(1) == "l" and " -> " in name:
            name = name.split(" -> ")[0]
        is_dir = m.group(1) == "d"
        if name.rstrip("/") == target.rstrip("/") or name == prefix:
            if not is_dir:
                return [{"path": "", "bytes": int(m.group(2)), "dir": False}], False
            continue
        if not name.startswith(prefix):
            continue
        name = name[len(prefix):].rstrip("/")
        if not name or name == ".":
            continue
        entries.append({"path": name, "bytes": None if is_dir else int(m.group(2)), "dir": is_dir})
        if len(entries) >= max_entries:
            return entries, True
    if proc.returncode != 0 and not entries:
        raise DownloadError("lftp: " + (proc.stderr.strip().splitlines() or ["exit %d" % proc.returncode])[-1])
    return entries, False


def _ssh_opts(port, has_password):
    return ["-oPort=%d" % int(port or 22), "-oStrictHostKeyChecking=accept-new", "-oConnectTimeout=15",
            "-oNumberOfPasswordPrompts=1", "-oBatchMode=%s" % ("no" if has_password else "yes")]


SFTP_LS = re.compile(r"^([dl-])[rwxsStT-]{9}\S*\s+\S+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d+\s+[\d:]+\s+(.*)$")


def _list_sftp(t, password, timeout, max_entries):
    if not shutil.which("sftp"):
        raise DownloadError("OpenSSH sftp is not installed")
    if password and not shutil.which("sshpass"):
        raise DownloadError("sshpass is not installed (needed for a password with OpenSSH sftp)")
    base = (t.get("path") or "").rstrip("/") or "."
    deadline = time.monotonic() + timeout
    level = [""]
    entries = []
    env = dict(os.environ)
    cmd = ["sftp"] + _ssh_opts(t["port"], bool(password)) + ["-b", "-", "%s@%s" % (t["user"], t["host"])
                                                              if t.get("user") else t["host"]]
    if password:
        env["SSHPASS"] = password
        cmd = ["sshpass", "-e"] + cmd
    for _depth in range(12):
        if not level:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return entries, True
        batch = "".join("-ls -la %s\n" % _esc_glob(base + ("/" + d if d else "")) for d in level)
        proc = _run(cmd, env=env, timeout=remaining, stdin=batch)
        if proc.returncode != 0 and not entries and "sftp>" not in proc.stdout:
            raise DownloadError("sftp: " + _redact((proc.stderr.strip().splitlines() or
                                                    ["exit %d" % proc.returncode])[-1], {"p": password}))
        nxt = []
        current = None
        for line in proc.stdout.splitlines():
            if line.startswith("sftp> "):
                current = None
                arg = line[len("sftp> "):].split(None, 2)[-1] if len(line.split()) >= 3 else ""
                for d in level:
                    if _esc_glob(base + ("/" + d if d else "")) == arg:
                        current = d
                        break
                continue
            m = SFTP_LS.match(line)
            if not m or current is None:
                continue
            name = m.group(3)
            if m.group(1) == "l" and " -> " in name:
                name = name.split(" -> ")[0]
            name = name.rsplit("/", 1)[-1]
            if name in (".", ".."):
                continue
            rel = (current + "/" + name) if current else name
            is_dir = m.group(1) == "d"
            entries.append({"path": rel, "bytes": None if is_dir else int(m.group(2)), "dir": is_dir})
            if is_dir:
                nxt.append(rel)
            if len(entries) >= max_entries:
                return entries, True
        level = nxt
    return entries, bool(level)


def _list_ftplib(t, password, timeout, max_entries):
    cls = ftplib.FTP_TLS if t["scheme"] == "ftps" else ftplib.FTP
    deadline = time.monotonic() + timeout
    try:
        ftp = cls()
        ftp.connect(t["host"], int(t["port"] or 21), timeout=min(timeout, 30))
        ftp.login(t.get("user") or "anonymous", password or ("anonymous@" if not t.get("user") else ""))
        if t["scheme"] == "ftps":
            ftp.prot_p()
    except (OSError, ftplib.Error, EOFError) as error:
        raise DownloadError("ftp: %s" % _redact(error, {"p": password}))
    base = (t.get("path") or "").rstrip("/") or "/"
    entries, queue = [], [""]
    try:
        while queue:
            if time.monotonic() > deadline:
                return entries, True
            rel = queue.pop(0)
            path = base + ("/" + rel if rel else "") if base != "/" else "/" + rel
            items = []
            try:
                for name, facts in ftp.mlsd(path, facts=["type", "size"]):
                    if name in (".", "..") or facts.get("type") in ("cdir", "pdir"):
                        continue
                    items.append((name, facts.get("type") == "dir", facts.get("size")))
            except ftplib.error_perm:
                lines = []
                ftp.retrlines("LIST " + path, lines.append)
                for line in lines:
                    m = re.match(r"^([dl-])[rwxsStT-]{9}\S*\s+\S+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d+\s+[\d:]+\s+(.*)$",
                                 line)
                    if m:
                        items.append((m.group(3).split(" -> ")[0], m.group(1) == "d", m.group(2)))
            for name, is_dir, size in items:
                r = (rel + "/" + name) if rel else name
                entries.append({"path": r, "bytes": None if is_dir else (int(size) if size else None), "dir": is_dir})
                if is_dir:
                    queue.append(r)
                if len(entries) >= max_entries:
                    return entries, True
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            pass
    return entries, False


def _list_http(t, password, timeout, max_entries):
    port = "" if t["port"] == _default_port(t["scheme"]) else ":%d" % t["port"]
    root = "%s://%s%s%s" % (t["scheme"], t["host"], port, urllib.parse.quote(t.get("path") or "/", safe="/~"))
    auth = (t.get("user"), password) if t.get("user") else None
    deadline = time.monotonic() + timeout
    try:
        if not root.endswith("/"):
            body, headers, final = _open_url(root, min(timeout, 30), auth, method="HEAD")
            ctype = headers.get("Content-Type", "")
            if "html" not in ctype:
                size = headers.get("Content-Length")
                return [{"path": "", "bytes": int(size) if size and size.isdigit() else None, "dir": False}], False
            root += "/"
        entries, queue, pages = [], [root], 0
        while queue:
            if time.monotonic() > deadline or pages > 2000:
                return entries, True
            page = queue.pop(0)
            pages += 1
            for url, is_dir, size in _http_index(page, min(30, max(1, deadline - time.monotonic())), auth):
                rel = urllib.parse.unquote(urllib.parse.urlsplit(url).path[len(urllib.parse.urlsplit(root).path):])
                rel = rel.rstrip("/")
                if not rel or rel.split("/")[-1].startswith("index.html"):
                    continue
                entries.append({"path": rel, "bytes": None if is_dir else size, "dir": is_dir})
                if size is not None and not is_dir:
                    entries[-1]["bytes_approx"] = True
                if is_dir:
                    queue.append(url)
                if len(entries) >= max_entries:
                    return entries, True
        return entries, False
    except urllib.error.HTTPError as error:
        raise DownloadError("%s: HTTP %d %s" % (t["scheme"], error.code, error.reason))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DownloadError("%s: %s" % (t["scheme"], _redact(error, {"p": password})))


def _list_s3(source, timeout, max_entries):
    bucket, key = source["bucket"], (source.get("key") or "").strip("/")
    profile = source.get("profile") or "default"
    region = source.get("region") or resolve_bucket_region(bucket, profile)
    if region and not source.get("region"):
        source["region"] = region
    env = dict(os.environ, AWS_PROFILE=profile)
    if region:
        env["AWS_REGION"] = region
    errors = []
    if shutil.which("rclone"):
        base = ["rclone", "lsjson", "--no-modtime", "--no-mimetype", ":s3,env_auth:%s/%s" % (bucket, key),
                "--s3-provider", "AWS", "--s3-no-check-bucket"] + (["--s3-region", region] if region else [])
        try:
            rows = None
            name = key.rsplit("/", 1)[-1]
            if key and not (source.get("key") or "").endswith("/"):
                # a key that is one object: rclone would otherwise list the whole parent folder recursively
                proc = _run(base, env=env, timeout=timeout)
                if proc.returncode == 0:
                    one = json.loads(proc.stdout or "[]")
                    if len(one) == 1 and not one[0].get("IsDir") and one[0].get("Path") == name:
                        rows = one
            if rows is None:
                proc = _run(base[:2] + ["-R", "--fast-list"] + base[2:], env=env, timeout=timeout)
                if proc.returncode == 0:
                    rows = json.loads(proc.stdout or "[]")
            if rows is not None:
                entries = [{"path": r["Path"], "bytes": None if r.get("IsDir") else r.get("Size"),
                            "dir": bool(r.get("IsDir"))} for r in rows]
                source["is_file"] = len(entries) == 1 and not entries[0]["dir"] and entries[0]["path"] == name
                if source["is_file"]:
                    entries[0]["path"] = ""
                entries.sort(key=lambda e: e["path"])
                return {"entries": entries[:max_entries], "truncated": len(entries) > max_entries,
                        "transport_used": "s3 (rclone, profile %s, %s)" % (profile, region), "transport_index": 0,
                        "errors": errors}
            errors.append("rclone: " + ((proc.stderr or "").strip().splitlines() or ["exit %d" % proc.returncode])[-1])
        except (DownloadError, ValueError) as error:
            errors.append("rclone: %s" % error)
    if shutil.which("aws"):
        cmd = ["aws", "s3", "ls", "--recursive", "s3://%s/%s" % (bucket, key + "/" if key else "")]
        if region:
            cmd += ["--region", region]
        try:
            proc = _run(cmd, env=env, timeout=timeout)
        except DownloadError as error:
            proc = None
            errors.append("aws: %s" % error)
        if proc is not None and proc.returncode == 0:
            entries, dirs = [], set()
            for line in proc.stdout.splitlines():
                m = re.match(r"^\S+\s+\S+\s+(\d+)\s+(.*)$", line)
                if not m:
                    continue
                rel = m.group(2)[len(key) + 1:] if key else m.group(2)
                parts = rel.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if d not in dirs:
                        dirs.add(d)
                        entries.append({"path": d, "bytes": None, "dir": True})
                entries.append({"path": rel, "bytes": int(m.group(1)), "dir": False})
            return {"entries": entries[:max_entries], "truncated": len(entries) > max_entries,
                    "transport_used": "s3 (aws cli, profile %s)" % profile, "transport_index": 0, "errors": errors}
        if proc is not None:
            errors.append("aws: " + ((proc.stderr or "").strip().splitlines() or ["exit %d" % proc.returncode])[-1])
    if not errors:
        errors.append("Neither rclone nor the aws cli is installed")
    return {"entries": [], "truncated": False, "transport_used": None, "transport_index": None, "errors": errors}


def _transport_text(t):
    port = "" if t.get("port") in (None, _default_port(t["scheme"])) else ":%s" % t["port"]
    user = (t["user"] + "@") if t.get("user") and t["scheme"] != "s3" else ""
    return "%s://%s%s%s%s" % (t["scheme"], user, t["host"], port, t.get("path") or "")


def _secret_for(t, secrets):
    ref = t.get("secret_ref") or ("PASSWORD" if t.get("has_password") else None)
    return (secrets or {}).get(ref) if ref else None


def list_remote(source, secrets=None, timeout=40, max_entries=5000):
    """Recursive listing of a source for the preview: entries relative to the source's folder."""
    secrets = dict(source.get("secrets") or {}, **(secrets or {}))
    kind = source.get("kind")
    if kind == "s3":
        return _list_s3(source, timeout, max_entries)
    if kind in ("geo", "sra"):
        try:
            info = geo_lookup(source["accession"], timeout=timeout)
        except Exception as error:  # noqa: BLE001
            return {"entries": [], "transport_used": None, "errors": [str(error)]}
        preview = destination_preview(source, "/", None, {"lookup": info})
        entries = [{"path": f["dest"][len(preview["root"]) + 1:], "bytes": f.get("bytes"), "dir": False,
                    "item": f.get("item")} for f in preview["files"]]
        return {"entries": entries, "transport_used": "ENA / GEO", "transport_index": None, "errors": [],
                "lookup": info}
    errors = []
    listers = {"sftp": [("lftp", _list_lftp), ("sftp", _list_sftp)],
               "ftp": [("lftp", _list_lftp), ("ftplib", _list_ftplib)],
               "ftps": [("lftp", _list_lftp), ("ftplib", _list_ftplib)],
               "http": [("urllib", _list_http), ("lftp", _list_lftp)],
               "https": [("urllib", _list_http), ("lftp", _list_lftp)]}
    for index, t in enumerate(source.get("transports") or []):
        label = _transport_text(t)
        if not _tcp_ok(t["host"], t["port"] or _default_port(t["scheme"])):
            errors.append("%s: cannot connect to %s port %s (blocked or down)" % (label, t["host"], t["port"]))
            continue
        password = _secret_for(t, secrets)
        if t.get("has_password") and not password:
            errors.append("%s: the password is not known (paste the e-mail again)" % label)
            continue
        for tool, fn in listers.get(t["scheme"], []):
            try:
                entries, truncated = fn(t, password, timeout, max_entries)
            except DownloadError as error:
                errors.append("%s via %s: %s" % (label, tool, _redact(error, secrets)))
                continue
            except Exception as error:  # noqa: BLE001 - shown to the user
                errors.append("%s via %s: %s" % (label, tool, _redact(error, secrets)))
                continue
            source["is_file"] = len(entries) == 1 and entries[0]["path"] == "" and not entries[0]["dir"]
            entries.sort(key=lambda e: e["path"])
            return {"entries": entries, "truncated": truncated, "transport_used": "%s (%s)" % (label, tool),
                    "transport_index": index, "errors": errors}
    return {"entries": [], "truncated": False, "transport_used": None, "transport_index": None, "errors": errors}


# --------------------------------------------------------------------------------------------- destinations

def _norm_dest(dest):
    dest = _check_text((dest or "").strip(), "The destination")
    if not dest.startswith("/"):
        raise ValueError("The destination must be an absolute folder path")
    return posixpath.normpath(dest)


def _norm_rel(rel, what="a path"):
    rel = _check_text(str(rel or ""), what).strip()
    rel = rel.strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("%s must not contain '..'" % what)
    return "/".join(parts)


def _primary_transport(source, options):
    ts = source.get("transports") or []
    idx = (options or {}).get("transport")
    if isinstance(idx, int) and 0 <= idx < len(ts):
        return ts[idx]
    return ts[0] if ts else {}


def _root_parts(source, options):
    """(components of the source folder, file name or None)."""
    kind = source.get("kind")
    if kind in ("geo", "sra"):
        return [source["accession"]], None
    t = _primary_transport(source, options)
    path = source.get("key") if kind == "s3" else t.get("path")
    comps = _components(path)
    is_file = source.get("is_file")
    if is_file is None:
        is_file = _looks_like_file(path or "")
    name = None
    if is_file and comps:
        name = comps.pop()
    if not comps and not name:
        comps = [_safe_name(t.get("user") or t.get("host") or "download", 120)]
    return comps, name


def _final_root(source, dest, options):
    options = options or {}
    dest = _norm_dest(dest)
    comps, name = _root_parts(source, options)
    if options.get("keep_levels") not in (None, ""):
        strip = len(comps) - max(0, int(options["keep_levels"]))
    elif options.get("strip_levels") not in (None, ""):
        strip = int(options["strip_levels"])
    else:
        strip = len(comps) if name else len(comps) - 1
    strip = max(0, min(len(comps), strip))
    sub = _norm_rel(options.get("subfolder"), "The subfolder") if options.get("subfolder") else ""
    parts = [dest] + ([sub] if sub else []) + comps[strip:]
    return posixpath.join(*parts), name


def _selected_entries(entries, selection):
    if selection is None:
        return [e for e in entries if not e.get("dir")]
    out = []
    sel = [_norm_rel(s if isinstance(s, str) else s.get("path", ""), "A selected path") for s in selection]
    for e in entries:
        if e.get("dir"):
            continue
        p = e["path"]
        if any(p == s or p.startswith(s + "/") or s == "" for s in sel):
            out.append(e)
    return out


def destination_preview(source, dest, selection=None, options=None, entries=None):
    """{"root", "files": [{"src", "dest", "bytes"}], "items"} - exactly where plan()'s script writes."""
    options = dict(options or {})
    kind = source.get("kind")
    if kind in ("geo", "sra"):
        return _geo_preview(source, dest, selection, options)
    root, name = _final_root(source, dest, options)
    t = _primary_transport(source, options)
    base = _transport_text(t) if kind != "s3" else "s3://%s/%s" % (source["bucket"], source.get("key") or "")
    entries = entries if entries is not None else options.get("entries")
    files, items = [], []
    if name:
        files.append({"src": base, "dest": posixpath.join(root, name), "bytes": (entries or [{}])[0].get("bytes")
                      if entries and len(entries) == 1 else None})
        items.append({"rel": "", "dir": False, "local": posixpath.join(root, name)})
        return {"root": root, "files": files, "items": items}
    if selection:
        for s in selection:
            rel = _norm_rel(s if isinstance(s, str) else s.get("path", ""), "A selected path")
            known = next((e for e in entries or [] if e["path"] == rel), None)
            is_dir = known["dir"] if known else (not _looks_like_file(rel) or
                                                 (not isinstance(s, str) and bool(s.get("dir"))))
            if not isinstance(s, str) and "dir" in s:
                is_dir = bool(s["dir"])
            items.append({"rel": rel, "dir": is_dir, "local": posixpath.join(root, rel) if rel else root})
    else:
        items.append({"rel": "", "dir": True, "local": root})
    if entries:
        for e in _selected_entries(entries, selection):
            files.append({"src": base.rstrip("/") + "/" + e["path"], "dest": posixpath.join(root, e["path"]),
                          "bytes": e.get("bytes")})
    else:
        for it in items:
            files.append({"src": base.rstrip("/") + ("/" + it["rel"] if it["rel"] else "/"),
                          "dest": it["local"] + ("/" if it["dir"] else ""), "bytes": None, "dir": it["dir"]})
    return {"root": root, "files": files, "items": items}


def _geo_lookup_for(source, options):
    info = options.get("lookup") or source.get("lookup")
    if not info:
        info = geo_lookup(source["accession"])
    return info


def _fastq_suffix(name, run, nfiles):
    stem = name
    for ext in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
            break
    m = re.match(r"^%s_(\d+)$" % re.escape(run), stem)
    if m:
        return "_R" + m.group(1)
    if stem == run:
        return "_R1" if nfiles == 1 else "_unpaired"
    return "_" + _safe_name(stem[len(run):] if stem.startswith(run) else stem)


def _geo_preview(source, dest, selection, options):
    info = _geo_lookup_for(source, options)
    root, _ = _final_root(source, dest, options)
    rename = options.get("rename") or "run"
    sel_runs, sel_urls, sel_gsm_supp, everything = set(), set(), set(), selection is None
    for s in selection or []:
        if isinstance(s, str):
            if re.match(r"^[SED]RR\d+$", s):
                sel_runs.add(s)
            elif "://" in s:
                sel_urls.add(s)
            elif re.match(r"^GSM\d+$", s):
                sel_gsm_supp.add(s)
            continue
        typ = s.get("type")
        if typ in ("fastq", "run") and s.get("run") and not s.get("url"):
            sel_runs.add(s["run"])
        elif s.get("url"):
            sel_urls.add(s["url"])
        elif typ == "supplementary" and s.get("gsm"):
            sel_gsm_supp.add(s["gsm"])
    files, runs_plan, used = [], [], set()

    def unique(path):
        base, n = path, 2
        while path in used:
            stem, dot, ext = base.partition(".")
            path = "%s_%d%s%s" % (stem, n, dot, ext)
            n += 1
        used.add(path)
        return path

    for s in info.get("samples", []):
        nruns = len(s.get("runs") or [])
        for run in s.get("runs") or []:
            rid = run["run"]
            fq = run.get("fastq") or []
            chosen = [f for f in fq if everything or rid in sel_runs or f["url"] in sel_urls or
                      f.get("ftp_url") in sel_urls]
            if not fq and (everything or rid in sel_runs):
                layout = (run.get("layout") or "").upper()
                fq_names = ([rid + "_1.fastq.gz", rid + "_2.fastq.gz"] if layout == "PAIRED" else [rid + ".fastq.gz"])
                chosen = [{"name": n, "url": None, "ftp_url": None, "bytes": None, "md5": None, "predicted": True}
                          for n in fq_names]
            if not chosen:
                continue
            entry = {"run": rid, "files": [], "sra_only": not fq}
            for f in chosen:
                if rename == "gsm_title" and s.get("gsm"):
                    prefix = s["gsm"] + ("_" + _safe_name(s.get("title")) if s.get("title") else "")
                    if nruns > 1:
                        prefix += "_" + rid
                    name = prefix + _fastq_suffix(f["name"], rid, len(fq) or len(chosen)) + ".fastq.gz"
                else:
                    name = f["name"]
                path = unique(posixpath.join(root, "fastq", name))
                item = {"type": "fastq", "run": rid, "gsm": s.get("gsm"), "url": f.get("url"), "name": f["name"]}
                files.append({"src": f.get("url") or ("SRA " + rid), "dest": path, "bytes": f.get("bytes"),
                              "md5": f.get("md5"), "item": item, "urls": [u for u in (f.get("url"), f.get("ftp_url"))
                                                                         if u]})
                entry["files"].append({"suffix": _sra_suffix(f["name"], rid), "dest": path, "file": files[-1]})
            runs_plan.append(entry)
        for f in s.get("supplementary") or []:
            if f["url"] in sel_urls or f.get("ftp_url") in sel_urls or s.get("gsm") in sel_gsm_supp:
                path = unique(posixpath.join(root, "supplementary", _safe_name(f["name"], 200)
                                             if "/" in f["name"] else f["name"]))
                files.append({"src": f["url"], "dest": path, "bytes": f.get("bytes"), "md5": None,
                              "bytes_approx": True,
                              "item": {"type": "supplementary", "gsm": s.get("gsm"), "url": f["url"]},
                              "urls": [u for u in (f["url"], f.get("ftp_url")) if u]})
    for f in info.get("series_supplementary") or []:
        if everything or f["url"] in sel_urls or f.get("ftp_url") in sel_urls:
            path = unique(posixpath.join(root, "supplementary", f["name"]))
            files.append({"src": f["url"], "dest": path, "bytes": f.get("bytes"), "md5": None,
                          "bytes_approx": bool(f.get("bytes_approx", f.get("bytes") is not None)),
                          "item": {"type": "supplementary", "url": f["url"]},
                          "urls": [u for u in (f["url"], f.get("ftp_url")) if u]})
    return {"root": root, "files": files, "runs": runs_plan, "lookup": info}


def _sra_suffix(name, run):
    """fasterq-dump --split-files names: '<run>_1.fastq' -> '_1', '<run>.fastq' -> ''."""
    stem = re.sub(r"\.(fastq|fq)(\.gz)?$", "", name)
    return stem[len(run):] if stem.startswith(run) else ""


# --------------------------------------------------------------------------------------------- plan / scripts

COMMON_BASH = r'''
wb_step() { printf '##WB step %s\n' "$*"; }
wb_progress() { printf '##WB progress %s\n' "$1"; }
wb_have() { command -v "$1" >/dev/null 2>&1; }
wb_fail() { printf 'FAILED: %s\n' "$*" >&2; printf '%s\n' "$*" >> "$WB_TMP/failed"; }
wb_size() { stat -c %s -- "$1" 2>/dev/null || echo -1; }
wb_md5_ok() { [ "$(md5sum < "$1" | cut -d' ' -f1)" = "$2" ]; }
# wb_probe HOST PORT: can a TCP connection be opened (10 s)?
wb_probe() { timeout 10 bash -c 'exec 3<>"/dev/tcp/$1/$2"' wb_probe "$1" "$2" >/dev/null 2>&1; }
# wb_retry ATTEMPTS COMMAND...: run COMMAND until it succeeds, waiting longer each time (127 = tool missing: no retry)
wb_retry() {
  local n=$1 i=1 delay=${WB_RETRY_DELAY:-15} rc
  shift
  while :; do
    rc=0
    "$@" || rc=$?
    if [ "$rc" -eq 0 ]; then return 0; fi
    if [ "$rc" -eq 127 ] || [ "$i" -ge "$n" ]; then return "$rc"; fi
    printf 'attempt %d of %d failed (exit %d); retrying in %ds\n' "$i" "$n" "$rc" "$delay"
    sleep "$delay"
    i=$((i + 1)); delay=$((delay * 2)); if [ "$delay" -gt 300 ]; then delay=300; fi
  done
}
# wb_watch TOTAL_BYTES DIR: progress from the size of DIR while the download runs (background)
wb_watch() {
  local total=$1 dir=$2 parent=$$ b k
  [ "$total" -gt 0 ] 2>/dev/null || return 0
  while kill -0 "$parent" 2>/dev/null; do
    for k in 1 2 3 4 5 6; do sleep 5; kill -0 "$parent" 2>/dev/null || return 0; done
    b=$(du -sb -- "$dir" 2>/dev/null | cut -f1) || b=0
    b=$(( ${b:-0} * 100 / total )); if [ "$b" -gt 99 ]; then b=99; fi
    wb_progress "$b"
  done
}
wb_unwatch() { if [ -n "${WB_WATCH:-}" ]; then kill "$WB_WATCH" 2>/dev/null || true; WB_WATCH=; fi; }
wb_secret() { local var="WB_SECRET_$1"; printf '%s' "${!var}"; }
# wb_pass SECRET COMMAND...: run an ssh-based COMMAND with the password from WB_SECRET_<SECRET> (sshpass -e)
wb_pass() {
  local name=$1
  shift
  if [ -z "$name" ]; then "$@"; return; fi
  wb_have sshpass || { echo "sshpass is not installed"; return 127; }
  SSHPASS="$(wb_secret "$name")" sshpass -e "$@"
}
WB_SSH_OPTS=(-oStrictHostKeyChecking=accept-new -oConnectTimeout=30 -oServerAliveInterval=30 -oServerAliveCountMax=10 -oNumberOfPasswordPrompts=1)
# wb_lftp SECRET FILE: run an lftp command file, the password (if any) in LFTP_PASSWORD (open --env-password)
wb_lftp() {
  wb_have lftp || { echo "lftp is not installed"; return 127; }
  if [ -n "$1" ]; then LFTP_PASSWORD="$(wb_secret "$1")" lftp -f "$2" </dev/null; else lftp -f "$2" </dev/null; fi
}
# wb_sftp PORT SECRET TARGET BATCHFILE: OpenSSH sftp batch (resumes with get -a)
wb_sftp() {
  wb_have sftp || { echo "sftp is not installed"; return 127; }
  local bm=-oBatchMode=yes
  if [ -n "$2" ]; then bm=-oBatchMode=no; fi
  wb_pass "$2" sftp "$bm" -oPort="$1" "${WB_SSH_OPTS[@]}" -b "$4" "$3" </dev/null
}
# wb_scp PORT SECRET SOURCE DEST [-r]: scp (no resume: the last resort)
wb_scp() {
  wb_have scp || { echo "scp is not installed"; return 127; }
  local bm=-oBatchMode=yes
  if [ -n "$2" ]; then bm=-oBatchMode=no; fi
  wb_pass "$2" scp ${5:-} -p "$bm" -P "$1" "${WB_SSH_OPTS[@]}" "$3" "$4" </dev/null
}
# wb_wget USER SECRET ARGS...: wget; the credentials reach it through --use-askpass (a helper printing
# WB_ASK_USER / WB_ASK_PASS from its environment), so they are neither on a command line nor in a file
wb_wget() {
  wb_have wget || { echo "wget is not installed"; return 127; }
  local user=$1 secret=$2
  shift 2
  if [ -z "$user" ] && [ -z "$secret" ]; then wget -e robots=off "$@" </dev/null; return; fi
  if [ ! -x "$WB_TMP/askpass" ]; then
    printf '%s\n' '#!/bin/sh' 'case "$1" in *sername*) printf "%s\n" "$WB_ASK_USER" ;; *) printf "%s\n" "$WB_ASK_PASS" ;; esac' > "$WB_TMP/askpass"
    chmod 700 "$WB_TMP/askpass"
  fi
  WB_ASK_USER="$user" WB_ASK_PASS="$(if [ -n "$secret" ]; then wb_secret "$secret"; fi)" \
    wget -e robots=off --auth-no-challenge --use-askpass="$WB_TMP/askpass" "$@" </dev/null
}
# wb_curl USER SECRET ARGS...: curl with the credentials in a configuration read from standard input (-K -)
wb_curl() {
  wb_have curl || { echo "curl is not installed"; return 127; }
  local user=$1 secret=$2 pw=""
  shift 2
  if [ -n "$secret" ]; then pw=$(wb_secret "$secret"); fi
  if [ -n "$user" ]; then
    pw="$user:$pw"; pw=${pw//\\/\\\\}; pw=${pw//\"/\\\"}
    printf 'user = "%s"\n' "$pw" | curl -K - "$@"
  else
    curl "$@" </dev/null
  fi
}
# wb_fetch DEST BYTES MD5 URL...: one file, resumable, each URL with wget then curl, checked by size and md5
wb_fetch() {
  local dest=$1 bytes=$2 md5=$3 try url got
  shift 3
  mkdir -p -- "$(dirname -- "$dest")"
  if [ -n "$bytes" ] && [ -f "$dest" ] && [ "$(wb_size "$dest")" = "$bytes" ]; then
    if [ -z "$md5" ] || [ "$WB_VERIFY_MD5" != 1 ] || wb_md5_ok "$dest" "$md5"; then
      printf 'already complete: %s\n' "$dest"; return 0
    fi
  fi
  for try in 1 2 3 4; do
    for url in "$@"; do
      [ -n "$url" ] || continue
      got=0
      if wb_have wget && wget -c -nv --tries=3 --timeout=60 --waitretry=15 -O "$dest" -- "$url" </dev/null; then got=1
      elif wb_have curl && curl -fsSL --retry 3 --retry-delay 15 --connect-timeout 30 -C - -o "$dest" -- "$url" </dev/null; then got=1
      fi
      [ "$got" = 1 ] || continue
      if [ -n "$bytes" ] && [ "$(wb_size "$dest")" != "$bytes" ]; then
        printf 'size of %s is %s, expected %s\n' "$dest" "$(wb_size "$dest")" "$bytes"; continue
      fi
      if [ -n "$md5" ] && [ "$WB_VERIFY_MD5" = 1 ]; then
        if wb_md5_ok "$dest" "$md5"; then printf 'md5 ok: %s\n' "$dest"; return 0; fi
        printf 'md5 mismatch for %s: kept as %s.md5-mismatch, downloading again\n' "$dest" "$dest"
        mv -f -- "$dest" "$dest.md5-mismatch"; continue
      fi
      return 0
    done
    sleep $((try * 20))
  done
  return 1
}
'''

VERIFY_BASH = r'''
# wb_verify_md5 ROOT STRICT WORKERS [PATH...]: check files against md5 lists found under ROOT (md5sum.txt, MD5.txt, *.md5)
wb_verify_md5() {
  if ! wb_have python3; then echo "python3 not found: md5 lists were not checked"; return 0; fi
  python3 - "$@" <<'WB_PY'
import hashlib, os, re, sys
from concurrent.futures import ThreadPoolExecutor
root, strict, workers, only = sys.argv[1], sys.argv[2] == "1", max(1, int(sys.argv[3])), sys.argv[4:]
HEX = r"([0-9a-fA-F]{32})"
def is_list(name):
    n = name.lower()
    return n.endswith((".md5", ".md5sum")) or ("md5" in n and n.endswith((".txt", ".tsv", ".csv", ".list", ".chk"))) \
        or n in ("md5", "md5sum", "md5sums", "checksums.txt", "checksum.txt")
lists, by_name = [], {}
for d, dirs, names in os.walk(root):
    dirs[:] = [x for x in dirs if not x.startswith(".wb-")]
    for n in names:
        p = os.path.join(d, n)
        by_name.setdefault(n, []).append(p)
        if is_list(n) and os.path.getsize(p) < 50 * 1024 * 1024:
            lists.append(p)
def parse(path):
    out = []
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip().strip("\ufeff")
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^MD5\s*\((.+)\)\s*=\s*" + HEX + "$", line, re.I)
        if m:
            out.append((m.group(2).lower(), m.group(1))); continue
        m = re.match(r"^\\?" + HEX + r"\s+\*?(.+)$", line)
        if m:
            out.append((m.group(1).lower(), m.group(2).strip())); continue
        m = re.match(r"^(.+?)[\s,;]+" + HEX + "$", line)
        if m:
            out.append((m.group(2).lower(), m.group(1).strip().strip('"'))); continue
        m = re.match("^" + HEX + "$", line)
        if m and path.lower().endswith(".md5"):
            out.append((m.group(1).lower(), os.path.basename(path)[:-4]))
    return out
def locate(listfile, name):
    name = name.strip().strip('"').replace("\\", "/")
    if name.startswith("./"):
        name = name[2:]
    parts = [p for p in name.split("/") if p]
    for start in range(len(parts)):
        rel = os.path.join(*parts[start:])
        for base in (os.path.dirname(listfile), root):
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    hits = by_name.get(parts[-1] if parts else name, [])
    return hits[0] if len(hits) == 1 else None
checks, missing = {}, []
for lf in lists:
    for digest, name in parse(lf):
        p = locate(lf, name)
        if p is None:
            missing.append(name)
        elif os.path.abspath(p) != os.path.abspath(lf):
            checks[os.path.abspath(p)] = digest
if only:
    keep = [os.path.abspath(x) for x in only]
    checks = {p: d for p, d in checks.items() if any(p == k or p.startswith(k.rstrip("/") + "/") for k in keep)}
if not lists:
    print("No md5 list was found in the download: nothing to verify")
    sys.exit(0)
print("##WB step Verifying md5 sums (%d files)" % len(checks), flush=True)
def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
bad, done = [], 0
total = sum(os.path.getsize(p) for p in checks) or 1
seen = 0
with ThreadPoolExecutor(workers) as pool:
    for p, got in zip(list(checks), pool.map(md5, list(checks))):
        seen += os.path.getsize(p)
        if got != checks[p]:
            bad.append(p)
            print("md5 MISMATCH: %s" % p, flush=True)
        else:
            print("md5 ok: %s" % p, flush=True)
        print("##WB progress %d" % (seen * 100 // total), flush=True)
print("md5: %d ok, %d mismatched, %d listed but not downloaded" % (len(checks) - len(bad), len(bad), len(missing)))
for name in missing[:20]:
    print("  not found: %s" % name)
sys.exit(1 if bad or (strict and missing and not only) else 0)
WB_PY
}
'''

FINISH_BASH = r'''
wb_finish() {
  local n size
  wb_unwatch
  wb_step "Summary"
  n=$(find "$WB_ROOT" -type f ! -path '*/.wb-*' ! -name '*.md5-mismatch' 2>/dev/null | wc -l)
  size=$(du -sh -- "$WB_ROOT" 2>/dev/null | cut -f1)
  printf 'Destination: %s\nFiles there: %s, total size %s\n' "$WB_ROOT" "$n" "${size:-?}"
  if [ "$n" = 0 ]; then wb_fail "nothing was downloaded (is the address or the selection right?)"; fi
  if [ -s "$WB_TMP/failed" ]; then
    printf 'Problems:\n'
    sed 's/^/  - /' "$WB_TMP/failed"
    rm -rf -- "$WB_TMP"
    return 1
  fi
  rm -rf -- "$WB_TMP"
  wb_progress 100
  printf 'Download complete.\n'
}
'''


def _q(value):
    return shlex.quote(str(value))


def _header(source, root, options, secrets_used):
    lines = [
        "# Omics Workbench download: %s" % re.sub(r"[\r\n]", " ", str(source.get("label") or source.get("kind"))),
        "# Generated %s. This file holds no passwords: they come from WB_SECRET_* variables." %
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "WB_ROOT=%s" % _q(root),
        "WB_PARALLEL=%d" % max(1, int(options.get("parallel") or 4)),
        "WB_VERIFY_MD5=%d" % (0 if options.get("verify_md5") is False else 1),
        'WB_TMP=$(mktemp -d "${WB_JOB_DIR:-${TMPDIR:-/tmp}}/wb-download.XXXXXX")',
        ': > "$WB_TMP/failed"',
        "WB_WATCH=",
    ]
    for name in secrets_used:
        lines.append(': "${%s%s:?the password (%s%s) was not passed to the job}"'
                     % (SECRET_ENV_PREFIX, name, SECRET_ENV_PREFIX, name))
    return "\n".join(lines) + "\n" + COMMON_BASH


def _printf_file(path_expr, lines):
    """bash that writes LINES (each shell-quoted) to a file."""
    if not lines:
        return ": > %s\n" % path_expr
    out = "printf '%s\\n' \\\n"
    out += " \\\n".join("  " + _q(l) for l in lines)
    return out + " > %s\n" % path_expr


def _methods_for(scheme, method):
    tools = TOOLS.get(scheme, [])
    if method in (None, "", "auto"):
        return tools
    return [method] if method in tools else []


def _http_url(t, remote, is_dir):
    port = "" if t["port"] == _default_port(t["scheme"]) else ":%d" % t["port"]
    path = remote if remote.startswith("/") else "/" + remote
    if is_dir and not path.endswith("/"):
        path += "/"
    url = "%s://%s%s%s" % (t["scheme"], t["host"], port, urllib.parse.quote(path, safe="/~@:+,=-._"))
    if t.get("query") and not is_dir and remote == t.get("path"):
        url += "?" + t["query"]
    return url


def _remote_join(t, rel):
    base = t.get("path") or ""
    if not rel:
        return base
    if not base:
        return rel
    return base.rstrip("/") + "/" + rel


def _transport_block(i, t, items, files, options, secret_ref):
    """bash for one transport: command files and wb_try_<i> trying its tools in order. Returns (setup, function)."""
    method = options.get("method") or "auto"
    tools = _methods_for(t["scheme"], method)
    scheme, user = t["scheme"], t.get("user") or ""
    sec = secret_ref or ""
    setup, body = [], []
    label = _transport_text(t)
    parallel = max(1, int(options.get("parallel") or 4))
    body.append("wb_try_%d() {" % i)
    body.append("  # %s" % re.sub(r"[\r\n]", " ", label))
    for tool in tools:
        if tool == "lftp":
            cmds = _lftp_settings() + [_lftp_open_line(t, bool(sec))]
            for it in items:
                remote = _remote_join(t, it["rel"]) or "."
                if it["dir"]:
                    cmds.append("mirror -c --parallel=%d --use-pget-n=8 --verbose %s %s"
                                % (parallel, _lftp_quote(remote), _lftp_quote(it["local"])))
                else:
                    cmds.append("pget -c -n 8 %s -o %s" % (_lftp_quote(remote), _lftp_quote(it["local"])))
            setup.append(_printf_file('"$WB_TMP/t%d.lftp"' % i, cmds))
            body.append('  echo "Trying lftp ($WB_TMP/t%d.lftp)"' % i)
            body.append('  wb_retry 4 wb_lftp %s "$WB_TMP/t%d.lftp" && return 0' % (_q(sec) if sec else "''", i))
        elif tool == "sftp":
            batch = []
            for it in items:
                remote = _remote_join(t, it["rel"])
                if it["dir"]:
                    batch.append("get -a -r %s %s/" % ((_esc_glob(remote.rstrip("/")) + "/*") if remote else "*",
                                                      _esc_glob(it["local"])))
                else:
                    batch.append("get -a %s %s" % (_esc_glob(remote), _esc_glob(it["local"])))
            setup.append(_printf_file('"$WB_TMP/t%d.sftp"' % i, batch))
            target = ("%s@%s" % (user, t["host"])) if user else t["host"]
            body.append('  echo "Trying OpenSSH sftp"')
            body.append('  wb_retry 4 wb_sftp %d %s %s "$WB_TMP/t%d.sftp" && return 0'
                        % (int(t["port"]), _q(sec) if sec else "''", _q(target), i))
        elif tool == "scp":
            target = ("%s@%s" % (user, t["host"])) if user else t["host"]
            fn = ["wb_scp_%d() {" % i]
            if files:
                for f in files:
                    remote = _remote_join(t, f["rel"])
                    cond = '[ "$(wb_size %s)" = %s ] || ' % (_q(f["local"]), _q(f["bytes"])) if f.get("bytes") \
                        is not None else ""
                    fn.append("  %smkdir -p -- %s" % (cond, _q(posixpath.dirname(f["local"]))))
                    fn.append("  %swb_scp %d %s %s %s || return 1"
                              % (cond, int(t["port"]), _q(sec) if sec else "''",
                                 _q(target + ":" + _esc_glob(remote)), _q(f["local"])))
            else:
                for it in items:
                    remote = _remote_join(t, it["rel"])
                    if it["dir"]:
                        src = target + ":" + ((_esc_glob(remote.rstrip("/")) + "/*") if remote else "*")
                        fn.append("  wb_scp %d %s %s %s -r || return 1"
                                  % (int(t["port"]), _q(sec) if sec else "''", _q(src), _q(it["local"] + "/")))
                    else:
                        fn.append("  wb_scp %d %s %s %s || return 1"
                                  % (int(t["port"]), _q(sec) if sec else "''", _q(target + ":" + _esc_glob(remote)),
                                     _q(it["local"])))
            fn.append("}")
            setup.append("\n".join(fn) + "\n")
            body.append('  echo "Trying scp (cannot resume a partly copied file)"')
            body.append("  wb_retry 3 wb_scp_%d && return 0" % i)
        elif tool == "wget":
            fn = ["wb_wget_%d() {" % i]
            for it in items:
                remote = _remote_join(t, it["rel"])
                if it["dir"]:
                    url = _http_url(t, remote, True)
                    cut = len(_components(urllib.parse.urlsplit(url).path))
                    fn.append("  wb_wget %s %s -c -r -np -nH --cut-dirs=%d --reject 'index.html*' -nv --tries=5 "
                              "--waitretry=30 --timeout=60 -P %s -- %s || return 1"
                              % (_q(user) if user else "''", _q(sec) if sec else "''", cut, _q(it["local"]), _q(url)))
                else:
                    fn.append("  wb_wget %s %s -c -nv --tries=5 --waitretry=30 --timeout=60 -O %s -- %s || return 1"
                              % (_q(user) if user else "''", _q(sec) if sec else "''", _q(it["local"]),
                                 _q(_http_url(t, remote, False))))
            fn.append("}")
            setup.append("\n".join(fn) + "\n")
            body.append('  echo "Trying wget"')
            body.append("  wb_retry 4 wb_wget_%d && return 0" % i)
        elif tool == "curl":
            todo = files or [it for it in items if not it["dir"]]
            if not todo or (not files and any(it["dir"] for it in items)):
                continue
            fn = ["wb_curl_%d() {" % i]
            for f in todo:
                remote = _remote_join(t, f["rel"])
                cond = '[ "$(wb_size %s)" = %s ] || ' % (_q(f["local"]), _q(f["bytes"])) \
                    if f.get("bytes") is not None else ""
                fn.append("  %smkdir -p -- %s" % (cond, _q(posixpath.dirname(f["local"]))))
                fn.append("  %swb_curl %s %s -fL --retry 3 --retry-delay 20 --connect-timeout 30 -C - -o %s -- %s "
                          "|| return 1" % (cond, _q(user) if user else "''", _q(sec) if sec else "''",
                                           _q(f["local"]), _q(_http_url(t, remote, False))))
            fn.append("}")
            setup.append("\n".join(fn) + "\n")
            body.append('  echo "Trying curl"')
            body.append("  wb_retry 3 wb_curl_%d && return 0" % i)
    body.append("  return 1")
    body.append("}")
    return "".join(setup), "\n".join(body) + "\n", bool(tools)


def _remote_script(source, root, preview, options, secrets):
    items = preview["items"]
    entries = options.get("entries")
    files = []
    if entries and not source.get("is_file"):
        for e in _selected_entries(entries, options.get("_selection")):
            files.append({"rel": e["path"], "local": posixpath.join(root, e["path"]), "bytes": e.get("bytes")
                          if not e.get("bytes_approx") else None})
    elif items and not items[0]["dir"] and len(items) == 1:
        files = [{"rel": items[0]["rel"], "local": items[0]["local"], "bytes": None}]
    total = sum(f["bytes"] or 0 for f in files) if files else 0
    transports = list(enumerate(source.get("transports") or []))
    pick = options.get("transport")
    if isinstance(pick, int) and 0 <= pick < len(transports):
        transports = [transports[pick]] + [x for x in transports if x[0] != pick]
    used_secrets, blocks, tries = [], [], []
    for i, t in transports:
        ref = t.get("secret_ref") or ("PASSWORD" if t.get("has_password") else None)
        if ref and ref not in used_secrets:
            used_secrets.append(ref)
        setup, fn, ok = _transport_block(i, t, items, files, options, ref)
        if not ok:
            continue
        blocks.append(setup + fn)
        tries.append((i, t))
    if not tries:
        raise ValueError("Method %r cannot be used with %s" % (options.get("method"),
                                                               ", ".join(t["scheme"] for _i, t in transports)))
    script = _header(source, root, options, used_secrets) + VERIFY_BASH + FINISH_BASH
    script += "\n".join(blocks)
    script += "\nwb_step %s\n" % _q("Preparing " + root)
    dirs = sorted({it["local"] if it["dir"] else posixpath.dirname(it["local"]) for it in items} | {root})
    script += "mkdir -p -- %s\n" % " ".join(_q(d) for d in dirs)
    script += 'wb_step "Checking which servers and ports answer"\n'
    for i, t in tries:
        port = t["port"] or _default_port(t["scheme"])
        script += ('if wb_probe %s %d; then WB_UP_%d=1; echo %s; else WB_UP_%d=0; echo %s; fi\n'
                   % (_q(t["host"]), int(port), i, _q("reachable: " + _transport_text(t)), i,
                      _q("not reachable (port blocked?): " + _transport_text(t))))
    if total:
        script += 'wb_watch %d "$WB_ROOT" &\nWB_WATCH=$!\n' % total
    script += "WB_DONE=0\n"
    for i, t in tries:
        script += ('if [ "$WB_DONE" = 0 ] && [ "$WB_UP_%d" = 1 ]; then wb_step %s; '
                   'if wb_try_%d; then WB_DONE=1; fi; fi\n'
                   % (i, _q("Downloading via " + _transport_text(t)), i))
    script += 'if [ "$WB_DONE" = 0 ]; then\n  echo "No reachable transport worked; trying the others anyway"\n'
    for i, t in tries:
        script += ('  if [ "$WB_DONE" = 0 ] && [ "$WB_UP_%d" = 0 ]; then wb_step %s; '
                   'if wb_try_%d; then WB_DONE=1; fi; fi\n'
                   % (i, _q("Downloading via " + _transport_text(t)), i))
    script += "fi\nwb_unwatch\n"
    script += 'if [ "$WB_DONE" = 0 ]; then wb_fail "every transport and tool failed (see the messages above)"; fi\n'
    script += _verify_lines(items, root, options)
    script += "wb_finish || exit 1\n"
    return script, used_secrets


def _verify_lines(items, root, options):
    if options.get("verify_md5") is False:
        return ""
    whole = len(items) == 1 and items[0]["rel"] == "" and items[0]["dir"]
    only = [] if whole else [it["local"] for it in items]
    return ('if [ "$WB_DONE" = 1 ]; then\n  wb_step "Verifying md5 sums"\n'
            '  wb_verify_md5 "$WB_ROOT" %d "$WB_PARALLEL" %s || wb_fail "md5 verification failed"\nfi\n'
            % (1 if whole else 0, " ".join(_q(x) for x in only)))


def _rclone_filter_escape(path):
    return re.sub(r"([\\*?\[\]{}])", r"\\\1", path)


def _s3_script(source, root, preview, options):
    bucket, key = source["bucket"], (source.get("key") or "").strip("/")
    profile = source.get("profile") or "default"
    region = source.get("region") or bucket_settings(bucket)["region"] or _REGION_CACHE.get(bucket)
    method = options.get("method") or "auto"
    tools = _methods_for("s3", method)
    if not tools:
        raise ValueError("Method %r cannot be used with s3" % method)
    items = preview["items"]
    selection = [it for it in items if it["rel"]]
    parallel = max(1, int(options.get("parallel") or 16))
    s = _header(source, root, options, []) + VERIFY_BASH + FINISH_BASH
    s += "export AWS_PROFILE=%s\n" % _q(profile)
    if region:
        s += "WB_REGION=%s\n" % _q(region)
    else:
        s += ("wb_step \"Looking up the bucket's region\"\n"
              "WB_REGION=$(aws s3api get-bucket-location --bucket %s --query LocationConstraint --output text "
              "2>/dev/null || true)\n"
              'case "$WB_REGION" in None|null) WB_REGION=us-east-1 ;; EU) WB_REGION=eu-west-1 ;; esac\n'
              'if [ -z "$WB_REGION" ]; then WB_REGION=$(curl -sI https://%s.s3.amazonaws.com/ 2>/dev/null | '
              "awk -F': ' 'tolower($1)==\"x-amz-bucket-region\" {print $2}' | tr -d '\\r'); fi\n"
              'WB_REGION=${WB_REGION:-us-east-1}\necho "bucket region: $WB_REGION"\n' % (_q(bucket), bucket))
    s += "export AWS_REGION=$WB_REGION\n"
    s += ("wb_rclone_progress() { local line p; while IFS= read -r line; do printf '%s\\n' \"$line\"; "
          "p=$(printf '%s' \"$line\" | sed -n 's/.*, \\([0-9]\\{1,3\\}\\)%,.*/\\1/p'); "
          "if [ -n \"$p\" ]; then wb_progress \"$p\"; fi; done; }\n")
    remote = ":s3,env_auth:%s/%s" % (bucket, key)
    rflags = ('--s3-provider AWS --s3-region "$WB_REGION" --s3-no-check-bucket --fast-list --transfers %d '
              '--multi-thread-streams 16 --stats 30s --stats-one-line --stats-log-level NOTICE --retries 5 '
              '--low-level-retries 20' % parallel)
    if source.get("is_file"):
        name = preview["items"][0]["local"]
        rclone = ("wb_have rclone || return 127\n  rclone copyto %s %s %s 2>&1 | wb_rclone_progress"
                  % (_q(remote), _q(name), rflags))
        aws = "wb_have aws || return 127\n  aws s3 cp %s %s --region \"$WB_REGION\" --only-show-errors" % (
            _q("s3://%s/%s" % (bucket, key)), _q(name))
    else:
        filt = ""
        aws_filters = ""
        if selection:
            lines = []
            for it in selection:
                e = _rclone_filter_escape(it["rel"])
                lines.append("+ /%s" % e)        # the selection is a file ...
                lines.append("+ /%s/**" % e)     # ... or a folder: both rules are harmless
            lines.append("- **")
            s += _printf_file('"$WB_TMP/filter.txt"', lines)
            filt = ' --filter-from "$WB_TMP/filter.txt"'
            aws_filters = " --exclude '*'" + "".join(
                " --include %s --include %s" % (_q(it["rel"]), _q(it["rel"] + "/*")) for it in selection)
        rclone = ("wb_have rclone || return 127\n  rclone copy %s \"$WB_ROOT\" %s%s 2>&1 | wb_rclone_progress"
                  % (_q(remote), rflags, filt))
        aws = ("wb_have aws || return 127\n  aws s3 sync %s \"$WB_ROOT\" --region \"$WB_REGION\" --only-show-errors%s"
               % (_q("s3://%s/%s/" % (bucket, key) if key else "s3://%s/" % bucket), aws_filters))
    s += "wb_s3_rclone() {\n  %s\n}\nwb_s3_aws() {\n  %s\n}\n" % (rclone, aws)
    s += "wb_step %s\nmkdir -p -- %s\n" % (_q("Preparing " + root), _q(root))
    s += 'wb_step "Checking the connection to S3"\n'
    s += ('if wb_probe "s3.$WB_REGION.amazonaws.com" 443; then echo "S3 reachable"; '
          'else echo "s3.$WB_REGION.amazonaws.com:443 did not answer; trying anyway"; fi\n')
    s += "WB_DONE=0\n"
    for tool in tools:
        s += ('if [ "$WB_DONE" = 0 ]; then wb_step %s; if wb_retry 3 wb_s3_%s; then WB_DONE=1; fi; fi\n'
              % (_q("Downloading s3://%s/%s with %s" % (bucket, key, tool)), tool))
    s += 'if [ "$WB_DONE" = 0 ]; then wb_fail "rclone and aws s3 both failed (see above)"; fi\n'
    s += _verify_lines(items, root, options)
    s += "wb_finish || exit 1\n"
    return s


SRA_BASH = r'''
# wb_sra_dump RUN SUFFIX DEST [SUFFIX DEST...]: prefetch + fasterq-dump, gzip, move to the final names
wb_sra_dump() {
  local run=$1 work f base suf dest first=""
  shift
  declare -A map=()
  while [ $# -ge 2 ]; do map["x$1"]=$2; first=${first:-$2}; shift 2; done
  if ! wb_have prefetch || ! wb_have fasterq-dump; then echo "prefetch / fasterq-dump (sra-tools) not found"; return 1; fi
  work="$WB_ROOT/.wb-sra/$run"
  mkdir -p -- "$work/out"
  wb_retry 3 prefetch --max-size u --output-directory "$work" "$run" </dev/null || return 1
  (cd -- "$work" && fasterq-dump --split-files --include-technical --threads "$WB_PARALLEL" --temp "$work" --outdir "$work/out" "$run" </dev/null) || return 1
  for f in "$work/out/$run"*.fastq; do
    [ -e "$f" ] || continue
    base=${f##*/}; suf=${base#"$run"}; suf=${suf%.fastq}
    dest=${map["x$suf"]:-}
    if [ -z "$dest" ]; then dest="$(dirname -- "$first")/$run$suf.fastq.gz"; echo "extra read file $base -> $dest"; fi
    if wb_have pigz; then pigz -f -p "$WB_PARALLEL" -- "$f"; else gzip -f -- "$f"; fi
    mkdir -p -- "$(dirname -- "$dest")"
    mv -f -- "$f.gz" "$dest"
    printf 'converted from SRA: %s\n' "$dest"
  done
  rm -rf -- "$work"
  rmdir -- "$WB_ROOT/.wb-sra" 2>/dev/null || true
}
'''


def _geo_script(source, root, preview, options):
    method = options.get("method") or "auto"
    if method not in ("auto", "wget", "curl", "fasterq-dump"):
        raise ValueError("Method %r cannot be used for GEO / SRA downloads (use wget, curl or fasterq-dump)" % method)
    files = preview["files"]
    for f in files:
        for u in f.get("urls") or []:
            _check_text(u, "A URL")
    s = _header(source, root, options, []) + FINISH_BASH + SRA_BASH
    if method == "wget":
        s += "wb_have() { [ \"$1\" != curl ] && command -v \"$1\" >/dev/null 2>&1; }\n"
    elif method == "curl":
        s += "wb_have() { [ \"$1\" != wget ] && command -v \"$1\" >/dev/null 2>&1; }\n"
    total_bytes = sum(f.get("bytes") or 0 for f in files)
    known = all(f.get("bytes") and not f.get("bytes_approx") for f in files)
    s += "WB_TOTAL=%d\nWB_COUNT=%d\nWB_BYTES_KNOWN=%d\n" % (total_bytes, len(files), 1 if known and total_bytes else 0)
    s += r'''
: > "$WB_TMP/done"
: > "$WB_TMP/sra_runs"
wb_item_done() {
  printf '%s\n' "${1:-0}" >> "$WB_TMP/done"
  if [ "$WB_BYTES_KNOWN" = 1 ]; then
    wb_progress "$(awk -v t="$WB_TOTAL" '{s+=$1} END {printf "%d", s*100/t}' "$WB_TMP/done")"
  else
    wb_progress "$(( $(wc -l < "$WB_TMP/done") * 100 / WB_COUNT ))"
  fi
}
# wb_item KIND RUN DEST BYTES MD5 URL...
wb_item() {
  local kind=$1 run=$2 dest=$3 bytes=$4 md5=$5
  shift 5
  if [ "$#" -gt 0 ] && [ "${WB_METHOD:-auto}" != fasterq-dump ] && wb_fetch "$dest" "$bytes" "$md5" "$@"; then
    wb_item_done "$bytes"; return 0
  fi
  if [ "$kind" = fastq ] && [ -n "$run" ]; then
    printf '%s\n' "$run" >> "$WB_TMP/sra_runs"; return 0
  fi
  wb_fail "could not download $dest"
  return 0
}
'''
    s += "WB_METHOD=%s\n" % _q(method)
    s += "wb_step %s\nmkdir -p -- %s\n" % (_q("Preparing " + root), _q(root))
    dirs = sorted({posixpath.dirname(f["dest"]) for f in files})
    if dirs:
        s += "mkdir -p -- %s\n" % " ".join(_q(d) for d in dirs)
    # runs only in SRA: straight to fasterq-dump
    sra_only = [r for r in preview.get("runs", []) if r["sra_only"]]
    for r in sra_only:
        s += "printf '%%s\\n' %s >> \"$WB_TMP/sra_runs\"\n" % _q(r["run"])
    probe_hosts = sorted({urllib.parse.urlsplit(u).hostname for f in files for u in (f.get("urls") or [])[:1]})
    if probe_hosts:
        s += 'wb_step "Checking the connection to %s"\n' % ", ".join(probe_hosts)
        for h in probe_hosts:
            s += ('if wb_probe %s 443; then echo %s; elif wb_probe %s 21; then echo %s; else echo %s; fi\n'
                  % (_q(h), _q(h + ":443 reachable"), _q(h), _q(h + ": only ftp answers"),
                     _q(h + " did not answer on 443 or 21; trying anyway")))
    downloadable = [f for f in files if f.get("urls")]
    s += 'wb_step "Downloading %d file(s)"\n' % len(downloadable)
    s += "wb_progress 0\n"
    for f in downloadable:
        it = f.get("item") or {}
        s += ('wb_item %s %s %s %s %s %s </dev/null &\n'
              'while [ "$(jobs -rp | wc -l)" -ge "$WB_PARALLEL" ]; do wait -n || true; done\n'
              % (_q(it.get("type") or "file"), _q(it.get("run") or "''") if it.get("run") else "''", _q(f["dest"]),
                 _q(f["bytes"]) if f.get("bytes") and not f.get("bytes_approx") else "''",
                 _q(f["md5"]) if f.get("md5") else "''",
                 " ".join(_q(u) for u in f["urls"])))
    s += "wait\n"
    # SRA fallback / SRA-only runs
    cases = []
    for r in preview.get("runs", []):
        args = " ".join("%s %s" % (_q(x["suffix"]) if x["suffix"] else "''", _q(x["dest"])) for x in r["files"])
        weight = sum(x["file"].get("bytes") or 0 for x in r["files"])
        cases.append("    %s) wb_sra_dump %s %s && wb_item_done %d ;;" % (_q(r["run"]), _q(r["run"]), args, weight))
    if cases:
        s += "wb_sra_map() {\n  case \"$1\" in\n%s\n    *) return 1 ;;\n  esac\n}\n" % "\n".join(cases)
        s += r'''if [ -s "$WB_TMP/sra_runs" ]; then
  wb_step "Converting runs from SRA (prefetch + fasterq-dump)"
  while IFS= read -r run; do
    [ -n "$run" ] || continue
    echo "SRA: $run"
    wb_sra_map "$run" </dev/null || wb_fail "SRA conversion of $run failed"
  done < <(sort -u "$WB_TMP/sra_runs")
fi
'''
    s += "wb_finish || exit 1\n"
    return s


def _validate_source(source):
    kind = source.get("kind")
    if kind == "s3":
        if not BUCKET_RE.match(source.get("bucket") or ""):
            raise ValueError("Invalid s3 bucket name")
        _check_text(source.get("key"), "The s3 key")
    elif kind in ("remote", "url"):
        if not source.get("transports"):
            raise ValueError("The source has no address")
        for t in source["transports"]:
            if t.get("scheme") not in TOOLS or t["scheme"] == "s3":
                raise ValueError("Unsupported scheme %r" % t.get("scheme"))
            if not HOST_RE.match(t.get("host") or ""):
                raise ValueError("Invalid host name %r" % t.get("host"))
            t["port"] = int(t.get("port") or _default_port(t["scheme"]))
            for key in ("path", "user", "query"):
                _check_text(t.get(key), "The " + key)
    elif kind in ("geo", "sra"):
        if not ACCESSION_RE.fullmatch(source.get("accession") or ""):
            raise ValueError("Invalid accession")
    else:
        raise ValueError("Unknown source kind %r" % kind)


def plan(source, secrets, selection, dest, options):
    """Final paths and the download script. See the module docstring for the options."""
    _validate_source(source)
    options = dict(options or {})
    secrets = dict(source.get("secrets") or {}, **(secrets or {}))
    method = options.get("method") or "auto"
    if method not in METHODS:
        raise ValueError("Unknown method %r (one of %s)" % (method, ", ".join(METHODS)))
    if selection is not None and not isinstance(selection, (list, tuple)):
        raise ValueError("selection must be a list or None")
    if selection is not None and len(selection) == 0:
        raise ValueError("Nothing is selected")
    kind = source.get("kind")
    notes = []
    preview = destination_preview(source, dest, selection, options)
    root = preview["root"]
    if kind in ("geo", "sra"):
        script = _geo_script(source, root, preview, options)
        missing = sum(1 for r in preview.get("runs", []) if r["sra_only"])
        if missing:
            notes.append("%d run(s) have no FASTQ in ENA: they are converted from SRA with prefetch + "
                         "fasterq-dump --split-files --include-technical (file names are predicted)" % missing)
        notes.append("FASTQ files go to %s/fastq/, supplementary files to %s/supplementary/" % (root, root))
        files = [{"src": f["src"], "dest": f["dest"], "bytes": f.get("bytes"), "md5": f.get("md5")}
                 for f in preview["files"]]
        if not files:
            raise ValueError("Nothing to download for this selection")
        return {"root": root, "files": files, "script": script, "secrets": {}, "notes": notes}
    if kind == "s3":
        script = _s3_script(source, root, preview, options)
        if source.get("profile_missing") or (source.get("profile") or "default") not in aws_profiles():
            notes.append("AWS profile '%s' is not in ~/.aws/credentials: the download will fail until it is added"
                         % (source.get("profile") or "default"))
        if not source.get("region"):
            notes.append("The bucket's region is looked up when the job starts")
        notes.append("Files are copied with rclone (as aws_download), then aws s3 sync if rclone fails")
        return {"root": root, "files": preview["files"], "script": script, "secrets": {}, "notes": notes}
    if kind not in ("remote", "url"):
        raise ValueError("Unknown source kind %r" % kind)
    opts = dict(options, _selection=selection)
    script, used = _remote_script(source, root, preview, opts, secrets)
    out_secrets = {}
    for name in used:
        if not secrets.get(name):
            raise ValueError("The password for this source is not known: paste the e-mail again")
        out_secrets[name] = secrets[name]
    schemes = [t["scheme"] for t in source.get("transports") or []]
    if "sftp" in schemes and _methods_for("sftp", method)[-1:] == ["scp"]:
        notes.append("scp is the last resort: it cannot resume a partly copied file")
    if len(source.get("transports") or []) > 1:
        notes.append("Transports are tried in order: " + ", ".join(_transport_text(t) for t in source["transports"]))
    if not options.get("entries") and not source.get("is_file"):
        notes.append("The folder has not been listed: the preview shows the top folder only")
    notes.append("md5 sums are checked against md5 lists found in the delivery" if options.get("verify_md5")
                 is not False else "md5 verification is off")
    return {"root": root, "files": preview["files"], "script": script, "secrets": out_secrets, "notes": notes}
