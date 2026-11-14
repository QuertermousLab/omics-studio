# Omics Workbench

The analysis part of Omics Studio, at **http://171.65.68.140:8892/workbench/** (and in the Omics Workbench Mac app):
download sequencing data, run the lab's pipelines ([Pipelines-Wrappers](https://github.com/QuertermousLab/Pipelines-Wrappers))
on the lab servers, and follow the jobs. Everything runs in the background on the server you choose: closing the page or
the app never stops a job, and an e-mail can tell you when it ends.

| Page | What it does |
| --- | --- |
| `/workbench/` | Your recent jobs and the servers' load (the other pages are tabs in the top bar, after "Omics Workbench") |
| `/workbench/download/` | Downloads: a link (ftp, sftp, https), a whole delivery e-mail (Novogene, MedGenome, …: host, port, account and password are read from it), an `s3://` address (rclone with the profiles of `~/.aws/credentials`), or a GEO / SRA accession (choose raw FASTQs or processed files). Shows the final paths before starting. SFTP, lftp, scp and HTTPS are tried in turn (ports are sometimes blocked), transfers resume, MD5 sums are checked. Always runs on THOR, the default server (no server choice: the network matters, not the CPU), in your account. |
| `/workbench/bulk/` | RNA-seq (`RNAseq.sh`: STAR, featureCounts, DESeq2 for the comparisons you choose; every pair by default), ChIP-seq, CUT&RUN, CUT&Tag (`ChIPseq.sh`, then MACS2 with each sample's input / IgG, found from the names, or none), ATAC-seq (`ATACseq.sh` with your MACS2 options). The FASTQ folder is scanned: samples, lanes, read pairs, names, RNA-seq groups and ChIP controls are proposed for you to edit. |
| `/workbench/single-cell/` | Cell Ranger (`cellranger.sh`: GEX, ATAC, multiome; optional aggr; multiome GEX and ATAC libraries are paired by name without the 10x index `SI-…` and the flowcell, runs of one library in several folders become one library with a libraries-CSV row per run, merged copies are listed unticked), then the Seurat / Signac analysis (`scripts/sc_analysis.R`): QC thresholds per sample proposed from the median ± MADs (as in the lab's multiome analysis) for you to review, filtering, SCT / LSI, Harmony, WNN, clusters, UMAP, label transfer from a reference (`celltype`, `predicted.id`, cluster types… detected or chosen), RDS and h5ad / h5mu. Also for existing Cell Ranger outputs. |
| `/workbench/jobs/<id>/` | A job: its state and step, the live log, Stop; for a Seurat QC job, the thresholds to review and Continue. |
| `/admin/#servers` | (administrators) Each server's CPU (per core), load, memory, swap, disk and network throughput with two hours of history, who is logged in, CPU and memory per account, the busiest processes, GPUs and disks; the server list; installing the SSH key. |
| `/admin/#workbench` | (administrators) The Pipelines-Wrappers folder, environment lines for every job, how e-mails are sent (sendmail, SMTP, or sendmail on another server), s3 buckets. |

## Where and as whom jobs run

You choose the server from cards showing its current load (THOR by default; downloads always run on THOR). `/nfs` paths are the same everywhere, and
**Check paths on the server** verifies, from that server and in that account, the inputs, that the output folder can
be written, the references and indexes, and the programs. The job runs in the account you give, with its password: the
password is sent encrypted (RSA, as the sign-in page), kept in the service's memory while the job runs (to follow and
stop it) and never written. Administrators may leave account and password empty: the job then runs in the account set for
the server in Admin → Servers, with the service's SSH key.

Each job's folder is `<output folder>/.omics-workbench/<job id>/` on that server: `run.sh` (the wrapper: the account's
interactive environment, your environment lines, then `body.sh`), `body.sh` (the commands; **Show script** shows it
before starting), `log`, `pid`, `exit_code`. To rerun one by hand: `bash <job folder>/run.sh`.

## Service

`workbench/server.py` (standard library, Python 3.8+), 127.0.0.1:8896, unit `vizhub-workbench.service`
(`./vizhubctl restart workbench`; jobs keep running). State in `../state/workbench/`: `servers.json`, `settings.json`
(mode 600: it may hold an SMTP password), `jobs/<id>.json`. Tests: `python3 -m unittest discover -s workbench/tests`.
