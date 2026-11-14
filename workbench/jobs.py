"""Background jobs of Omics Workbench: downloads, bulk pipelines, Cell Ranger and Seurat runs.

A job runs on the server the person chose, in their account, detached from the web server (setsid nohup): closing the
page, the app or restarting this service never stops it. Its folder is `<output folder>/.omics-workbench/<job id>/`
on that server:
  run.sh       the wrapper (WRAPPER below): the account's interactive-shell environment (PATH, conda), the job's
               environment lines (setup.sh), then body.sh under bash -Eeuo pipefail; writes pid, started, finished,
               exit_code
  body.sh      what the job does (built by bulk.py, singlecell.py, download.py)
  secrets.env  passwords the body needs (mode 600), read and deleted by run.sh as it starts
  log          everything the job prints; `##WB step <text>` lines name the current step, `##WB progress <n>` its share
The record of each job (state/workbench/jobs/<id>.json) holds who started it, where, and what; never passwords. The SSH
password of a job run with a password is kept in memory only, for polling and cancelling: after a restart of this
service such a job is followed through NFS when its folder is readable from here, else it asks for the password again.

Standard library only.
"""

import base64
import json
import os
import re
import secrets
import shlex
import smtplib
import stat
import subprocess
import threading
import time
from email.message import EmailMessage
from pathlib import Path

import remote

ACTIVE = {"starting", "running"}
JOB_ID = re.compile(r"[a-z0-9]{12}")
LOG_CHUNK = 256 * 1024

# job folders of downloads run in this service's account into shared folders: in this checkout's state (mode 700; the
# checkout is not writable by others, unlike the shared folders), so nobody can swap their log, pid or scripts
PRIVATE_RUNS = Path(os.environ.get("OMICS_WORKBENCH_PRIVATE_RUNS") or Path(__file__).resolve().parents[1] / "state" / "workbench" / "runs")

WRAPPER = r"""#!/bin/bash
# Omics Workbench job @ID@ (@TITLE@). Started by the web server; everything it prints is in ./log.
cd "$(dirname "$0")" || exit 1
echo $$ > pid
date +%s > started
rm -f exit_code finished
if [ -f secrets.env ]; then set -a; . ./secrets.env; set +a; rm -f secrets.env; fi
on_term() { echo "##WB cancelled $(date '+%F %T')"; echo 143 > exit_code; date +%s > finished; trap - TERM; kill -TERM -- -$$ 2>/dev/null; exit 143; }
trap on_term TERM INT HUP
if [ "${WB_LOGIN_ENV:-1}" = 1 ]; then
  # the account's environment as in an interactive shell (~/.bashrc returns early for scripts): PATH, conda, modules
  marker=0
  while IFS= read -r -d '' kv; do
    if [ "$marker" = 0 ]; then [ "$kv" = "WB-ENV-START" ] && marker=1; continue; fi
    case "$kv" in BASH*|SHELLOPTS=*|PWD=*|OLDPWD=*|SHLVL=*|_=*|PS1=*|PS2=*|PROMPT_COMMAND=*|WB_*|TERM=*|HIST*|COLUMNS=*|LINES=*) ;;
      *=*) export "$kv" 2>/dev/null ;; esac
  done < <(timeout 60 bash -ic 'printf "WB-ENV-START\0"; env -0' 2>/dev/null </dev/null)
fi
export WB_JOB_DIR="$PWD"
if [ -f setup.sh ]; then . ./setup.sh || { echo "The environment lines (setup.sh) failed" >&2; echo 1 > exit_code; date +%s > finished; exit 1; }; fi
echo "##WB start $(date '+%F %T') on $(hostname) as $(whoami), job folder $PWD"
bash -Eeuo pipefail ./body.sh &
child=$!
wait $child
rc=$?
echo $rc > exit_code.tmp && mv exit_code.tmp exit_code
date +%s > finished
if [ $rc = 0 ]; then echo "##WB done $(date '+%F %T')"; else echo "##WB failed (exit $rc) $(date '+%F %T')"; fi
exit $rc
"""


def now():
    return time.time()


