"""Running commands on the lab servers: here (THOR, as the account that runs the service) or over SSH.

A Target is a server from servers.json plus the account to use. Without a password the service's own SSH key is used
(only administrators may run that way: workbench/server.py checks); with a password, sshpass hands it to ssh through
the environment (never on a command line) and public-key login is switched off, so a wrong password never falls back
to the service's key.

Connections are shared (ssh ControlMaster) so the load monitor and job polling cost one login per server. The control
socket's name is a keyed hash of host, port, account and password, so a socket opened with one person's password is
never reused by a request that does not know it. Sockets live in the runtime folder (sockets do not work on NFS).

Standard library only (Python 3.8+).
"""

import getpass
import hashlib
import hmac
import os
import secrets
import shlex
import socket
import subprocess
import tarfile
import io
from pathlib import Path

SERVICE_USER = getpass.getuser()
_SALT = secrets.token_bytes(16)  # per process: a restart never reuses another session's sockets


def runtime_dir():
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/{}".format(os.getuid()))
    if not base.is_dir():
        base = Path("/tmp")
    folder = base / "omics-workbench-{}".format(os.getuid())
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def local_addresses():
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        names.update(out.split())
    except Exception:
        pass
    return {n.lower() for n in names if n}


LOCAL = local_addresses()


class RemoteError(Exception):
    """A command could not be run (connection, login); its message is meant for the page."""


class Target:
    def __init__(self, server, user=None, password=None):
        self.server = server
        self.user = (user or "").strip() or server.get("user") or SERVICE_USER
        self.password = password or None
        self.host = server["host"]
        self.port = int(server.get("port") or 22)

    @property
    def local(self):
        """Run directly (no SSH): this machine, the service's own account, no password given."""
        return self.host.lower() in LOCAL and self.user == SERVICE_USER and not self.password

    @property
    def uses_key(self):
        return not self.password

    def describe(self):
        return "{}@{}".format(self.user, self.server.get("name") or self.host)

    def control_path(self):
        key = "|".join([self.host, str(self.port), self.user, self.password or "<key>"]).encode()
        digest = hmac.new(_SALT, key, hashlib.sha256).hexdigest()[:20]
        return str(runtime_dir() / digest)

    def ssh_argv(self, command):
        argv = ["ssh", "-p", str(self.port), "-T",
                "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR",
                "-o", "ControlMaster=auto", "-o", "ControlPersist=600", "-o", "ControlPath=" + self.control_path()]
        if self.password:
            argv = ["sshpass", "-e"] + argv + ["-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                                                 "-o", "PreferredAuthentications=password,keyboard-interactive"]
        else:
            argv += ["-o", "BatchMode=yes"]
        # the remote login shell may not be bash: run bash explicitly
        return argv + ["{}@{}".format(self.user, self.host), "--", "bash -c " + shlex.quote(command)]

    def run(self, command, input=None, timeout=60, check=False):
        """Run a bash command; returns (exit code, stdout, stderr) as text (bytes for binary input)."""
        env = dict(os.environ)
        if self.local:
            argv = ["bash", "-c", command]
        else:
            argv = self.ssh_argv(command)
            if self.password:
                env["SSHPASS"] = self.password
        try:
            proc = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, env=env,
                                  stdin=None if input is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RemoteError("{}: no answer within {} s".format(self.describe(), timeout))
        except FileNotFoundError as error:
            raise RemoteError("{} is not installed on this server".format(error.filename))
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if not self.local:
            login_failure(self, proc.returncode, err)
        if check and proc.returncode != 0:
            raise RemoteError("{}: {}".format(self.describe(), (err or out).strip()[-400:] or "exit {}".format(proc.returncode)))
        return proc.returncode, out, err

    def put_files(self, folder, files, timeout=60):
        """Create folder (mode 755) on the target and write files {name: (bytes, mode)} into it."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, (data, mode) in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = mode
                info.mtime = int(__import__("time").time())
                tar.addfile(info, io.BytesIO(data))
        quoted = shlex.quote(folder)
        self.run("umask 022 && mkdir -p {0} && chmod 755 {0} && tar -C {0} -xpf - --no-same-owner".format(quoted),
                 input=buffer.getvalue(), timeout=timeout, check=True)


def login_failure(target, code, err):
    """Turn ssh / sshpass failures into messages for the page."""
    text = err.strip()
    if target.password and code == 5:
        raise RemoteError("{}: the password was not accepted".format(target.describe()))
    if target.password and code in (1, 2, 3, 4, 6):
        raise RemoteError("{}: could not log in ({})".format(target.describe(), text[-200:] or "sshpass {}".format(code)))
    if code == 255:
        if "Permission denied" in text:
            how = "the password" if target.password else "the server's SSH key (give the account's password, or install the key in Admin → Servers)"
            raise RemoteError("{}: login refused with {}".format(target.describe(), how))
        raise RemoteError("{}: {}".format(target.describe(), text[-300:] or "SSH connection failed"))


def install_key(server, user, password, timeout=40):
    """Copy the service account's public key to user@server (ssh-copy-id with the password, used once)."""
    key = Path.home() / ".ssh" / "id_rsa.pub"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        if (Path.home() / ".ssh" / name).is_file():
            key = Path.home() / ".ssh" / name
            break
    if not key.is_file():
        raise RemoteError("This server's account has no SSH key (~/.ssh/id_*.pub)")
    public = key.read_text().strip()
    target = Target(server, user, password)
    command = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
               "grep -qxF {0} ~/.ssh/authorized_keys || printf '%s\\n' {0} >> ~/.ssh/authorized_keys; echo installed").format(shlex.quote(public))
    code, out, err = target.run(command, timeout=timeout)
    if code != 0 or "installed" not in out:
        raise RemoteError("{}: {}".format(target.describe(), (err or out).strip()[-300:]))
    # check that the key works now
    code, out, _ = Target(server, user).run("echo ok", timeout=timeout)
    return code == 0 and out.strip() == "ok"
