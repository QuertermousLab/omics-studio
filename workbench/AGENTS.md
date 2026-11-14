# Omics Workbench — guide for agents and new sessions

User documentation: [README.md](README.md). The repository rules in [../AGENTS.md](../AGENTS.md) apply.

- `server.py` (HTTP, routes, permissions), `servers.py` (server list and load monitor: `COLLECTOR` runs with python3 on
  each server every 20 s), `remote.py` (local or SSH execution; sshpass with the password in the environment, public-key
  login off when a password is given; ControlMaster sockets in the runtime folder, named by a keyed hash that includes the
  password), `jobs.py` (job records, `WRAPPER`, polling, logs, e-mail), `fastq.py` (scanning and sample grouping),
  `pipelines.py` (bulk and Cell Ranger / Seurat job scripts, path checks), `download.py` (sources and download scripts),
  `scripts/sc_analysis.R` (the Seurat / Signac analysis). The pages are the hub's (`hub/web/src/workbench-page.ts`,
  `hub/web/src/workbench/`, `hub/web/pages/workbench.html`, `./vizhubctl build`); `server.py` serves
  `hub/static/workbench.html` for every page address. The admin page's Servers and Workbench tabs:
  `auth/web/admin-servers.mjs`.
- **Running jobs belong to people**: never stop, remove or rerun someone's job, and never delete a job folder. Jobs are
  detached (setsid) and the unit has `KillMode=process`, so `./vizhubctl restart workbench` is safe; jobs run with a
  password are then followed through NFS, or ask for the password again.
- **Passwords**: account passwords reach the server encrypted (`rsa:` fields, decrypted in `decrypt_passwords`), stay in
  memory, and never go into a file, a log, a job record or a command line. Only administrators run without a password
  (the service's key) — with one exception: a download into a shared folder (`shared_folder()`: under `/nfs`, its
  nearest existing folder writable by everyone, reached through no link; the lab's folders are 777, homes are not;
  the user's decision, 2026-11-13) runs in the service's account (quanyiz) for anyone signed in, with `umask 0000`.
  Such a job (`shared: true`) keeps its job folder in `jobs.PRIVATE_RUNS` (`state/workbench/runs`, mode 700; not in
  the home, which the user wants left alone) and never reads files through the service (`read_file` refuses): the
  shared folders are writable by everyone, so a job folder there could be swapped for links or fake pid files. Download secrets (delivery passwords) go to the job in `secrets.env` (mode 600, deleted by
  `run.sh` as it starts). Keep it that way.
- Every value from a page reaches a script through `shlex.quote`; sample and group names are checked (`name_ok`).
- The pipelines are the lab's [Pipelines-Wrappers](https://github.com/QuertermousLab/Pipelines-Wrappers) (settings
  `wrappers`, default `/nfs/baldar/quanyiz/app/Pipelines-Wrappers`, its own repository). `RNAseq.sh -c` (comparisons)
  and `ATACseq.sh -M` (MACS2 options) were added there for these pages; the sample scanning of ChIP-seq / ATAC-seq is
  done here (`fastq.py`), and ChIP peak calling (MACS2 with controls) is in `pipelines.bulk_script`.
- Mail: THOR's postfix runs (enabled 2026-11-13; it delivers directly, no relay host); Admin → Workbench can switch to
  SMTP or sendmail on another server.
- To test without signing in: run a second instance with sign-in off on another port and its own state
  (`env -u OMICS_CANVAS_AUTH python3 workbench/server.py --port 18896 --state <scratch>`); never create sign-in sessions.
