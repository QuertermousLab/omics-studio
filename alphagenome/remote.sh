#!/usr/bin/env bash
# Omics Studio's control of AlphaGenome on its GPU server (FREYA), run there by Omics Studio's restricted key.
#
# The key's line in the GPU server's ~/.ssh/authorized_keys forces this script and allows nothing else (no shell, no
# terminal) apart from forwarding to AlphaGenome's proxy port:
#   restrict,port-forwarding,permitopen="127.0.0.1:8080",command="/home/quanyiz/baldar/vizhub/alphagenome/remote.sh" ssh-ed25519 …
# The command asked for arrives in SSH_ORIGINAL_COMMAND (or as the first argument when run by hand):
#   status       JSON: the four services, readiness, GPU memory, other users of the model (batch jobs)
#   start        start everything (deploy/start.sh) in the background and return at once; repeated starts are harmless
#   stop         stop and free the GPUs, unless batch jobs or other programs are using the model (exit 3)
#   stop-force   stop even then (administrators, after a confirmation on the page)
#   restart-web  restart AlphaGenome Atlas and the proxy (after the pages or proxy.py changed), when they run and
#                nothing else uses the model; the GPU workers keep running
# It lives in Omics Studio's checkout (writable only by its owner), not in AlphaGenome's folder (writable by everyone).
set -euo pipefail

ROOT="${ALPHAGENOME_ROOT:-${HOME}/baldar/alphagenome}"
request="${SSH_ORIGINAL_COMMAND:-${1:-status}}"
case "${request}" in
  status|start|stop|stop-force|restart-web) ;;
  *) echo "refused: ${request}" >&2; exit 2 ;;
esac

# AlphaGenome's settings: the proxy port of this host, Python and the GPU ports (config.env + hosts/<host>.env)
# shellcheck source=/dev/null
source "${ROOT}/deploy/common.sh"
services=(alphagenome-worker@0.service alphagenome-worker@1.service alphagenome-ui.service alphagenome-proxy.service)
log_dir="${ROOT}/deploy/logs/${ALPHAGENOME_HOSTNAME}"
start_log="${log_dir}/omics-studio-start.log"
start_pid="${XDG_RUNTIME_DIR:-/tmp}/alphagenome-omics-studio-start.pid"
export SYSTEMD_PAGER="" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

starting() {
  [[ -r "${start_pid}" ]] && kill -0 "$(<"${start_pid}")" 2>/dev/null
}