class Jobs:
    def __init__(self, state_dir, servers, settings, public_url=""):
        self.dir = Path(state_dir) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.servers = servers
        self.settings = settings
        self.public_url = public_url.rstrip("/")
        self.lock = threading.RLock()
        self.passwords = {}   # job id -> SSH password (memory only)
        self.jobs = {}
        for f in sorted(self.dir.glob("*.json")):
            try:
                job = json.loads(f.read_text())
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                continue

    # ---------------------------------------------------------------- records

    def save(self, job):
        tmp = self.dir / (job["id"] + ".tmp")
        tmp.write_text(json.dumps(job, indent=1))
        tmp.replace(self.dir / (job["id"] + ".json"))

    def update(self, job, **changes):
        with self.lock:
            job.update(changes)
            self.save(job)

    def get(self, jid):
        if not JOB_ID.fullmatch(jid or "") or jid not in self.jobs:
            raise KeyError("No such job")
        return self.jobs[jid]

    def visible(self, identity, everyone=False):
        jobs = sorted(self.jobs.values(), key=lambda j: -j["created"])
        if everyone and identity.get("admin"):
            return jobs
        return [j for j in jobs if j["owner"] == identity["user"]]

    def allowed(self, job, identity):
        return job["owner"] == identity["user"] or identity.get("admin")

    @staticmethod
    def public(job):
        hidden = {"files"}
        return {k: v for k, v in job.items() if k not in hidden}

    # ---------------------------------------------------------------- starting

    def target(self, job, password=None):
        server = self.servers.get(job["server"])
        return remote.Target(server, job["account"] or None, password or self.passwords.get(job["id"]))

    def create(self, identity, kind, title, server_id, account, password, folder, body, extra_files=None,
               secrets_env=None, setup="", email="", params=None, outputs=None, parent=None, shared=False):
        """Record a job and start it in the background; returns the record. shared: a download into a shared folder,
        run in this service's account for someone who gave none; its job folder is then PRIVATE_RUNS/<id> (the shared
        folders are writable by everyone, so a job folder there could be swapped for links to this account's files)."""
        server = self.servers.get(server_id)
        jid = secrets.token_hex(6)
        folder = folder.rstrip("/")
        if shared:
            PRIVATE_RUNS.mkdir(parents=True, exist_ok=True)
            os.chmod(str(PRIVATE_RUNS), 0o700)
            job_dir = str(PRIVATE_RUNS / jid)
        else:
            job_dir = "{}/.omics-workbench/{}".format(folder, jid)
        job = {"id": jid, "kind": kind, "title": title[:200], "owner": identity["user"], "created": now(),
               "server": server["id"], "server_name": server["name"], "account": (account or "").strip(),
               "auth": "password" if password else "key", "folder": folder, "job_dir": job_dir,
               "state": "starting", "step": "Starting", "progress": None, "pid": None, "exit_code": None,
               "started": None, "finished": None, "email": (email or "").strip(), "notified": False,
               "params": params or {}, "outputs": outputs or [], "parent": parent, "error": None, "shared": bool(shared)}
        files = {"run.sh": (WRAPPER.replace("@ID@", jid).replace("@TITLE@", re.sub(r"[\r\n]", " ", title)[:100]).encode(), 0o755),
                 "body.sh": (body.encode(), 0o755)}
        if setup.strip():
            files["setup.sh"] = (setup.strip().encode() + b"\n", 0o644)
        if secrets_env:
            lines = "".join("{}={}\n".format(k, shlex.quote(str(v))) for k, v in secrets_env.items())
            files["secrets.env"] = (lines.encode(), 0o600)
        for name, data in (extra_files or {}).items():
            files[name] = (data.encode() if isinstance(data, str) else data, 0o644)
        with self.lock:
            self.jobs[jid] = job
            if password:
                self.passwords[jid] = password
            self.save(job)
        threading.Thread(target=self._launch, args=(job, files), daemon=True).start()
        return job

    def _launch(self, job, files):
        try:
            target = self.target(job)
            target.put_files(job["job_dir"], files)
            # only setsid goes to the background (not a subshell that would keep this command's output open)
            code, out, err = target.run("cd {} && {{ setsid nohup bash ./run.sh > log 2>&1 < /dev/null & echo $!; }}".format(
                shlex.quote(job["job_dir"])), timeout=40)
            pid = int(out.strip().splitlines()[-1]) if code == 0 and out.strip() else None
            if not pid:
                raise remote.RemoteError((err or out).strip()[-300:] or "the job did not start")
            self.update(job, state="running", pid=pid, started=now(), step="Running")
        except Exception as error:  # noqa: BLE001 - every failure is shown on the job's page
            self.update(job, state="failed", error=str(error), finished=now(), step="Could not start")
            self.notify(job)

    # ---------------------------------------------------------------- following

    def readable_here(self, job):
        """The job folder through this machine's file system (NFS), when it is the same folder."""
        p = Path(job["job_dir"])
        try:
            return p / "run.sh" if (p / "run.sh").is_file() else None
        except OSError:
            return None

    def poll(self, job):
        """Refresh a running job's state from its folder."""
        script = ("cd {} 2>/dev/null || {{ echo gone; exit 0; }}; "
                  "printf 'exit=%s\\n' \"$(cat exit_code 2>/dev/null)\"; "
                  "printf 'finished=%s\\n' \"$(cat finished 2>/dev/null)\"; "
                  "if [ -s pid ] && kill -0 \"$(cat pid)\" 2>/dev/null; then echo alive=1; else echo alive=0; fi; "
                  "printf 'size=%s\\n' \"$(stat -c %s log 2>/dev/null)\"; "
                  "grep -a '^##WB \\(step\\|progress\\)' log 2>/dev/null | tail -n 40").format(shlex.quote(job["job_dir"]))
        try:
            out = None
            if job["auth"] == "key" or job["id"] in self.passwords:
                code, out, _ = self.target(job).run(script, timeout=30)
            elif self.readable_here(job) and self.read_here(job, "log", 1) is not None:
                out = self.read_local_status(job)
            else:
                if job.get("step") != "Waiting for the password":
                    self.update(job, step="Waiting for the password", needs_password=True)
                return
        except remote.RemoteError as error:
            self.update(job, poll_error=str(error), poll_time=now())
            return
        self.apply_status(job, out)

    def open_here(self, job, path):
        """A file of the job opened through this machine's file system, or None. This service reads as its own account,
        so for a job in another account only what that account could show anyway is read here: no symbolic link, not a
        file of this service's account, readable by everyone. Anything else is read on the job's server, as the job's
        account (a job folder could otherwise point at this account's private files)."""
        try:
            fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return None
        info = os.fstat(fd)
        trusted = job["auth"] == "key" and (job["account"] or remote.SERVICE_USER) == remote.SERVICE_USER
        if not stat.S_ISREG(info.st_mode) or (not trusted and (info.st_uid == os.getuid() or not info.st_mode & stat.S_IROTH)):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")

    def read_here(self, job, name, limit=65536, tail=False):
        f = self.open_here(job, Path(job["job_dir"]) / name)
        if f is None:
            return None
        with f:
            if tail:
                size = os.fstat(f.fileno()).st_size
                f.seek(max(0, size - limit))
            return f.read(limit)

    def read_local_status(self, job):
        def read(name):
            data = self.read_here(job, name, 100)
            return data.decode("utf-8", "replace").strip() if data else ""
        lines = ["exit=" + read("exit_code"), "finished=" + read("finished")]
        f = self.open_here(job, Path(job["job_dir"]) / "log")
        if f is None:
            return None
        with f:
            info = os.fstat(f.fileno())
            # cannot check the process across machines: a log written in the last 30 minutes counts as alive
            lines.append("alive=" + ("1" if now() - info.st_mtime < 1800 or not read("exit_code") else "0"))
            lines.append("size={}".format(info.st_size))
            f.seek(max(0, info.st_size - 65536))
            tail = f.read().decode("utf-8", "replace").splitlines()
        lines += [l for l in tail if l.startswith("##WB step") or l.startswith("##WB progress")][-40:]
        return "\n".join(lines)

    def apply_status(self, job, out):
        if out is None:
            return
        if out.strip() == "gone":
            self.update(job, state="failed", error="The job folder is gone: {}".format(job["job_dir"]), finished=now())
            self.notify(job)
            return
        values, step, progress = {}, job.get("step"), job.get("progress")
        for line in out.splitlines():
            if line.startswith("##WB step "):
                step = line[10:].strip()[:200]
            elif line.startswith("##WB progress "):
                try:
                    progress = max(0.0, min(100.0, float(line.split()[2])))
                except (IndexError, ValueError):
                    pass
            elif "=" in line:
                k, v = line.split("=", 1)
                values[k] = v.strip()
        changes = {"step": step, "progress": progress, "log_size": int(values.get("size") or 0), "poll_time": now(),
                   "poll_error": None, "needs_password": False}
        code = values.get("exit", "")
        if code.lstrip("-").isdigit():
            code = int(code)
            state = "done" if code == 0 else ("cancelled" if job["state"] == "cancelling" or code == 143 else "failed")
            changes.update(state=state, exit_code=code, finished=float(values.get("finished") or now()))
        elif values.get("alive") == "0" and job["state"] in ACTIVE | {"cancelling"}:
            changes.update(state="failed", error="The job stopped without finishing (server restarted or process killed)",
                           finished=now())
        self.update(job, **changes)
        if job["state"] not in ACTIVE and job["state"] != "cancelling":
            self.notify(job)
            self.passwords.pop(job["id"], None)

    def loop(self, every=8):
        while True:
            for job in list(self.jobs.values()):
                if job["state"] in ("running", "cancelling"):
                    try:
                        self.poll(job)
                    except Exception as error:  # noqa: BLE001 - keep following the other jobs
                        print("poll {}: {}".format(job["id"], error), flush=True)
            time.sleep(every)

    def start(self):
        threading.Thread(target=self.loop, name="job-poller", daemon=True).start()

    # ---------------------------------------------------------------- reading and acting

    def log(self, job, offset=0):
        """A piece of the job's log from offset: (text, next offset, size). Read through this machine's file system
        when the job folder is visible here (NFS), else on the job's server."""
        offset = max(0, int(offset))
        f = self.open_here(job, Path(job["job_dir"]) / "log") if self.readable_here(job) else None
        if f is not None:
            with f:
                size = os.fstat(f.fileno()).st_size
                start = offset if offset <= size else 0
                f.seek(start)
                data = f.read(LOG_CHUNK)
            # do not split a UTF-8 character between two reads: drop a trailing incomplete sequence
            i = len(data) - 1
            while i >= 0 and len(data) - i < 4 and (data[i] & 0xC0) == 0x80:
                i -= 1
            if i >= 0 and data[i] >= 0xC0 and len(data) - i < (2 if data[i] < 0xE0 else 3 if data[i] < 0xF0 else 4):
                data = data[:i]
            return data.decode("utf-8", "replace"), start + len(data), size
        if job["auth"] == "password" and job["id"] not in self.passwords:
            raise PermissionError("Give the password of {} to read this job's log".format(job["account"] or "the account"))
        code, out, err = self.target(job).run(
            "f={}/log; s=$(stat -c %s \"$f\" 2>/dev/null || echo 0); o={}; [ $o -gt $s ] && o=0; echo $s $o; "
            "tail -c +$((o + 1)) \"$f\" 2>/dev/null | head -c {}".format(shlex.quote(job["job_dir"]), offset, LOG_CHUNK), timeout=30)
        head, _, rest = out.partition("\n")
        try:
            size, start = (int(x) for x in head.split())
        except ValueError:
            return "", offset, offset
        return rest, start + len(rest.encode("utf-8")), size

    def cancel(self, job, password=None):
        if job["state"] not in ACTIVE:
            raise ValueError("The job is not running")
        if password:
            self.passwords[job["id"]] = password
        if job["auth"] == "password" and job["id"] not in self.passwords:
            raise PermissionError("Give the password of {} to stop this job".format(job["account"]))
        if not job.get("pid"):
            raise ValueError("The job has not started yet")
        self.target(job).run("kill -TERM -- -{0} 2>/dev/null || kill -TERM {0}".format(int(job["pid"])), timeout=30)
        self.update(job, state="cancelling", step="Stopping")

    def remove(self, job):
        if job["state"] in ACTIVE | {"cancelling"}:
            raise ValueError("Stop the job before removing it from the list")
        with self.lock:
            self.jobs.pop(job["id"], None)
            self.passwords.pop(job["id"], None)
            try:
                (self.dir / (job["id"] + ".json")).unlink()
            except OSError:
                pass

    def read_file(self, job, name, limit=2 * 2**20):
        """A file the job wrote (e.g. tables/qc_thresholds_proposed.tsv), relative to its output folder."""
        if name.startswith("/") or ".." in name.split("/"):
            raise PermissionError("Only files inside the job's output folder")
        if job.get("shared"):
            # read as this service's account from a folder anyone can change: never
            raise PermissionError("This job's files are opened from the folder itself, not through Omics Workbench")
        path = job["folder"] + "/" + name
        f = self.open_here(job, path)
        if f is not None:
            with f:
                if os.fstat(f.fileno()).st_size > limit:
                    raise ValueError("File too large")
                return f.read()
        if job["auth"] == "password" and job["id"] not in self.passwords:
            raise PermissionError("Give the password of {} to read the job's files".format(job["account"]))
        code, out, err = self.target(job).run("f={0}; [ -f \"$f\" ] && [ $(stat -c %s \"$f\") -le {1} ] && base64 -w0 \"$f\"".format(
            shlex.quote(path), limit), timeout=60)
        if code != 0:
            raise KeyError("Not found (or too large): {}".format(name))
        return base64.b64decode(out.strip())

    # ---------------------------------------------------------------- e-mail

    def notify(self, job):
        if job.get("notified") or not job.get("email"):
            return
        self.update(job, notified=True)
        threading.Thread(target=self._send, args=(job,), daemon=True).start()

    def _send(self, job):
        try:
            tail = ""
            try:
                text, _, _ = self.log(job, offset=max(0, (job.get("log_size") or 0) - 6000))
                tail = "\n".join(text.splitlines()[-60:])
            except Exception:  # noqa: BLE001 - the e-mail goes without the log
                tail = "(the log could not be read)"
            word = {"done": "finished", "failed": "failed", "cancelled": "was stopped"}.get(job["state"], job["state"])
            link = "{}/workbench/jobs/{}/".format(self.public_url, job["id"]) if self.public_url else ""
            body = ["Your Omics Workbench job {}: {}".format(word, job["title"]), "",
                    "Server: {} ({})".format(job["server_name"], job["account"] or "service account"),
                    "Output folder: {}".format(job["folder"]),
                    "Started: {}".format(time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("started") or job["created"]))),
                    "Ended: {}".format(time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("finished") or now())))]
            if job.get("error"):
                body.append("Error: {}".format(job["error"]))
            if link:
                body += ["", "Log and details: " + link]
            body += ["", "Last lines of the log:", tail]
            send_mail(self.settings.get("mail") or {}, self.servers, job["email"],
                      "[Omics Workbench] {} {}".format(job["title"][:80], word), "\n".join(body))
            self.update(job, mail_error=None)
        except Exception as error:  # noqa: BLE001 - shown on the job's page
            self.update(job, mail_error=str(error))


def send_mail(config, servers, to, subject, text):
    """Send with the method set in Admin → Workbench: smtp (host, port, starttls, user, password), sendmail (this
    server's), or ssh (sendmail on another server that has a running mail service)."""
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", to or ""):
        raise ValueError("Not an e-mail address: {}".format(to))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.get("from") or "Omics Workbench <{}@{}>".format(remote.SERVICE_USER, os.uname().nodename)
    msg["To"] = to
    msg.set_content(text)
    method = config.get("method") or "sendmail"
    if method == "smtp":
        host, port = config.get("host") or "localhost", int(config.get("port") or 25)
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if config.get("starttls"):
                smtp.starttls()
            if config.get("user"):
                smtp.login(config["user"], config.get("password") or "")
            smtp.send_message(msg)
    elif method == "ssh":
        target = remote.Target(servers.get(config.get("server") or ""))
        code, out, err = target.run("/usr/sbin/sendmail -t -oi", input=bytes(msg), timeout=60)
        if code != 0:
            raise RuntimeError("sendmail on {}: {}".format(target.describe(), (err or out).strip()[-300:]))
    else:
        proc = subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=bytes(msg), capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError("sendmail: {}".format(proc.stderr.decode("utf-8", "replace").strip()[-300:]))
