# AlphaGenome in Omics Studio — guide for agents and new sessions

User documentation: [README.md](README.md). The repository rules in [../AGENTS.md](../AGENTS.md) apply.

- `server.py` (standard library, 127.0.0.1:8897, `vizhub-alphagenome.service`): control page and API, the SSH tunnel to
  AlphaGenome's proxy on the GPU server (FREYA), the proxy of `/alphagenome/…` through it, idle stop. `remote.sh`: the
  only thing the restricted key may run on the GPU server (status / start / stop / stop-force / restart-web).
  `web/`: the control page (Genome Canvas's tokens).
- AlphaGenome itself is `~/baldar/alphagenome`, the lab's own project (not a git repository; its guide is
  `deploy/README.md`). Changes there for Omics Studio: basePath `/alphagenome` (`ui/next.config.ts`, `ui/app/base-path.ts`,
  `UI_BASE_PATH` in `deploy/proxy.py`), the Nord theme, the Start / stop link. Keep the three base-path places in step.
  Never rebuild its `ui/dist` without restarting its web services afterwards (`remote.sh restart-web`): a running Atlas
  serves its assets from that folder. CHAMPAGNE shares the folder.
- **The GPUs belong to people's work**: never stop AlphaGenome while batch jobs or other programs use it (that is what
  `stop` refuses; `stop-force` is for administrators on the page), and never stop it if it was started by hand on the
  server. Only a page start is stopped automatically.
- **The key**: `state/alphagenome/freya_ed25519` (not in git, 600). The server uses it with `IdentitiesOnly`, never an
  agent or the account's own keys (THOR has no login key for the lab servers since 2026-11-17, by the user's choice).
  It is restricted in FREYA's `authorized_keys` (`restrict,port-forwarding,permitopen,command=remote.sh`). Never widen it,
  and never add another key there for this. The GPU server's address lives only in `deploy/site.env`
  (`VIZHUB_ALPHAGENOME_SSH`), not in git.
- The session cookie never goes to AlphaGenome (`proxy()` drops Cookie / Authorization). AlphaGenome's own admin token
  for global BigWig tracks stays AlphaGenome's.
- Test without FREYA: `tests/test_alphagenome.py` (a fake `ssh` on PATH), or run a second instance with `--remote ""`
  on another port. Never create sign-in sessions on the live store for testing.