# Programs other than AlphaGenome's own services that use the model: batch SNP predictions and other clients connected
# to the proxy or the workers (other accounts' connections show no process; ours show their name). The SSH tunnel's
# connections (sshd: people using the pages) and the services' own connections are not counted.
users_of_model() {
  local ports="(dport = :${PUBLIC_REST_PORT} or dport = :${GPU0_REST_PORT} or dport = :${GPU1_REST_PORT} or dport = :${GPU0_GRPC_PORT} or dport = :${GPU1_GRPC_PORT})"
  local own=" "
  for s in "${services[@]}"; do
    local cg
    cg="$(systemctl --user show -p ControlGroup --value "${s}" 2>/dev/null || true)"
    [[ -n "${cg}" && -r "/sys/fs/cgroup${cg}/cgroup.procs" ]] && own+="$(tr '\n' ' ' < "/sys/fs/cgroup${cg}/cgroup.procs")"
  done
  ss -Htnp state established "${ports}" 2>/dev/null | while read -r _ _ _ peer users; do
    pid="$(grep -o 'pid=[0-9]*' <<< "${users}" | head -1 | cut -d= -f2)"
    name="$(grep -o '(("[^"]*"' <<< "${users}" | head -1 | cut -d'"' -f2)"
    [[ -n "${pid}" && "${own}" == *" ${pid} "* ]] && continue
    [[ "${name}" == "sshd" ]] && continue
    printf '%s\n' "${name:-another account}"
  done
  # batch scripts that are between requests still count
  ps -eo user=,args= | grep -E "(predict_snps|run_background_snps|audit_snp_background|benchmark)\.py" | grep -v grep |
    awk '{print "batch job (" $1 ")"}' || true
}

status_json() {
  local states=() s
  for s in "${services[@]}"; do states+=("$(systemctl --user is-active "${s}" 2>/dev/null || true)"); done
  local ready gpus users
  ready="$(curl -s -m 3 "http://${PUBLIC_HOST}:${PUBLIC_REST_PORT}/readyz" 2>/dev/null || true)"
  gpus="$(nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null || true)"
  users="$(users_of_model | sort | uniq -c || true)"
  STATES="${states[*]}" READY="${ready}" GPUS="${gpus}" USERS="${users}" STARTING="$(starting && echo 1 || echo 0)" \
  PORT="${PUBLIC_REST_PORT}" HOSTNAME_S="${ALPHAGENOME_HOSTNAME}" START_LOG="${start_log}" python3 - <<'EOF'
import json, os
names = ["worker0", "worker1", "ui", "proxy"]
states = dict(zip(names, os.environ["STATES"].split()))
try:
    ready = json.loads(os.environ["READY"] or "null")
except ValueError:
    ready = None
gpus = []
for line in os.environ["GPUS"].splitlines():
    parts = [p.strip() for p in line.split(",")]
    if len(parts) == 5:
        gpus.append({"index": int(parts[0]), "name": parts[1], "used": int(parts[2]), "total": int(parts[3]),
                     "util": int(parts[4]) if parts[4].isdigit() else None})
users = []
for line in os.environ["USERS"].splitlines():
    n, _, what = line.strip().partition(" ")
    if what:
        users.append({"what": what, "count": int(n)})
tail = ""
try:
    with open(os.environ["START_LOG"], "rb") as f:
        f.seek(0, 2); f.seek(max(0, f.tell() - 1500))
        tail = f.read().decode("utf-8", "replace")
except OSError:
    pass
ok = isinstance(ready, dict) and ready.get("ready") is True and ready.get("healthy") == 2
print(json.dumps({"host": os.environ["HOSTNAME_S"], "port": int(os.environ["PORT"]), "services": states,
                  "running": any(v in ("active", "activating", "reloading") for v in states.values()),
                  "ready": bool(ok and states.get("ui") == "active"), "starting": os.environ["STARTING"] == "1",
                  "gpus": gpus, "users": users, "startLog": tail}))
EOF
}

case "${request}" in
  status)
    status_json
    ;;
  start)
    if starting; then echo '{"started": false, "reason": "already starting"}'; exit 0; fi
    mkdir -p "${log_dir}"
    # detached: the SSH command returns at once and the start goes on (start.sh waits up to its timeout for the GPUs)
    setsid nohup bash "${ROOT}/deploy/start.sh" > "${start_log}" 2>&1 < /dev/null &
    echo $! > "${start_pid}"
    echo '{"started": true}'
    ;;
  stop|stop-force)
    if [[ "${request}" == "stop" ]]; then
      busy="$(users_of_model | sort -u | paste -sd ', ' - || true)"
      if [[ -n "${busy}" ]]; then
        printf '{"stopped": false, "reason": "in use by %s"}\n' "${busy//\"/}"
        exit 3
      fi
    fi
    if starting; then kill -- "-$(<"${start_pid}")" 2>/dev/null || kill "$(<"${start_pid}")" 2>/dev/null || true; fi
    rm -f "${start_pid}"
    bash "${ROOT}/deploy/stop.sh" > "${log_dir}/omics-studio-stop.log" 2>&1 || { tail -5 "${log_dir}/omics-studio-stop.log" >&2; exit 1; }
    echo '{"stopped": true}'
    ;;
  restart-web)
    busy="$(users_of_model | sort -u | paste -sd ', ' - || true)"
    if [[ -n "${busy}" ]]; then
      printf '{"restarted": false, "reason": "in use by %s"}\n' "${busy//\"/}"
      exit 3
    fi
    if systemctl --user is-active --quiet alphagenome-proxy.service; then
      systemctl --user restart alphagenome-ui.service alphagenome-proxy.service
      echo '{"restarted": true}'
    else
      echo '{"restarted": false, "reason": "not running"}'
    fi
    ;;
esac
