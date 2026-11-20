# AlphaGenome in Omics Studio

`/alphagenome/` opens the lab's local **AlphaGenome** deployment: the model on FREYA's two L4 GPUs and its web
browser, **AlphaGenome Atlas**. It shows predicted accessibility, histone marks and RNA, variant effects (REF / ALT /
Δ) and JASPAR motifs, with the coronary smooth muscle (CL:0002592) models. The model does not run all the time. Whoever
needs it presses **Start**. It stops by itself after an hour without predictions, and **Stop** frees the GPUs at once.

AlphaGenome itself lives in `~/baldar/alphagenome` (its own project, the same path on THOR and FREYA). Its
`deploy/README.md` covers the workers, the proxy and the Atlas, and how to use them with an SSH tunnel. This folder
holds only Omics Studio's side of it.

## Addresses

| Address | What |
| --- | --- |
| `/alphagenome/studio/` | Start and stop, the GPUs' memory, who started it and who uses it |
| `/alphagenome/` | AlphaGenome Atlas while the model runs; otherwise the control page |
| `/alphagenome/studio-api/status` | `GET`: the state as JSON |
| `/alphagenome/studio-api/start`, `/stop` | `POST`; `{"force": true}` (administrators) stops it while batch jobs use the model |
| `/alphagenome/health` | open, for `./vizhubctl status` and the home page card |

## How it works

```
browser ── gateway :8892 ── alphagenome/server.py :8897 (THOR)
                                   │  ssh -i state/alphagenome/freya_ed25519   (restricted key)
                                   ├─ remote.sh status | start | stop | stop-force | restart-web
                                   └─ tunnel 127.0.0.1:8898 → FREYA 127.0.0.1:8080 (AlphaGenome's proxy)
                                                               ├─ GPU workers (systemd user units on FREYA)
                                                               └─ AlphaGenome Atlas (basePath /alphagenome)
```

- **Start** runs `remote.sh start` on FREYA, which starts AlphaGenome's `deploy/start.sh` in the background: both
  workers, the proxy and the Atlas. That takes about 1–3 minutes. The page polls until the proxy is ready, opens the
  tunnel and goes to the Atlas.
- **Idle stop**: if it was started from the page and nobody has predicted anything for
  `VIZHUB_ALPHAGENOME_IDLE_MINUTES` (60), it is stopped. Predictions, gene, motif and variant lookups, and a page load
  all count as use. The Atlas's 15-second health and track-list polls don't count, so a forgotten tab does not keep the
  GPUs busy. AlphaGenome started by hand on FREYA (`start_alphagenome.sh`) is never stopped automatically.
- **Who can stop it**: anyone signed in, unless someone else used it in the last 10 minutes. Then only an
  administrator can. `remote.sh stop` refuses while other programs use the model: batch SNP predictions
  (`predict_snps.py`, `run_background_snps.py`, …) or other clients connected to the proxy or workers. Only
  administrators can override that with `stop-force`, after a confirmation.
- The Atlas is served unchanged through the tunnel; the path stays `/alphagenome/…`. Omics Studio's session cookie is
  not passed on, and responses come uncompressed (the Atlas would answer zstd, which not every browser reads).

## Setting it up

1. `deploy/site.env`: `VIZHUB_ALPHAGENOME_SSH=quanyiz@<FREYA's address>`, then `./vizhubctl configure --yes` and
   `./vizhubctl install`. The port and idle settings are `VIZHUB_ALPHAGENOME_*`; see `deploy/site.env.example`.
2. The key: `state/alphagenome/freya_ed25519` (made with `ssh-keygen -t ed25519`; not in git). On FREYA, add its
   public half to `~/.ssh/authorized_keys` of the account that runs AlphaGenome, restricted to this script and the
   proxy's port:

   ```
   restrict,port-forwarding,permitopen="127.0.0.1:8080",command="/home/quanyiz/baldar/vizhub/alphagenome/remote.sh" ssh-ed25519 AAAA… omics-studio-alphagenome@THOR
   ```

   With `restrict` the key gets no shell, terminal, agent or X11 forwarding. `command=` runs `remote.sh` whatever
   is asked, and `remote.sh` only accepts its five words. `permitopen` limits the tunnel to the proxy's port; use
   `PUBLIC_REST_PORT` of the host's AlphaGenome configuration, which is 18088 on CHAMPAGNE.
3. `./vizhubctl restart alphagenome`; `./vizhubctl status` checks it.

`remote.sh` lives in this checkout (only its owner can write it), not in AlphaGenome's folder, which everyone can write.

## Changes made to AlphaGenome for this (2026-11-20)

- `ui/next.config.ts`: `basePath: "/alphagenome"`. The pages' requests go through `apiUrl()` (`ui/app/base-path.ts`).
- `deploy/proxy.py`: the pages' `/ui-api/…` also answers under `/alphagenome/`, and `/` redirects there. The proxy serves
  the built assets (`/alphagenome/_next/` from `ui/dist/client/alphagenome/_next`) itself: vinext 1.0.0-beta.2's server
  does not find them under a basePath in this Cloudflare-plugin build. Command-line
  clients and batch scripts keep using the root (`/v1/…`, `/healthz`). An SSH tunnel's `http://127.0.0.1:18080/` still
  works: it leads to `/alphagenome/`.
- The Atlas uses Genome Canvas's layout (top bar, track rail, stage chrome row, status bar) and Nord theme, light and
  dark, following the same saved choice, in the system font. At the top left, **Omics Studio** (home) and
  **AlphaGenome** (this module's start / stop page) appear only when it is opened through Omics Studio.
  Data colours (tracks, REF / ALT / Δ, motif letters) are unchanged.
- After changing the Atlas, rebuild it in `ui/` (`npm run build`). Then restart its web services on each server that
  runs it: `remote.sh restart-web` (or `deploy/restart.sh` there). The GPU workers keep running.

## Tests

`python3 -B -m unittest discover -s tests` (also in CI) runs the control server against a fake GPU server. The fake
`ssh` covers start, the tunnel, the proxy (path kept, cookie dropped), refusing a busy stop, the key options,
static files and the refused commands of `remote.sh`.
